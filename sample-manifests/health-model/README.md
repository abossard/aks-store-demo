# Health model live sample

This directory is the canonical environment-neutral sample consumed directly
by the `ahm-for-k8s` live harness. It is product content, not a fixture,
snapshot, or simulated Kubernetes response.

Render exactly these runtime tokens before applying the YAML to one new
operator-owned namespace:

- `__STORAGE_CLASS__`: an existing dynamic `Delete`-reclaim StorageClass.
- `__GATEWAY_CLASS__`: an accepted Istio GatewayClass.
- `__DNS_PROBE_NAME__`: a runtime-supplied public FQDN.

Apply `workload.yaml`, `gateway-api.yaml`, the conditional
`cilium-fqdn-policy.yaml`, and `traffic.yaml` with real `kubectl --namespace`.
The Cilium policy is valid only when the cluster advertises the Cilium policy
API and ACNS security. The bounded Jobs use
`ghcr.io/azure-samples/aks-store-demo/store-front:2.2.0`,
`busybox:1.37.0`, and the official `agnhost:2.43` multi-architecture digest.

Delete the exact rendered files, then delete the owned namespace and wait for
its dynamically provisioned volume to disappear. Never commit rendered files,
cluster identifiers, endpoints, credentials, or runtime token values.

Official sources:

- https://learn.microsoft.com/azure/aks/managed-gateway-api
- https://learn.microsoft.com/azure/aks/istio-gateway-api
- https://learn.microsoft.com/azure/aks/how-to-apply-fqdn-filtering-policies
- https://learn.microsoft.com/azure/aks/container-network-observability-metrics
