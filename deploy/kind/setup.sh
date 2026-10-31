#!/bin/sh
# Create a local kind cluster for claimcheck with Calico, so NetworkPolicy is enforced.
# Usage: deploy/kind/setup.sh [cluster-name]   (default: claimcheck)
set -eu
NAME="${1:-claimcheck}"
CALICO_VERSION="v3.29.1"
HERE="$(cd "$(dirname "$0")" && pwd)"

kind create cluster --name "$NAME" --config "$HERE/cluster.yaml" --wait 0s
kubectl --context "kind-$NAME" apply -f "https://raw.githubusercontent.com/projectcalico/calico/$CALICO_VERSION/manifests/calico.yaml"
kubectl --context "kind-$NAME" -n kube-system rollout status daemonset/calico-node --timeout=300s
kubectl --context "kind-$NAME" -n kube-system rollout status deployment/calico-kube-controllers --timeout=300s
kubectl --context "kind-$NAME" wait --for=condition=Ready nodes --all --timeout=300s
kubectl --context "kind-$NAME" create namespace claimcheck --dry-run=client -o yaml | kubectl --context "kind-$NAME" apply -f -
echo "ready: kubectl context kind-$NAME"
