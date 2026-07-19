#!/usr/bin/env pwsh

if ($env:DEPLOY_ISTIO -ceq "true") {
    & kubectl apply -f sample-manifests/istio/gateway.yaml
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply the Istio gateway route."
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