#!/bin/sh

set -eu

if [ "${DEPLOY_ISTIO:-false}" = "true" ]; then
  kubectl apply -f sample-manifests/istio/gateway.yaml
  kubectl delete gateway.networking.istio.io/store-front-gateway-external -n pets --ignore-not-found=true
  kubectl delete virtualservice.networking.istio.io/store-front-vs-external -n pets --ignore-not-found=true

  if [ "${DEPLOY_OBSERVABILITY_TOOLS:-false}" = "true" ]; then
    kubectl apply -f sample-manifests/istio/ama-metrics-prometheus-config.yaml
  fi
fi

############################################
# Delete custom-values.yaml
############################################
rm -f custom-values.yaml