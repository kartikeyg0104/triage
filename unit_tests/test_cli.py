import json

import pytest

from claimcheck import cli, extract, report
from claimcheck.models import Claim, Evidence, Status, Verdict


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "urls.py").write_text("def parse_url(s):\n    return s\n")
    return tmp_path


def write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_supported_exits_0(repo, tmp_path, capsys):
    rep = write(tmp_path, "r.txt", "Crash in `parse_url()` on long input")
    code = cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model"])
    assert code == 0
    assert "**Verdict: supported**" in capsys.readouterr().out


def test_missing_symbol_exits_1(repo, tmp_path, capsys):
    rep = write(tmp_path, "r.txt", "RCE in parse_url_v2()")
    code = cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model"])
    out = capsys.readouterr().out
    assert code == 1
    assert "does not appear in any of" in out
    assert "regex fallback" in out


def test_no_claims_exits_2(repo, tmp_path):
    rep = write(tmp_path, "r.txt", "it is slow")
    assert cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model"]) == 2


def test_diff_without_tests_is_flagged(repo, tmp_path, capsys):
    rep = write(tmp_path, "r.txt", "Fixes `parse_url()`")
    diff = write(tmp_path, "d.patch", "--- a/urls.py\n+++ b/urls.py\n")
    code = cli.main(["check", "--repo", str(repo), "--report", rep, "--diff", diff, "--no-model"])
    assert code == 2  # unverified: no test change does not prove the change is untested
    assert "existing tests may still cover it" in capsys.readouterr().out


def test_json_output(repo, tmp_path, capsys):
    rep = write(tmp_path, "r.txt", "RCE in parse_url_v2()")
    cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["verdict"] == "refuted"
    assert data["extraction_model"] is None
    assert data["evidence"][0]["status"] == "refuted"


def test_missing_repo_exits_2(tmp_path, capsys):
    rep = write(tmp_path, "r.txt", "x")
    assert cli.main(["check", "--repo", str(tmp_path / "nope"), "--report", rep]) == 2


def test_markdown_lists_each_claim():
    verdict = Verdict([Evidence(Claim("symbol", "f() exists"), Status.CONFIRMED, "defined", "a.py:1")])
    text = report.to_markdown(verdict, "gemma3:1b")
    assert "Claim extraction: gemma3:1b" in text
    assert "| ✓ confirmed | f() exists | defined | observed | `a.py:1` |" in text
    assert "Tested revision: unknown" in text


def test_revision_of_a_git_checkout_is_reported(tmp_path, capsys):
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], cwd=tmp_path, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    (tmp_path / "urls.py").write_text("def parse_url(s):\n    return s\n")
    rep = write(tmp_path, "r.txt", "x `parse_url()`")
    cli.main(["check", "--repo", str(tmp_path), "--report", rep, "--no-model", "--json"])
    assert json.loads(capsys.readouterr().out)["revision"] == sha


def test_kubernetes_executor_is_used_with_its_settings(repo, tmp_path, monkeypatch, capsys):
    from claimcheck import k8s
    from claimcheck.models import Claim, Evidence, Status

    seen = {}

    class FakeExecutor:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def run_repro(self, repo_path, command, expect, timeout=30):
            seen["run"] = (command, expect)
            return Evidence(Claim("repro", command), Status.CONFIRMED, "reproduced in pod", "kubernetes[ns/gvisor]: x")

    monkeypatch.setattr(k8s, "KubernetesExecutor", FakeExecutor)
    rep = write(tmp_path, "r.txt", "crash in `parse_url()`")
    code = cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model", "--repro", "python3 poc.py",
                     "--expect", "boom", "--executor", "kubernetes", "--k8s-namespace", "ns",
                     "--k8s-runtime-class", "gvisor", "--kube-context", "kind-x"])
    assert code == 0
    assert seen["init"] == {"namespace": "ns", "image": "python:3.12-slim", "runtime_class": "gvisor", "context": "kind-x",
                            "impersonate": None}
    assert seen["run"] == ("python3 poc.py", "boom")
    assert "kubernetes[ns/gvisor]" in capsys.readouterr().out


EXECUTOR_SA = "system:serviceaccount:claimcheck:claimcheck-executor"


def kubernetes_check(repo, tmp_path, *extra):
    rep = write(tmp_path, "r.txt", "crash in `parse_url()`")
    return ["check", "--repo", str(repo), "--report", rep, "--no-model", "--repro", "python3 poc.py",
            "--expect", "boom", "--executor", "kubernetes", *extra]


def fake_kubectl(calls, stderr_for=None):
    """A kubectl that records its arguments; `stderr_for` maps a verb to a failure."""
    import subprocess

    def run(args, input=None, capture_output=True, text=True, timeout=None):
        calls.append(args)
        verb = args[1]
        if stderr_for and verb in stderr_for:
            return subprocess.CompletedProcess(args, 1, "", stderr_for[verb])
        if verb == "get":
            state = {"terminated": {"exitCode": 1}}
            return subprocess.CompletedProcess(args, 0, json.dumps({"items": [{"status": {"containerStatuses": [{"state": state}]}}]}), "")
        logs = "CLAIMCHECK-EGRESS-BLOCKED" if sum(a[1] == "logs" for a in calls) == 1 else "boom"
        return subprocess.CompletedProcess(args, 0, logs if verb == "logs" else "", "")
    return run


def use_fake_kubectl(monkeypatch, run):
    from claimcheck import k8s
    real = k8s.KubernetesExecutor
    monkeypatch.setattr(k8s, "KubernetesExecutor", lambda **kw: real(**kw, run=run, poll_interval=0))


def test_k8s_impersonate_runs_every_kubectl_call_as_that_identity(repo, tmp_path, monkeypatch, capsys):
    calls = []
    use_fake_kubectl(monkeypatch, fake_kubectl(calls))
    code = cli.main(kubernetes_check(repo, tmp_path, "--k8s-impersonate", EXECUTOR_SA, "--json"))
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["evidence"][-1]["status"] == "confirmed"
    assert {args[1] for args in calls} >= {"apply", "get", "logs", "delete"}
    assert all(args[args.index("--as") + 1] == EXECUTOR_SA for args in calls)


def test_without_k8s_impersonate_kubectl_runs_as_the_caller(repo, tmp_path, monkeypatch, capsys):
    calls = []
    use_fake_kubectl(monkeypatch, fake_kubectl(calls))
    assert cli.main(kubernetes_check(repo, tmp_path)) == 0
    assert calls and not [args for args in calls if "--as" in args]


def test_rbac_denial_is_reported_as_an_unverified_rbac_setup_failure(repo, tmp_path, monkeypatch, capsys):
    denied = "system:serviceaccount:claimcheck:default"
    stderr = (f'Error from server (Forbidden): error when retrieving current configuration of: ... configmaps '
              f'"claimcheck-1" is forbidden: User "{denied}" cannot get resource "configmaps" in API group "" in the '
              'namespace "claimcheck"')
    calls = []
    use_fake_kubectl(monkeypatch, fake_kubectl(calls, {"apply": stderr}))
    code = cli.main(kubernetes_check(repo, tmp_path, "--k8s-impersonate", denied, "--json"))
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["verdict"] == "unverified"
    repro = out["evidence"][-1]
    assert repro["status"] == "unverified"
    assert "RBAC setup failure" in repro["detail"]
    assert f'User "{denied}" cannot get configmaps' in repro["detail"]
    assert "NetworkPolicy" not in repro["detail"]


def test_k8s_impersonate_is_in_the_help():
    assert "--k8s-impersonate" in cli.build_parser()._subparsers._group_actions[0].choices["check"].format_help()


def test_sarif_flag_writes_the_file_and_keeps_stdout_and_exit_code(repo, tmp_path, capsys):
    rep = write(tmp_path, "r.txt", "RCE in parse_url_v2()")
    out_path = tmp_path / "out" / "claimcheck.sarif"
    out_path.parent.mkdir()
    code = cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model", "--sarif", str(out_path)])
    assert code == 1
    assert "**Verdict: refuted**" in capsys.readouterr().out
    data = json.loads(out_path.read_text())
    assert data["version"] == "2.1.0"
    result = data["runs"][0]["results"][0]
    assert result["ruleId"] == "claimcheck/symbol" and result["level"] == "error"
    assert "parse_url_v2" in result["message"]["text"]


def test_without_sarif_flag_no_file_is_written(repo, tmp_path):
    rep = write(tmp_path, "r.txt", "Crash in `parse_url()`")
    assert cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model"]) == 0
    assert not list(tmp_path.rglob("*.sarif"))


# Issue #73: the truncation flag reaches the verdict and every output.

class FakeOllamaResponse:
    def __init__(self, answer):
        self.body = json.dumps({"response": json.dumps(answer)}).encode()

    def read(self, *args):
        body, self.body = self.body, b""
        return body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_ollama(monkeypatch, answer):
    """Replace the HTTP call to Ollama; extract._ollama itself stays the real code."""
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        return FakeOllamaResponse(answer)

    monkeypatch.setattr(extract.urllib.request, "urlopen", urlopen)
    return calls


LONG_REPORT = "Crash in `parse_url()` on long input.\n" + "padding line\n" * 2000  # > 16 KiB


def test_run_check_marks_a_long_report_as_truncated(repo, monkeypatch):
    calls = fake_ollama(monkeypatch, {"symbols": ["parse_url"]})
    assert len(LONG_REPORT) > extract.MAX_REPORT_CHARS
    verdict, model = cli.run_check(repo, LONG_REPORT, None, None, "", True)
    assert calls, "the fake model was not called"
    assert model == extract.MODEL
    assert verdict.report_truncated is True


def test_run_check_does_not_mark_a_short_report(repo, monkeypatch):
    fake_ollama(monkeypatch, {"symbols": ["parse_url"]})
    verdict, model = cli.run_check(repo, "Crash in `parse_url()`", None, None, "", True)
    assert model == extract.MODEL
    assert verdict.report_truncated is False


def test_cli_outputs_show_truncation_for_a_long_report(repo, tmp_path, monkeypatch, capsys):
    fake_ollama(monkeypatch, {"symbols": ["parse_url"]})
    rep = write(tmp_path, "r.txt", LONG_REPORT)
    sarif = tmp_path / "out.sarif"
    cli.main(["check", "--repo", str(repo), "--report", rep, "--sarif", str(sarif)])
    assert "Report truncated to 16 KiB before the model" in capsys.readouterr().out
    assert json.loads(sarif.read_text())["runs"][0]["properties"]["reportTruncated"] is True
    cli.main(["check", "--repo", str(repo), "--report", rep, "--json"])
    assert json.loads(capsys.readouterr().out)["report_truncated"] is True


def test_cli_short_report_with_model_is_not_truncated(repo, tmp_path, monkeypatch, capsys):
    fake_ollama(monkeypatch, {"symbols": ["parse_url"]})
    rep = write(tmp_path, "r.txt", "Crash in `parse_url()`")
    cli.main(["check", "--repo", str(repo), "--report", rep, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["extraction_model"] == extract.MODEL
    assert data["report_truncated"] is False


def test_no_model_never_says_truncated_even_for_a_long_report(repo, tmp_path, monkeypatch, capsys):
    calls = fake_ollama(monkeypatch, {"symbols": ["parse_url"]})
    rep = write(tmp_path, "r.txt", LONG_REPORT)
    cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model"])
    assert "truncated" not in capsys.readouterr().out.lower()
    cli.main(["check", "--repo", str(repo), "--report", rep, "--no-model", "--json"])
    assert json.loads(capsys.readouterr().out)["report_truncated"] is False
    assert calls == []
