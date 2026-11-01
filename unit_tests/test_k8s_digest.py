"""Digest pinning is reported without changing repro verdicts; strict mode refuses before kubectl."""

import json

import pytest

from claimcheck import cli, k8s
from claimcheck.models import Status
from test_k8s import FakeKubectl


DIGEST = "0123456789abcdef" * 4
PINNED = [f"python@sha256:{DIGEST}", f"python:3.12-slim@sha256:{DIGEST}",
          f"registry.example:5000/team/python:3.12-slim@sha256:{DIGEST}"]
UNPINNED = ["python:3.12-slim", "python", "registry.example:5000/team/python:latest"]
MALFORMED = ["", "python@sha256:", "python@sha256:abc", f"python@sha256:{'g' * 64}",
             f"python@sha256:{DIGEST}0", f"python@sha256:{DIGEST[:-1]}",
             f"@sha256:{DIGEST}", f"python@@sha256:{DIGEST}",
             f"python@sha256:{DIGEST}:latest", f"python@sha256:{DIGEST}\n",
             f"python image@sha256:{DIGEST}", f"python@sha512:{DIGEST}"]
OUTCOMES = [(1, "RuntimeError: boom", Status.CONFIRMED), (0, "ok", Status.REFUTED),
            (127, "command not found: boom", Status.UNVERIFIED)]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "poc.py").write_text("raise SystemExit('boom')\n")
    return root


@pytest.mark.parametrize("image", PINNED)
@pytest.mark.parametrize("exit_code,logs,status", OUTCOMES)
def test_pinned_image_is_named_in_source_without_a_warning(repo, image, exit_code, logs, status):
    fake = FakeKubectl(exit_code=exit_code, logs=logs)
    evidence = k8s.KubernetesExecutor(image=image, run=fake, poll_interval=0,
                                     require_network_isolation=False).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is status
    assert image in evidence.source and "image pinned by digest" in evidence.source
    assert "image not pinned by digest" not in evidence.detail
    job = next(obj for obj in json.loads(fake.calls[0][1])["items"] if obj["kind"] == "Job")
    assert job["spec"]["template"]["spec"]["containers"][0]["image"] == image


@pytest.mark.parametrize("image", UNPINNED)
@pytest.mark.parametrize("exit_code,logs,status", OUTCOMES)
def test_unpinned_image_adds_a_note_without_changing_the_verdict(repo, image, exit_code, logs, status):
    fake = FakeKubectl(exit_code=exit_code, logs=logs)
    evidence = k8s.KubernetesExecutor(image=image, run=fake, poll_interval=0,
                                     require_network_isolation=False).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is status
    assert image in evidence.source and "image not pinned by digest" in evidence.source
    assert "image not pinned by digest" in evidence.detail
    assert fake.verbs()[0] == "apply" and fake.verbs()[-1] == "delete"


@pytest.mark.parametrize("image", UNPINNED + MALFORMED)
def test_require_digest_refuses_before_the_network_probe_or_repo_copy(repo, monkeypatch, image):
    fake = FakeKubectl()
    executor = k8s.KubernetesExecutor(image=image, require_digest=True, run=fake, poll_interval=0)

    def unexpected(*args):
        pytest.fail("an unpinned image must be refused before any setup")

    monkeypatch.setattr(executor, "network_isolated", unexpected)
    monkeypatch.setattr(k8s, "pack_repo", unexpected)
    evidence = executor.run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "refused to run" in evidence.detail and "image not pinned by digest" in evidence.detail
    assert "image not pinned by digest" in evidence.source
    assert "egress not verified" in evidence.source
    assert fake.calls == []


@pytest.mark.parametrize("image", PINNED)
def test_require_digest_allows_pinned_probe_and_repro(repo, image):
    fake = FakeKubectl(logs=[k8s.PROBE_OK, "RuntimeError: boom"])
    evidence = k8s.KubernetesExecutor(image=image, require_digest=True, run=fake,
                                     poll_interval=0).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.CONFIRMED
    assert "image pinned by digest" in evidence.source and "egress blocked, verified" in evidence.source
    assert fake.verbs().count("apply") == 2
    for args, data in fake.calls:
        if args[1] == "apply":
            job = next(obj for obj in json.loads(data)["items"] if obj["kind"] == "Job")
            assert job["spec"]["template"]["spec"]["containers"][0]["image"] == image


@pytest.mark.parametrize("image", MALFORMED)
def test_malformed_reference_is_not_reported_as_pinned_or_as_a_reproduction(repo, image):
    fake = FakeKubectl(pod_state={"waiting": {"reason": "InvalidImageName", "message": "invalid reference"}})
    evidence = k8s.KubernetesExecutor(image=image, run=fake, poll_interval=0,
                                     require_network_isolation=False).run_repro(repo, "python3 poc.py", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "InvalidImageName" in evidence.detail and "image not pinned by digest" in evidence.detail
    assert "image not pinned by digest" in evidence.source
    assert fake.verbs()[-1] == "delete"


@pytest.mark.parametrize("failure", ["network", "apply", "rbac", "archive"])
def test_unpinned_note_is_kept_on_setup_failures(repo, monkeypatch, failure):
    forbidden = ('pods is forbidden: User "test-user" cannot list resource "pods" '
                 'in API group "" in the namespace "claimcheck"')
    fake = FakeKubectl(logs="CONNECTED", apply_fails=failure == "apply",
                       forbid={"get": forbidden} if failure == "rbac" else None)
    if failure == "archive":
        monkeypatch.setattr(k8s, "MAX_REPO_BYTES", 1)
    evidence = k8s.KubernetesExecutor(run=fake, poll_interval=0,
                                     require_network_isolation=failure == "network").run_repro(repo, "true", "boom")
    assert evidence.status is Status.UNVERIFIED
    assert "image not pinned by digest" in evidence.detail
    assert "image not pinned by digest" in evidence.source


@pytest.mark.parametrize("image,strict,code,status", [
    (UNPINNED[0], False, 0, "confirmed"), (PINNED[0], True, 0, "confirmed"),
    (UNPINNED[0], True, 2, "unverified"), (MALFORMED[2], True, 2, "unverified"),
])
def test_cli_require_digest_reaches_the_executor(repo, tmp_path, monkeypatch, capsys, image, strict, code, status):
    report = tmp_path / "report.txt"
    report.write_text("The command reproduces the failure")
    fake = FakeKubectl(logs=[k8s.PROBE_OK, "RuntimeError: boom"])
    real = k8s.KubernetesExecutor
    monkeypatch.setattr(k8s, "KubernetesExecutor", lambda **kw: real(**kw, run=fake, poll_interval=0))
    flags = ["--k8s-require-digest"] if strict else []
    assert cli.main(["check", "--repo", str(repo), "--report", str(report), "--no-model", "--json",
                     "--executor", "kubernetes", "--k8s-image", image, "--repro", "python3 poc.py",
                     "--expect", "boom", *flags]) == code
    evidence = json.loads(capsys.readouterr().out)["evidence"][-1]
    assert evidence["status"] == status
    if status == "unverified":
        assert "image not pinned by digest" in evidence["detail"]
        assert fake.calls == []
    else:
        assert fake.verbs().count("apply") == 2


def test_require_digest_flag_is_optional_and_in_help():
    parser = cli.build_parser()
    args = parser.parse_args(["check", "--repo", ".", "--report", "report.txt"])
    assert args.k8s_require_digest is False
    help_text = parser._subparsers._group_actions[0].choices["check"].format_help()
    assert "--k8s-require-digest" in help_text
