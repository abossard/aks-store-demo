#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


CONFIGMAP_NAME = "ama-metrics-settings-configmap"
CONFIGMAP_NAMESPACE = "kube-system"
ALLOWED_DATA_KEYS = {
    "schema-version",
    "config-version",
    "controlplane-metrics",
}
NAP_METRICS = (
    "karpenter_nodeclaims_disrupted_total",
    "karpenter_nodes_created_total",
    "karpenter_nodes_terminated_total",
    "karpenter_pods_state",
    "karpenter_voluntary_disruption_decisions_total",
    "karpenter_voluntary_disruption_eligible_nodes",
)


class MergeError(RuntimeError):
    pass


class Runner:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def run(
        self,
        category: str,
        command: list[str],
        *,
        document: Any | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            command,
            input=(
                json.dumps(document, sort_keys=True, separators=(",", ":"))
                if document is not None
                else None
            ),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.events.append({"category": category, "exit": result.returncode})
        if check and result.returncode:
            detail = (result.stderr or result.stdout).replace("\n", " ").strip()
            raise MergeError(
                f"{category} exited {result.returncode}: {detail[:240]}"
            )
        return result

    def json(
        self,
        category: str,
        command: list[str],
        *,
        document: Any | None = None,
    ) -> Any:
        result = self.run(category, command, document=document)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise MergeError(f"{category} returned invalid JSON") from error


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(compact_json(value).encode()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def kubectl_command(
    kubectl: str,
    kubeconfig: Path | None,
    arguments: list[str],
) -> list[str]:
    command = [kubectl]
    if kubeconfig is not None:
        command.extend(["--kubeconfig", str(kubeconfig)])
    return [*command, *arguments]


def absent_state() -> dict[str, Any]:
    return {
        "name": CONFIGMAP_NAME,
        "namespace": CONFIGMAP_NAMESPACE,
        "present": False,
    }


def state_shape(document: dict[str, Any] | None) -> dict[str, Any]:
    if document is None:
        return absent_state()
    metadata = document.get("metadata", {})
    return {
        "annotations": metadata.get("annotations", {}),
        "binaryData": document.get("binaryData", {}),
        "data": document.get("data", {}),
        "immutable": document.get("immutable"),
        "labels": metadata.get("labels", {}),
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "present": True,
    }


def state_hash(document: dict[str, Any] | None) -> str:
    return sha256_json(state_shape(document))


def resource_version_hash(document: dict[str, Any] | None) -> str:
    if document is None:
        return sha256_text("")
    value = str(document.get("metadata", {}).get("resourceVersion", ""))
    return sha256_text(value)


def read_current(
    runner: Runner,
    kubectl: str,
    kubeconfig: Path | None,
) -> dict[str, Any] | None:
    result = runner.run(
        "kubectl.configmap.get",
        kubectl_command(
            kubectl,
            kubeconfig,
            [
                "get",
                "configmap",
                CONFIGMAP_NAME,
                "--namespace",
                CONFIGMAP_NAMESPACE,
                "--ignore-not-found",
                "-o",
                "json",
            ],
        ),
    )
    if not result.stdout.strip():
        return None
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise MergeError("ConfigMap read returned invalid JSON") from error
    if not isinstance(document, dict):
        raise MergeError("ConfigMap read returned a non-object")
    return document


def load_intent(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    required = (
        "name: ama-metrics-settings-configmap",
        "namespace: kube-system",
        "schema-version: v2",
        "config-version: ahmk8s1",
        "default-targets-scrape-enabled: |-",
        "node-auto-provisioning = true",
    )
    if any(value not in text for value in required):
        raise MergeError("AMA intent manifest does not match the supported contract")
    if "__" in text:
        raise MergeError("AMA intent manifest contains an unresolved token")


def _section_bounds(lines: list[str], section: str) -> tuple[int, int] | None:
    header = f"{section}: |-"
    matches = [
        index
        for index, line in enumerate(lines)
        if line.strip() == header and not line[:1].isspace()
    ]
    if len(matches) > 1:
        raise MergeError(f"duplicate controlplane section: {section}")
    if not matches:
        return None
    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line and not line[:1].isspace() and line.strip().endswith(": |-"):
            end = index
            break
    return start, end


def _assignment_matches(
    lines: list[str],
    bounds: tuple[int, int],
    key: str,
) -> list[tuple[int, re.Match[str]]]:
    pattern = re.compile(
        rf"^(\s*{re.escape(key)}\s*=\s*)([^#]*?)(\s*(?:#.*)?)$"
    )
    return [
        (index, match)
        for index in range(bounds[0] + 1, bounds[1])
        if (match := pattern.match(lines[index]))
    ]


def read_assignment(
    text: str,
    section: str,
    key: str,
) -> str | None:
    lines = text.splitlines()
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return None
    matches = _assignment_matches(lines, bounds, key)
    if len(matches) > 1:
        raise MergeError(f"duplicate assignment: {section}.{key}")
    if not matches:
        return None
    return matches[0][1].group(2).strip()


def set_assignment(
    text: str,
    section: str,
    key: str,
    value: str,
) -> str:
    trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    bounds = _section_bounds(lines, section)
    if bounds is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([f"{section}: |-", f"  {key} = {value}"])
    else:
        matches = _assignment_matches(lines, bounds, key)
        if len(matches) > 1:
            raise MergeError(f"duplicate assignment: {section}.{key}")
        if matches:
            index, match = matches[0]
            lines[index] = f"{match.group(1)}{value}{match.group(3)}"
        else:
            indentation = "  "
            for index in range(bounds[0] + 1, bounds[1]):
                if lines[index].strip() and lines[index][:1].isspace():
                    indentation = lines[index][
                        : len(lines[index]) - len(lines[index].lstrip())
                    ]
                    break
            lines.insert(bounds[1], f"{indentation}{key} = {value}")
    rendered = "\n".join(lines)
    return rendered + ("\n" if trailing_newline else "")


def parse_boolean(value: str | None) -> bool | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise MergeError("AMA boolean assignment has an unsupported value")


def merge_document(
    current: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    if current is None:
        return (
            {
                "apiVersion": "v1",
                "data": {
                    "config-version": "ahmk8s1",
                    "controlplane-metrics": (
                        "default-targets-scrape-enabled: |-\n"
                        "  node-auto-provisioning = true"
                    ),
                    "schema-version": "v2",
                },
                "kind": "ConfigMap",
                "metadata": {
                    "name": CONFIGMAP_NAME,
                    "namespace": CONFIGMAP_NAMESPACE,
                },
            },
            sorted(ALLOWED_DATA_KEYS),
        )

    data = current.get("data", {})
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in data.items()
    ):
        raise MergeError("AMA ConfigMap data must contain only string values")
    schema = data.get("schema-version") or data.get("config_schema_version")
    if schema not in (None, "", "v2"):
        raise MergeError("AMA ConfigMap schema v1 cannot be merged safely")
    controlplane = data.get("controlplane-metrics", "")
    if not isinstance(controlplane, str):
        raise MergeError("AMA controlplane-metrics must be a string")
    controlplane = set_assignment(
        controlplane,
        "default-targets-scrape-enabled",
        "node-auto-provisioning",
        "true",
    )
    minimal = parse_boolean(
        read_assignment(
            controlplane,
            "minimal-ingestion-profile",
            "enabled",
        )
    )
    if minimal is False:
        keep_list = "|".join(NAP_METRICS)
        controlplane = set_assignment(
            controlplane,
            "default-targets-metrics-keep-list",
            "node-auto-provisioning",
            f'"{keep_list}"',
        )

    proposed = json.loads(json.dumps(current))
    proposed.pop("status", None)
    metadata = proposed.setdefault("metadata", {})
    for key in (
        "creationTimestamp",
        "managedFields",
        "resourceVersion",
        "uid",
    ):
        metadata.pop(key, None)
    proposed_data = proposed.setdefault("data", {})
    changed: list[str] = []
    intended = {
        "controlplane-metrics": controlplane,
        "schema-version": data.get("schema-version") or "v2",
    }
    if "config-version" not in data:
        intended["config-version"] = "ahmk8s1"
    for key, value in intended.items():
        if proposed_data.get(key) != value:
            proposed_data[key] = value
            changed.append(key)
    return proposed, sorted(changed)


def json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def data_patch(
    current: dict[str, Any],
    proposed: dict[str, Any],
    changed_keys: list[str],
) -> list[dict[str, Any]]:
    resource_version = current.get("metadata", {}).get("resourceVersion")
    if not isinstance(resource_version, str) or not resource_version:
        raise MergeError("AMA ConfigMap resourceVersion is missing")
    patch: list[dict[str, Any]] = [
        {
            "op": "test",
            "path": "/metadata/resourceVersion",
            "value": resource_version,
        }
    ]
    current_data = current.get("data")
    if not isinstance(current_data, dict):
        patch.append({"op": "add", "path": "/data", "value": {}})
        current_data = {}
    proposed_data = proposed.get("data", {})
    for key in changed_keys:
        patch.append(
            {
                "op": "replace" if key in current_data else "add",
                "path": f"/data/{json_pointer(key)}",
                "value": proposed_data[key],
            }
        )
    return patch


def validate_server_result(
    current: dict[str, Any] | None,
    proposed: dict[str, Any],
    actual: dict[str, Any],
) -> None:
    if actual.get("data", {}) != proposed.get("data", {}):
        raise MergeError("server validation changed AMA ConfigMap data")
    actual_metadata = actual.get("metadata", {})
    proposed_metadata = proposed.get("metadata", {})
    if current is not None and (
        actual_metadata.get("labels", {}) != proposed_metadata.get("labels", {})
        or actual_metadata.get("annotations", {})
        != proposed_metadata.get("annotations", {})
    ):
        raise MergeError("server validation changed AMA metadata")


def apply_merge(
    runner: Runner,
    kubectl: str,
    kubeconfig: Path | None,
    current: dict[str, Any] | None,
    proposed: dict[str, Any],
    changed_keys: list[str],
) -> dict[str, Any]:
    if not changed_keys:
        return current or proposed
    if current is None:
        dry_run = runner.json(
            "kubectl.configmap.create.dry-run",
            kubectl_command(
                kubectl,
                kubeconfig,
                ["create", "--dry-run=server", "-f", "-", "-o", "json"],
            ),
            document=proposed,
        )
        validate_server_result(None, proposed, dry_run)
        return runner.json(
            "kubectl.configmap.create",
            kubectl_command(
                kubectl,
                kubeconfig,
                ["create", "-f", "-", "-o", "json"],
            ),
            document=proposed,
        )

    patch = data_patch(current, proposed, changed_keys)
    patch_text = compact_json(patch)
    dry_run = runner.json(
        "kubectl.configmap.patch.dry-run",
        kubectl_command(
            kubectl,
            kubeconfig,
            [
                "patch",
                "configmap",
                CONFIGMAP_NAME,
                "--namespace",
                CONFIGMAP_NAMESPACE,
                "--type=json",
                "--patch",
                patch_text,
                "--dry-run=server",
                "-o",
                "json",
            ],
        ),
    )
    validate_server_result(current, proposed, dry_run)
    return runner.json(
        "kubectl.configmap.patch",
        kubectl_command(
            kubectl,
            kubeconfig,
            [
                "patch",
                "configmap",
                CONFIGMAP_NAME,
                "--namespace",
                CONFIGMAP_NAMESPACE,
                "--type=json",
                "--patch",
                patch_text,
                "-o",
                "json",
            ],
        ),
    )


def rollback(
    runner: Runner,
    kubectl: str,
    kubeconfig: Path | None,
    backup_path: Path,
    expected_current_hash: str,
) -> dict[str, Any]:
    backup = json.loads(backup_path.read_text(encoding="utf-8"))
    current = read_current(runner, kubectl, kubeconfig)
    if state_hash(current) != expected_current_hash:
        raise MergeError("AMA rollback refused because current state changed")
    if backup == absent_state():
        runner.run(
            "kubectl.configmap.delete.dry-run",
            kubectl_command(
                kubectl,
                kubeconfig,
                [
                    "delete",
                    "configmap",
                    CONFIGMAP_NAME,
                    "--namespace",
                    CONFIGMAP_NAMESPACE,
                    "--dry-run=server",
                ],
            ),
        )
        runner.run(
            "kubectl.configmap.delete",
            kubectl_command(
                kubectl,
                kubeconfig,
                [
                    "delete",
                    "configmap",
                    CONFIGMAP_NAME,
                    "--namespace",
                    CONFIGMAP_NAMESPACE,
                    "--wait=true",
                ],
            ),
        )
        restored = read_current(runner, kubectl, kubeconfig)
        if restored is not None:
            raise MergeError("AMA rollback did not remove the created ConfigMap")
    else:
        if not isinstance(backup, dict) or current is None:
            raise MergeError("AMA rollback backup is invalid")
        backup_data = backup.get("data", {})
        current_data = current.get("data", {})
        changed_keys = sorted(
            key
            for key in set(backup_data) | set(current_data)
            if backup_data.get(key) != current_data.get(key)
        )
        if not set(changed_keys) <= ALLOWED_DATA_KEYS:
            raise MergeError("AMA rollback found an unrelated concurrent change")
        proposed = json.loads(json.dumps(current))
        proposed["data"] = dict(current_data)
        for key in changed_keys:
            if key in backup_data:
                proposed["data"][key] = backup_data[key]
            else:
                proposed["data"].pop(key, None)
        resource_version = current.get("metadata", {}).get("resourceVersion")
        patch: list[dict[str, Any]] = [
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": resource_version,
            }
        ]
        for key in changed_keys:
            path = f"/data/{json_pointer(key)}"
            if key in backup_data:
                patch.append(
                    {
                        "op": "replace" if key in current_data else "add",
                        "path": path,
                        "value": backup_data[key],
                    }
                )
            else:
                patch.append({"op": "remove", "path": path})
        patch_text = compact_json(patch)
        runner.json(
            "kubectl.configmap.rollback.dry-run",
            kubectl_command(
                kubectl,
                kubeconfig,
                [
                    "patch",
                    "configmap",
                    CONFIGMAP_NAME,
                    "--namespace",
                    CONFIGMAP_NAMESPACE,
                    "--type=json",
                    "--patch",
                    patch_text,
                    "--dry-run=server",
                    "-o",
                    "json",
                ],
            ),
        )
        runner.json(
            "kubectl.configmap.rollback",
            kubectl_command(
                kubectl,
                kubeconfig,
                [
                    "patch",
                    "configmap",
                    CONFIGMAP_NAME,
                    "--namespace",
                    CONFIGMAP_NAMESPACE,
                    "--type=json",
                    "--patch",
                    patch_text,
                    "-o",
                    "json",
                ],
            ),
        )
        restored = read_current(runner, kubectl, kubeconfig)
        if state_hash(restored) != state_hash(backup):
            raise MergeError("AMA rollback did not restore the original state")
    return {
        "commands": runner.events,
        "restoredStateSha256": state_hash(backup if backup != absent_state() else None),
        "status": "rolled-back",
    }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    kubectl = shutil.which(args.kubectl) or args.kubectl
    runner = Runner()
    kubeconfig = args.kubeconfig.resolve() if args.kubeconfig else None
    if args.rollback_backup:
        return rollback(
            runner,
            kubectl,
            kubeconfig,
            args.rollback_backup.resolve(),
            args.expected_current_state_sha256,
        )

    load_intent(args.manifest.resolve())
    current = read_current(runner, kubectl, kubeconfig)
    pre_hash = state_hash(current)
    if args.expected_state_sha256 and pre_hash != args.expected_state_sha256:
        raise MergeError(
            f"AMA expected-state hash mismatch: {pre_hash}"
        )
    if args.backup:
        args.backup.parent.mkdir(parents=True, exist_ok=True)
        args.backup.write_text(
            compact_json(current if current is not None else absent_state()),
            encoding="utf-8",
        )
    proposed, changed_keys = merge_document(current)
    applied = apply_merge(
        runner,
        kubectl,
        kubeconfig,
        current,
        proposed,
        changed_keys,
    )
    observed = read_current(runner, kubectl, kubeconfig)
    if observed is None:
        raise MergeError("AMA ConfigMap disappeared after merge")
    if observed.get("data", {}) != proposed.get("data", {}):
        raise MergeError("AMA ConfigMap post-state differs from the merge")
    unrelated_before = {
        key: value
        for key, value in (current or {}).get("data", {}).items()
        if key not in ALLOWED_DATA_KEYS
    }
    unrelated_after = {
        key: value
        for key, value in observed.get("data", {}).items()
        if key not in ALLOWED_DATA_KEYS
    }
    metadata_before = (current or {}).get("metadata", {})
    metadata_after = observed.get("metadata", {})
    unrelated_preserved = (
        unrelated_before == unrelated_after
        and metadata_before.get("labels", {}) == metadata_after.get("labels", {})
        and metadata_before.get("annotations", {})
        == metadata_after.get("annotations", {})
    )
    if not unrelated_preserved:
        raise MergeError("AMA merge changed unrelated fields")
    return {
        "changedDataKeys": changed_keys,
        "commands": runner.events,
        "created": current is None and bool(changed_keys),
        "napEnabled": (
            read_assignment(
                observed.get("data", {}).get("controlplane-metrics", ""),
                "default-targets-scrape-enabled",
                "node-auto-provisioning",
            )
            == "true"
        ),
        "postResourceVersionSha256": resource_version_hash(applied),
        "postStateSha256": state_hash(observed),
        "preResourceVersionSha256": resource_version_hash(current),
        "preStateSha256": pre_hash,
        "schemaVersion": observed.get("data", {}).get("schema-version"),
        "status": "changed" if changed_keys else "unchanged",
        "unrelatedFieldsPreserved": unrelated_preserved,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-state-sha256")
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--rollback-backup", type=Path)
    parser.add_argument("--expected-current-state-sha256")
    args = parser.parse_args()
    if args.rollback_backup:
        if not args.expected_current_state_sha256:
            parser.error(
                "--expected-current-state-sha256 is required for rollback"
            )
    elif args.manifest is None:
        parser.error("--manifest is required")
    return args


def main() -> int:
    args = parse_args()
    try:
        result = execute(args)
    except (MergeError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
