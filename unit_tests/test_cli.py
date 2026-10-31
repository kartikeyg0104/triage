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
