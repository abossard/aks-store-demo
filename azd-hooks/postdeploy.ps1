#!/usr/bin/env pwsh

if ($env:DEPLOY_ISTIO -ceq "true") {
    & kubectl wait --for=condition=Accepted gatewayclass/istio --timeout=10m
    if ($LASTEXITCODE -ne 0) {
        throw "Managed Istio GatewayClass did not become accepted."
    }

    & kubectl apply -f sample-manifests/istio/gateway-api.yaml
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply the Gateway API route."
    }

    if ($env:DEPLOY_OBSERVABILITY_TOOLS -ceq "true") {
        & kubectl apply -f sample-manifests/istio/ama-metrics-prometheus-config.yaml
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to apply the Istio metrics scrape configuration."
        }
    }
}

if (($env:DEPLOY_OBSERVABILITY_TOOLS -ceq "true") -and ($env:DEPLOY_NODE_AUTO_PROVISIONING -ceq "true")) {
    & python3.11 azd-hooks/merge-ama-metrics-settings.py --manifest sample-manifests/observability/ama-metrics-settings-configmap.yaml
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to merge the AMA node-auto-provisioning target."
    }
}

############################################
# Delete custom-values.yaml
############################################
Remove-Item -Path custom-values.yaml -ErrorAction SilentlyContinue