import json

import pytest

from claimcheck import cli, report
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


# Issue #78: --model-name picks the Ollama model.

def test_model_name_flag_picks_the_model_and_the_report_names_it(repo, tmp_path, monkeypatch, capsys):
    from test_extract import fake_ollama
    monkeypatch.delenv("CLAIMCHECK_MODEL", raising=False)
    rep = write(tmp_path, "r.txt", "Crash in `parse_url()` on long input")
    with fake_ollama({"symbols": ["parse_url"]}, known=("qwen3:0.6b",)) as (base, requests):
        monkeypatch.setenv("OLLAMA_HOST", base)
        code = cli.main(["check", "--repo", str(repo), "--report", rep, "--model-name", "qwen3:0.6b", "--json"])
        assert code == 0
        assert json.loads(capsys.readouterr().out)["extraction_model"] == "qwen3:0.6b"
        cli.main(["check", "--repo", str(repo), "--report", rep, "--model-name", "qwen3:0.6b"])
        assert "Claim extraction: qwen3:0.6b" in capsys.readouterr().out
    assert [r["body"]["model"] for r in requests] == ["qwen3:0.6b", "qwen3:0.6b"]


def test_model_name_flag_overrides_claimcheck_model(repo, tmp_path, monkeypatch, capsys):
    from test_extract import fake_ollama
    monkeypatch.setenv("CLAIMCHECK_MODEL", "from-env:1b")
    rep = write(tmp_path, "r.txt", "Crash in `parse_url()`")
    with fake_ollama({"symbols": ["parse_url"]}, known=("from-env:1b", "from-flag:2b")) as (base, requests):
        monkeypatch.setenv("OLLAMA_HOST", base)
        cli.main(["check", "--repo", str(repo), "--report", rep, "--json"])
        cli.main(["check", "--repo", str(repo), "--report", rep, "--model-name", "from-flag:2b", "--json"])
    assert [r["body"]["model"] for r in requests] == ["from-env:1b", "from-flag:2b"]


def test_unknown_model_name_falls_back_to_regex_and_the_report_says_so(repo, tmp_path, monkeypatch, capsys):
    from test_extract import fake_ollama
    monkeypatch.delenv("CLAIMCHECK_MODEL", raising=False)
    rep = write(tmp_path, "r.txt", "Crash in `parse_url()`")
    with fake_ollama({"symbols": ["parse_url"]}) as (base, requests):
        monkeypatch.setenv("OLLAMA_HOST", base)
        code = cli.main(["check", "--repo", str(repo), "--report", rep, "--model-name", "nosuch:7b"])
    captured = capsys.readouterr()
    assert code == 2
    assert "regex fallback" in captured.out
    assert "nosuch:7b" in captured.out and "not found" in captured.out
    assert "nosuch:7b" in captured.err and "not found" in captured.err


def test_model_name_is_in_the_help():
    help_text = cli.build_parser()._subparsers._group_actions[0].choices["check"].format_help()
    assert "--model-name" in help_text and "CLAIMCHECK_MODEL" in help_text


@pytest.mark.parametrize("format", ["markdown", "json", "sarif"])
def test_report_names_the_model_in_the_server_reply(repo, tmp_path, monkeypatch, capsys, format):
    from test_extract import fake_ollama
    rep = write(tmp_path, "r.txt", "Crash in `parse_url()`")
    output = tmp_path / "result.sarif"
    flags = ["--json"] if format == "json" else ["--sarif", str(output)] if format == "sarif" else []
    with fake_ollama({"symbols": ["parse_url"]}, known=("gemma3",), answer_as="gemma3:latest") as (base, requests):
        monkeypatch.setenv("OLLAMA_HOST", base)
        assert cli.main(["check", "--repo", str(repo), "--report", rep, "--model-name", "gemma3", *flags]) == 0
    stdout = capsys.readouterr().out
    if format == "json":
        assert json.loads(stdout)["extraction_model"] == "gemma3:latest"
    elif format == "sarif":
        assert json.loads(output.read_text())["runs"][0]["properties"]["extractionModel"] == "gemma3:latest"
    else:
        assert "Claim extraction: gemma3:latest" in stdout
    assert requests[0]["body"]["model"] == "gemma3"


@pytest.mark.parametrize("format", ["markdown", "json", "sarif"])
def test_unknown_model_setup_note_is_unverified_in_each_report(repo, tmp_path, monkeypatch, capsys, format):
    from test_extract import fake_ollama
    rep = write(tmp_path, "r.txt", "It feels slow")
    output = tmp_path / "result.sarif"
    flags = ["--json"] if format == "json" else ["--sarif", str(output)] if format == "sarif" else []
    with fake_ollama({}) as (base, requests):
        monkeypatch.setenv("OLLAMA_HOST", base)
        code = cli.main(["check", "--repo", str(repo), "--report", rep, "--model-name", "nosuch:7b", *flags])
    assert code == 2
    captured = capsys.readouterr()
    if format == "markdown":
        assert "unverified" in captured.out and "nosuch:7b" in captured.out and "not found" in captured.out
    elif format == "json":
        data = json.loads(captured.out)
        assert data["verdict"] == "unverified" and data["extraction_model"] is None
        assert len(data["evidence"]) == 1 and data["evidence"][0]["status"] == "unverified"
        assert "nosuch:7b" in data["evidence"][0]["detail"] and "regex" in data["evidence"][0]["detail"]
    else:
        run = json.loads(output.read_text())["runs"][0]
        assert run["properties"]["extractionModel"] is None
        assert len(run["results"]) == 1 and run["results"][0]["level"] == "warning"
        assert run["results"][0]["properties"]["status"] == "unverified"
        assert "nosuch:7b" in run["results"][0]["message"]["text"]
    assert requests[0]["body"]["model"] == "nosuch:7b"


def test_invalid_host_setup_note_is_in_the_report(repo, tmp_path, monkeypatch, capsys):
    from test_extract import fake_ollama
    rep = write(tmp_path, "r.txt", "It feels slow")
    with fake_ollama({}) as (base, requests):
        monkeypatch.setenv("OLLAMA_HOST", "file:///etc/passwd")
        assert cli.main(["check", "--repo", str(repo), "--report", rep, "--json"]) == 2
    data = json.loads(capsys.readouterr().out)
    assert requests == []
    assert len(data["evidence"]) == 1 and data["evidence"][0]["status"] == "unverified"
    assert "OLLAMA_HOST" in data["evidence"][0]["detail"]


def test_no_model_skips_host_and_model_settings(repo, tmp_path, monkeypatch, capsys):
    from test_extract import fake_ollama
    rep = write(tmp_path, "r.txt", "Crash in `parse_url()`")
    with fake_ollama({}) as (base, requests):
        monkeypatch.setenv("OLLAMA_HOST", base)
        monkeypatch.setenv("CLAIMCHECK_MODEL", "nosuch:7b")
        assert cli.main(["check", "--repo", str(repo), "--report", rep,
                         "--no-model", "--model-name", "also-missing:1b", "--json"]) == 0
    captured = capsys.readouterr()
    assert requests == [] and captured.err == ""
    assert json.loads(captured.out)["extraction_model"] is None
