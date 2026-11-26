#!/usr/bin/env bash
# The lab `themis bench-real` runs in: a kind cluster with Prometheus, Loki and Tempo, and Argus's demo shop deployed
# from a GitOps repository on disk. Built for a throwaway CI runner (.github/workflows/bench-real.yml): it creates a
# cluster, installs charts, and leaves port-forwards running in the background.
#
#   scripts/bench_real_lab.sh <argus-sre-agent checkout> <gitops dir to create>
#
# Afterwards: Prometheus on 127.0.0.1:9090, Loki on :3100, Tempo on :3200, and <gitops dir> is a git repository
# whose "baseline" tag is what the shop runs. Needs docker, kind, kubectl and helm.
set -euo pipefail

argus="$(cd "$1" && pwd)"
gitops="$2"
cluster=themis-bench
logs="${BENCH_LOGS:-/tmp/bench-real-lab}"
mkdir -p "$logs"

step() { printf '\n== %s (%ss)\n' "$1" "$SECONDS"; }

step "kind cluster"
kind get clusters | grep -qx "$cluster" || kind create cluster --name "$cluster" --config "$argus/deploy/kind/cluster.yaml" --wait 180s

step "shop images, built while the charts install"
(
  docker build -q -t argus-demo/shop:1.3.2 "$argus/demo/shop"
  docker build -q -t argus-demo/shop:1.4.0 --build-arg RELEASE_BUG=1 "$argus/demo/shop"
  kind load docker-image argus-demo/shop:1.3.2 argus-demo/shop:1.4.0 --name "$cluster"
) >"$logs/images.log" 2>&1 &
images=$!

step "observability charts"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts >/dev/null
helm repo update >/dev/null
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Grafana is for people; the agent reads Prometheus, Loki and Tempo directly, so it is left out to save a minute.
helm upgrade --install kps prometheus-community/kube-prometheus-stack -n monitoring \
  -f "$argus/deploy/helm/kube-prometheus-stack.yaml" --set grafana.enabled=false --wait --timeout 12m >"$logs/kps.log" 2>&1 &
kps=$!
helm upgrade --install loki grafana/loki -n monitoring -f "$argus/deploy/helm/loki.yaml" --wait --timeout 12m >"$logs/loki.log" 2>&1 &
loki=$!
helm upgrade --install tempo grafana/tempo -n monitoring -f "$argus/deploy/helm/tempo.yaml" --wait --timeout 12m >"$logs/tempo.log" 2>&1 &
tempo=$!
helm upgrade --install otel open-telemetry/opentelemetry-collector -n monitoring \
  -f "$argus/deploy/helm/otel-collector.yaml" --wait --timeout 12m >"$logs/otel.log" 2>&1 &
otel=$!

failed=0
for job in images kps loki tempo otel; do
  if ! wait "${!job}"; then
    echo "!! $job failed:"
    tail -n 40 "$logs/$job.log"
    failed=1
  fi
done
[ "$failed" = 0 ] || exit 1

step "GitOps repository"
rm -rf "$gitops"
cp -r "$argus/gitops" "$gitops"
git -C "$gitops" init -q -b main
git -C "$gitops" add -A
git -C "$gitops" -c user.name=platform -c user.email=platform@shop.local commit -q -m "shop: every service on 1.3.2"
git -C "$gitops" tag baseline

step "the shop, its alerts and its load"
kubectl apply -k "$gitops"
kubectl apply -f "$argus/deploy/k8s/shop-monitoring.yaml"
for d in frontend checkout payments loadgen; do kubectl -n shop rollout status "deploy/$d" --timeout=240s; done

step "port-forwards"
forward() { # kept alive: kubectl port-forward exits when its pod restarts
  (while true; do kubectl -n monitoring port-forward "svc/$1" "$2:$3" >>"$logs/forward-$1.log" 2>&1; sleep 1; done) &
}
forward kps-prometheus 9090 9090
forward loki 3100 3100
forward tempo 3200 3200

step "Prometheus scraping the shop and holding its alert rules"
for _ in $(seq 120); do
  up=$(curl -sf --get 'http://127.0.0.1:9090/api/v1/query' --data-urlencode 'query=count(up{namespace="shop"} == 1)' |
    jq -r '.data.result[0].value[1] // 0' 2>/dev/null || echo 0)
  rules=$(curl -sf 'http://127.0.0.1:9090/api/v1/rules' | jq '[.data.groups[].rules[] | select(.name == "HighErrorRate")] | length' 2>/dev/null || echo 0)
  if [ "${up:-0}" -ge 8 ] && [ "${rules:-0}" -ge 1 ]; then
    echo "scraping $up shop pods; alert rules loaded"
    break
  fi
  sleep 5
done
[ "${up:-0}" -ge 8 ] || { echo "!! Prometheus is not scraping the shop (up=$up)"; exit 1; }

step "lab ready"
kubectl get pods -A -o wide
