#!/usr/bin/env pwsh

if ($env:DEPLOY_ISTIO -ceq "true") {
    & kubectl apply -f sample-manifests/istio/gateway.yaml
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply the Istio gateway route."
    }

    & kubectl delete gateway.networking.istio.io/store-front-gateway-external -n pets --ignore-not-found=true
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to delete the legacy Istio Gateway."
    }

    & kubectl delete virtualservice.networking.istio.io/store-front-vs-external -n pets --ignore-not-found=true
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to delete the legacy Istio VirtualService."
    }

    if ($env:DEPLOY_OBSERVABILITY_TOOLS -ceq "true") {
        & kubectl apply -f sample-manifests/istio/ama-metrics-prometheus-config.yaml
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to apply the Istio metrics scrape configuration."
        }
    }
}

############################################
# Delete custom-values.yaml
############################################
Remove-Item -Path custom-values.yaml -ErrorAction SilentlyContinue