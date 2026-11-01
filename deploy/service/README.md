# deploy/service: the claimcheck API and worker on Kubernetes

Kustomize manifests for the long-running service: the HTTP API (`python -m claimcheck.server`) and the webhook worker (`python -m claimcheck.server.worker`). This is separate from [`deploy/k8s`](../k8s), which sets up the `claimcheck` namespace where the Kubernetes executor runs repro Jobs.

| Object | Name | Purpose |
| --- | --- | --- |
| Namespace | `claimcheck-service` | Pod Security `restricted` (enforce, audit, warn) |
| Deployment | `claimcheck-api` | one pod: container `api` and container `worker` |
| Service | `claimcheck-api` | ClusterIP, port 8080 |
| PersistentVolumeClaim | `claimcheck-data` | ReadWriteOnce, holds `/data/claimcheck.db` |
| ConfigMap | `claimcheck-repos` | the worker's `--repos` map (empty by default; [`deploy/triage`](../triage) maps this repo) |

The Secret `claimcheck-secrets` is **not** in the manifests. Create it yourself.

## Install

```sh
kubectl apply -f deploy/service/namespace.yaml
kubectl -n claimcheck-service create secret generic claimcheck-secrets \
  --from-literal=CLAIMCHECK_API_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=CLAIMCHECK_WEBHOOK_SECRET="<the secret configured on the GitHub webhook>"
kubectl apply -k deploy/service
```

Both keys must exist, or the `api` container won't start (`CreateContainerConfigError`). `CLAIMCHECK_WEBHOOK_SECRET` is the HMAC key for `X-Hub-Signature-256`. Without it the server answers every webhook with 503. `CLAIMCHECK_API_TOKEN` is required: the server refuses to bind `0.0.0.0` without it, and with it every `/v1/investigations*` and dashboard request needs `Authorization: Bearer <token>` (the signed webhook keeps its own HMAC check). The Service is still ClusterIP only; put an ingress with TLS in front of it before exposing it, and restrict who can reach it with a NetworkPolicy if your cluster enforces them.

## Image

`ghcr.io/kartikeyg0104/claimcheck:latest` is a placeholder name. Build the [Dockerfile](../../Dockerfile), push it, and point an overlay at it, preferably by digest:

```yaml
# my-overlay/kustomization.yaml
resources:
  - ../deploy/service   # relative: kustomize refuses absolute paths
images:
  - name: ghcr.io/kartikeyg0104/claimcheck
    newName: registry.example/claimcheck
    digest: sha256:...
```

The image's ENTRYPOINT is the `claimcheck` CLI, so both containers set `command: ["python"]` and pass `-m ...` as args.

## SQLite, the ReadWriteOnce PVC, and why it is one pod

The API and the worker share one SQLite file. SQLite locking works only between processes that see the same filesystem on one host, and a ReadWriteOnce volume can be mounted by only one node. Two Deployments would need pod affinity to land on the same node. Affinity is only checked at scheduling time, so a rescheduled API pod could move to another node while the worker still holds the volume, and then stay stuck on a Multi-Attach error. Two containers in one pod rule that out with no scheduling rules: they always share a node and a mount. The cost is that they scale and restart together, which is no loss here, since SQLite rules out scaling across nodes anyway. For the same reason the Deployment has `replicas: 1` and `strategy: Recreate`: a rolling update would briefly run two pods, possibly on two nodes. Scaling out means replacing SQLite, not adding replicas.

## Repositories

Both processes only read checkouts under `--repo-root /repos`, mounted read-only. In the base, `/repos` is an empty placeholder volume, and the worker's map (`claimcheck-repos`, `repos.json`) is `{}`, so every webhook event is recorded and then `skipped`. To check real repos, patch the `repos` volume to something holding the checkouts (a PVC, or an image volume) and fill in the map, e.g. `{"kartikeyg0104/triage": "triage"}` for `/repos/triage`. The worker refuses to start if a mapped path is not a directory under `/repos`.

[`deploy/triage`](../triage) does exactly that for this repository: it maps `kartikeyg0104/triage` to `/repos/triage` and adds a `checkout` init container (same image, same locked-down security context) that shallow-clones it from GitHub into the `repos` emptyDir before the API and the worker start. Both still mount `/repos` read-only.

```sh
kubectl apply -k deploy/triage
kubectl -n claimcheck-service rollout restart deploy/claimcheck-api   # re-clone to pick up newer commits
```

The clone needs egress to github.com from the pod. `unit_tests/test_service_triage_overlay.py` and `unit_tests/integration/test_service_triage_cluster.py` cover it.

## Security settings

The settings below apply to every container, and `unit_tests/test_service_manifests.py` checks each one:

- pod: `automountServiceAccountToken: false` (neither process calls the Kubernetes API), `fsGroup: 10001` so the PVC is writable, no host namespaces
- container: `runAsNonRoot`, `runAsUser: 10001` (the image's user), `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`
- writable paths are only `/data` (PVC) and `/tmp` (emptyDir, 256Mi). The image sets `HOME` and `TMPDIR` to `/tmp`.
- CPU and memory requests and limits are set on both containers.

## Probes

The server has no unauthenticated health endpoint, so the `api` probes are TCP checks on port 8080. They prove the server is listening, not that requests succeed. The `worker` listens on no port, so its probes run a short Python one-liner that opens the database and queries the `investigations` table. That fails if the volume or the file is unusable.

## Tests

- `pytest unit_tests/test_service_manifests.py` renders `kubectl kustomize deploy/service` and checks the fields above. It skips without kubectl or PyYAML.
- `CLAIMCHECK_K8S_CONTEXT=kind-claimcheck pytest unit_tests/integration/test_service_manifests_cluster.py` creates only the Namespace, runs `kubectl apply --dry-run=server -k deploy/service`, and also dry-runs the pod template as a Pod. That matters because `enforce` applies to Pods, while for Deployments the API server only warns. It also checks that a variant with privilege escalation is refused, then deletes the namespace. No pod is started, so the image need not be in the cluster. `deploy/kind/setup.sh` creates the kind cluster.
