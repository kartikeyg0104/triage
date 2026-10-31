# Security policy

claimcheck runs commands from bug reports and reads text from pull requests and webhooks, so isolation and the handling of untrusted input are the core of the project. We want to hear about anything that breaks them.

## Supported versions

Only `main` is supported. There are no releases yet; fixes land on `main`.

## Reporting a vulnerability

Report privately through GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue, PR or discussion for a vulnerability.

Please include the revision you tested, the executor (local sandbox or Kubernetes) and OS or cluster setup, the steps or a proof of concept, and what an attacker gains. We will acknowledge the report, work on a fix in a private advisory, and credit you when it is published unless you ask us not to.

## In scope

- **Local sandbox** (`claimcheck/sandbox.py`): a repro that reaches the network, reads or writes outside its throwaway copy of the repo, sees the caller's environment or secrets, or runs when isolation could not be set up.
- **Kubernetes executor** (`claimcheck/k8s.py`, `deploy/k8s`): a repro pod that gets egress, a service account token, privileges or capabilities beyond the restricted profile, or access to other namespaces; the egress probe reporting `egress blocked, verified` when egress is open; the RBAC in `deploy/k8s` granting more than the executor needs. See [docs/KUBERNETES_SECURITY.md](docs/KUBERNETES_SECURITY.md).
- **Webhook service and worker** (`claimcheck/server`): accepting an event without a valid `X-Hub-Signature-256`, accepting anything when no secret is set, reading files outside `--repo-root`, running a command or repro from request or event text, or bypassing the body size limit. See [docs/SERVER.md](docs/SERVER.md).
- **GitHub Action** (`action.yml`, `claimcheck/action.py`): PR titles and bodies are untrusted. PR text reaching a shell, a repro command from PR text being run, or the comment being used to leak data or workflow secrets are all in scope.
- **Fix verification** (`claimcheck/revisions.py`): a check that modifies the user's checkout or escapes the temporary worktrees.

## Out of scope and known limits

These are documented limits, not vulnerabilities. Reports that show they are worse than documented are welcome.

- **The local sandbox is process-level isolation** (`sandbox-exec` on macOS, `unshare -rn` on Linux), not a container, VM or gVisor. Use the Kubernetes executor for untrusted repros.
- **gVisor is untested.** `--k8s-runtime-class` is passed through to the pod, but no runtime class has been tested.
- **NetworkPolicy depends on the CNI.** On a cluster whose CNI does not enforce NetworkPolicy, claimcheck refuses to run the repro; that refusal is the intended behaviour.
- **The investigations API is unauthenticated** and binds to `127.0.0.1` by default. Exposing it beyond localhost without an authenticating proxy is a deployment choice, not a vulnerability.
- **No GitHub App authentication** (installation tokens, JWT) exists yet, and results are not posted back from the service.
- Wrong verdicts (for example a missed claim from the 1B model, or a JS definition the pattern match does not see) are correctness bugs: open a normal issue unless they can be used to make claimcheck run something it should not.
- Vulnerabilities in dependencies, Ollama, kind, Calico or Kubernetes itself should go to those projects.
