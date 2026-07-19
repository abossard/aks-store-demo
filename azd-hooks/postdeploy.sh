#!/bin/sh

set -eu

if [ "${DEPLOY_ISTIO:-false}" = "true" ]; then
  kubectl wait \
    --for=condition=Accepted \
    gatewayclass/istio \
    --timeout=10m
  kubectl apply -f sample-manifests/istio/gateway-api.yaml

  if [ "${DEPLOY_OBSERVABILITY_TOOLS:-false}" = "true" ]; then
    kubectl apply -f sample-manifests/istio/ama-metrics-prometheus-config.yaml
  fi
fi

if [ "${DEPLOY_OBSERVABILITY_TOOLS:-false}" = "true" ] &&
  [ "${DEPLOY_NODE_AUTO_PROVISIONING:-false}" = "true" ]; then
  python3.11 azd-hooks/merge-ama-metrics-settings.py \
    --manifest sample-manifests/observability/ama-metrics-settings-configmap.yaml
fi

############################################
# Delete custom-values.yaml
############################################
rm -f custom-values.yaml