# Kubernetes executor: RBAC and Pod Security

`deploy/k8s` (applied with `kubectl apply -k deploy/k8s`) gives the Kubernetes executor its own
least-privilege identity and makes the API server reject pods that are not locked down.

## What is enforced

| Object | Setting |
|---|---|
| Namespace `claimcheck` | `pod-security.kubernetes.io/enforce`, `audit` and `warn` = `restricted` (version `latest`) |
| ServiceAccount `claimcheck-executor` | `automountServiceAccountToken: false` |
| Role + RoleBinding `claimcheck-executor` | namespaced to `claimcheck`, bound only to that ServiceAccount |

The Role grants exactly what the executor's kubectl calls (`claimcheck/k8s.py`) need:

| kubectl call | Permissions |
|---|---|
| `apply -f -` (ConfigMap, NetworkPolicy, Job) | `get`, `create` on configmaps, networkpolicies, jobs |
| `get pods -l <run> -o json` | `list` pods |
| `logs job/<name>` | `get` jobs, `list` pods, `get` pods/log |
| `delete jobs,configmaps,networkpolicies -l <run> --wait=true` | `list`, `delete`, `watch` on the three |

Not granted: Secrets, creating Pods directly, `pods/exec`, `attach`, `portforward`,
`serviceaccounts/token`, Roles/RoleBindings, `patch`, `update`, `deletecollection`, any other namespace, and
anything cluster-wide. The repro Job's pods are created by the Job controller, so the executor never
needs `create pods`.

## Running as the ServiceAccount

`KubernetesExecutor(impersonate="system:serviceaccount:claimcheck:claimcheck-executor")` adds
`--as <user>` to every kubectl call (network probe, apply, poll, logs, cleanup). The caller's own
context must be allowed to impersonate; kind's admin is. Without impersonation, give the executor a
kubeconfig with a token for the ServiceAccount:

```bash
kubectl -n claimcheck create token claimcheck-executor --duration=1h
```

If the executor itself runs in a pod as this ServiceAccount, that pod has to opt in with
`automountServiceAccountToken: true`, because the ServiceAccount turns it off by default. The CLI
has no flag for `impersonate` yet; it is available from Python.

## What was tested

`unit_tests/integration/test_k8s_rbac.py`, on a kind cluster (Kubernetes v1.36, Calico), with
`CLAIMCHECK_K8S_CONTEXT=kind-claimcheck`. The CI `kubernetes` job applies `deploy/k8s` and then
runs it with the other integration tests (this was written before the first CI run of it).

- A full run as the ServiceAccount in `claimcheck` (network probe, then the repro) is confirmed,
  reports `egress blocked, verified`, and leaves no Job, ConfigMap or NetworkPolicy behind.
- The same executor as the ServiceAccount in `default` is refused by RBAC (`forbidden`).
- `kubectl auth can-i --as` answers yes for each permission above, and no for Secrets
  (get/list/watch/create), creating/getting/deleting pods, `pods/exec`, `attach`, `portforward`,
  `serviceaccounts/token`, creating Roles/RoleBindings, `patch`/`update`, `deletecollection`, any of
  those resources in `default`, `kube-system` and `claimcheck-it`, and cluster-wide
  namespaces/nodes/ClusterRoleBindings.
- A privileged pod, and a pod with default settings (root, capabilities kept), applied with
  `--dry-run=server` as the admin are rejected with `violates PodSecurity "restricted:latest"`, so it
  is admission, not RBAC, that stops them.
- The executor's own Job is accepted under `restricted` with no PodSecurity warning, and its pod
  template is accepted as a Pod.
- A privileged Job created by the ServiceAccount is accepted (Pod Security only warns about Jobs),
  but the Job controller is refused the pod (`FailedCreate ... violates PodSecurity`) and no pod
  exists.
- The Role, RoleBinding and ServiceAccount the cluster really has contain no `*`, no Secrets, no
  exec/attach/port-forward, no `patch`/`update`, and the token is not automounted.
- Each permission in the Role was removed in turn and a full run repeated, to check it is needed
  (a one-off check, not part of the test suite); see "Why each permission is there" below.

`unit_tests/test_k8s.py` checks the manifest files too, without a cluster: no `*`, no Secrets, no
exec/attach/port-forward, no RBAC or token resources, no ClusterRole, and exactly the rule set
above. Those tests need PyYAML and skip without it (CI's unit job does not install it, so there the
cluster tests are the check).

## Why each permission is there

Checked on kind (v1.36) by removing one verb at a time from the Role and repeating a full run as the
ServiceAccount (probe, then repro), then checking for leftover objects:

| Removed | Result |
|---|---|
| `get` or `create` on configmaps, networkpolicies or jobs | the probe cannot be created, so the run is refused |
| `list` pods, `get` pods/log | the probe's state or output cannot be read, so the run is refused |
| `list` or `delete` on any of the three | the run works but its objects are left behind |
| `watch` on any of the three | the run works when deletion is instant, but `delete --wait=true` cannot watch an object that is still terminating (tested with a finalizer: `cannot watch resource "configmaps"`) |
| `patch` on any of the three | no effect, so it is **not granted** |

`patch` is only used by `kubectl apply` when the object already exists. Every run uses fresh
names, so apply always creates. If you change the executor to re-apply objects, the Role must gain
`patch`.

## What is not enforced or not tested

- **gVisor is untested.** `runtime_class` is passed to the Job, but nothing checks that the
  cluster has that RuntimeClass or that it is really gVisor.
- **Creating Jobs is powerful.** A Job can run any pod spec that `restricted` allows, so an identity
  that can create Jobs in `claimcheck` can mount any ConfigMap or Secret in that namespace and read it
  through the pod's logs, or run a pod as `claimcheck-executor` with its token mounted. Keep no
  Secrets in `claimcheck`, and treat the executor's credentials as able to run code there. The
  untrusted repro itself gets no token and no network.
- **The executor can create NetworkPolicies.** NetworkPolicies are additive, so whoever holds the
  executor's identity could add one that allows egress. The probe pod checks the deny-all policy
  before the first run; it does not watch for later policies.
- If RBAC refuses the probe pod (wrong namespace, missing Role), the report says the probe "could
  not run" under the NetworkPolicy message; it does not name RBAC as the cause.
- No ResourceQuota or LimitRange is installed, so the namespace has no cap on the number of Jobs.
  Each repro pod has its own CPU and memory limits and a deadline.
- Pod Security `latest` follows the cluster's version; pin `enforce-version` if you need the policy
  to stay fixed across upgrades.
- Only tested on kind with Calico. Managed clusters may add their own admission policies or
  ClusterRoleBindings for `system:serviceaccounts` that widen what the ServiceAccount can do; check
  with `kubectl auth can-i --list --as system:serviceaccount:claimcheck:claimcheck-executor -n claimcheck`.
