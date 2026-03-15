#!/bin/bash
# k3s-nettest: test pod-to-service-IP connectivity on every cluster node
#
# Runs a busybox pod pinned to each node and attempts to reach the kubernetes
# ClusterIP (10.43.0.1). Reports PASS/FAIL per node and exits non-zero if any
# node fails.
#
# Usage: k3s-nettest [--namespace NS] [--timeout SEC] [--image IMAGE]
#
# Requires: kubectl in PATH, kubeconfig configured

NAMESPACE="kube-system"
TIMEOUT=5
IMAGE="busybox"
LABEL="k3s-nettest"
EXIT=0

while [[ $# -gt 0 ]]; do
  case $1 in
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --timeout)   TIMEOUT="$2";   shift 2 ;;
    --image)     IMAGE="$2";     shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# Get kubernetes service ClusterIP (authoritative, not hardcoded)
SVC_IP=$(kubectl get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}' 2>/dev/null)
if [[ -z "$SVC_IP" ]]; then
  echo "ERROR: could not determine kubernetes service ClusterIP" >&2
  exit 1
fi

NODES=$(kubectl get nodes --no-headers -o custom-columns=NAME:.metadata.name 2>/dev/null)
if [[ -z "$NODES" ]]; then
  echo "ERROR: could not list nodes" >&2
  exit 1
fi

echo "Testing pod-to-service connectivity (${SVC_IP}:443) on each node"
echo "Namespace: ${NAMESPACE}  Timeout: ${TIMEOUT}s  Image: ${IMAGE}"
echo ""

PODS=()

# Create all pods first
for NODE in $NODES; do
  POD="nettest-${NODE}"
  PODS+=("$POD")
  kubectl run "$POD" -n "$NAMESPACE" \
    --image="$IMAGE" \
    --restart=Never \
    --labels="app=${LABEL}" \
    --overrides="{\"spec\":{\"nodeName\":\"${NODE}\",\"tolerations\":[{\"operator\":\"Exists\"}]}}" \
    -- sleep 30 >/dev/null 2>&1
done

# Wait for all pods to be Running
echo -n "Waiting for pods"
for i in $(seq 1 30); do
  RUNNING=$(kubectl get pods -n "$NAMESPACE" -l "app=${LABEL}" --no-headers 2>/dev/null \
    | grep -c Running)
  TOTAL=${#PODS[@]}
  if [[ "$RUNNING" -eq "$TOTAL" ]]; then
    break
  fi
  echo -n "."
  sleep 1
done
echo ""
echo ""

# Test each node
for NODE in $NODES; do
  POD="nettest-${NODE}"
  RESULT=$(kubectl exec -n "$NAMESPACE" "$POD" -- \
    wget -T"$TIMEOUT" -qO- --no-check-certificate "https://${SVC_IP}/version" 2>&1)
  STATUS=$?

  # wget exits 1 on HTTP error (401) but that means connectivity works
  if echo "$RESULT" | grep -q "401 Unauthorized"; then
    printf "  %-12s  PASS  (401 Unauthorized — API server reachable)\n" "$NODE"
  elif [[ $STATUS -eq 0 ]]; then
    printf "  %-12s  PASS  (200 OK)\n" "$NODE"
  else
    printf "  %-12s  FAIL  (%s)\n" "$NODE" "$(echo "$RESULT" | tr -d '\n')"
    EXIT=1
  fi
done

echo ""

# Cleanup
echo -n "Cleaning up."
kubectl delete pods -n "$NAMESPACE" -l "app=${LABEL}" >/dev/null 2>&1

exit $EXIT
