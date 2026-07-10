#!/usr/bin/env bash
set -euo pipefail

NODE_COUNT="${1:-}"
NAMESPACE="${NAMESPACE:-pms}"

if [[ -z "$NODE_COUNT" || ! "$NODE_COUNT" =~ ^[135]$ ]]; then
  echo "Usage: $0 <1|3|5>" >&2
  exit 1
fi

if (( NODE_COUNT > 1 )); then
  DISPATCHER_REPLICAS=$((NODE_COUNT - 1))
else
  DISPATCHER_REPLICAS=1
fi

kubectl -n "$NAMESPACE" scale deployment/api --replicas="$NODE_COUNT"
kubectl -n "$NAMESPACE" scale deployment/worker --replicas="$NODE_COUNT"
kubectl -n "$NAMESPACE" scale deployment/callback-dispatcher --replicas="$DISPATCHER_REPLICAS"
kubectl -n "$NAMESPACE" rollout restart deployment/worker
kubectl -n "$NAMESPACE" rollout status deployment/api --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/worker --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/callback-dispatcher --timeout=180s

echo "Scaled api/worker to ${NODE_COUNT} and callback-dispatcher to ${DISPATCHER_REPLICAS}."
