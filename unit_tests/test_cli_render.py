"""Saved reports retain their evidence without rerunning an investigation."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from claimcheck import cli, report
from claimcheck.models import Claim, Evidence, Status, Verdict

SHA = "0123456789abcdef0123456789abcdef01234567"
KINDS = ["symbol", "pr_tests", "repro", "fix", "extraction", "network", "custom"]
RENDERERS = {"markdown": report.to_markdown, "sarif": report.to_sarif}


def saved_verdict(status: Status = Status.UNVERIFIED, basis: str = "observed",
                  kind: str = "symbol", revision: str = "", truncated: bool = False) -> Verdict:
    return Verdict([Evidence(Claim(kind, 'f() exists | "quoted"', "f"), status,
                             "evidence with a newline\nand \\ Unicode: ✓", "src/urls.py:42", basis)],
                   revision=revision, report_truncated=truncated)


def save(tmp_path: Path, verdict: Verdict, model: str | None = None) -> Path:
    path = tmp_path / "report.json"
    path.write_text(report.to_json(verdict, model), encoding="utf-8")
    return path


@pytest.mark.parametrize("status", list(Status))
@pytest.mark.parametrize("basis", ["observed", "inferred"])
@pytest.mark.parametrize("kind", KINDS)
def test_json_round_trip_keeps_every_evidence_status_basis_and_kind(status, basis, kind):
    verdict = saved_verdict(status, basis, kind)
    restored, model = report.from_json(report.to_json(verdict, "gemma3:1b"))
    assert restored.evidence[0].status == status
    assert restored.evidence[0].basis == basis
    assert restored.evidence[0].claim.kind == kind
    assert model == "gemma3:1b"
    assert report.to_json(restored, model) == report.to_json(verdict, model)
    for renderer in RENDERERS.values():
        assert renderer(restored, model) == renderer(verdict, model)


@pytest.mark.parametrize("revision", ["", SHA, f"{SHA}..{SHA}"])
@pytest.mark.parametrize("truncated", [False, True])
@pytest.mark.parametrize("model", [None, "gemma3:1b", ""])
def test_json_round_trip_keeps_model_truncation_and_revision(revision, truncated, model):
    verdict = saved_verdict(revision=revision, truncated=truncated)
    restored, restored_model = report.from_json(report.to_json(verdict, model))
    assert restored.revision == revision
    assert restored.report_truncated is truncated
    assert restored_model == model
    for renderer in RENDERERS.values():
        assert renderer(restored, restored_model) == renderer(verdict, model)


@pytest.mark.parametrize("statuses", [[], [Status.CONFIRMED, Status.UNVERIFIED],
                                     [Status.REFUTED, Status.UNVERIFIED, Status.CONFIRMED]])
def test_json_round_trip_keeps_empty_reports_order_and_conservative_outcome(statuses):
    verdict = Verdict([saved_verdict(status).evidence[0] for status in statuses])
    restored, model = report.from_json(report.to_json(verdict, None))
    assert restored.outcome == verdict.outcome
    assert [e.status for e in restored.evidence] == statuses
    for renderer in RENDERERS.values():
        assert renderer(restored, model) == renderer(verdict, None)


@pytest.mark.parametrize("format", RENDERERS)
@pytest.mark.parametrize("status", list(Status))
@pytest.mark.parametrize("basis", ["observed", "inferred"])
def test_render_matches_check_output_and_exit_code(tmp_path, monkeypatch, capsys, format, status, basis):
    verdict = saved_verdict(status, basis, "fix", SHA, True)
    model = "gemma3:1b"
    monkeypatch.setattr(cli, "run_check", lambda *args, **kwargs: (verdict, model))
    text = tmp_path / "input.txt"
    text.write_text("Fixes f()")
    sarif = tmp_path / "check.sarif"
    args = ["check", "--repo", str(tmp_path), "--report", str(text), "--sarif", str(sarif)]
    check_code = cli.main(args)
    markdown = capsys.readouterr().out
    check_output = markdown if format == "markdown" else sarif.read_text()
    assert cli.main([*args, "--json"]) == check_code
    saved = tmp_path / "saved.json"
    saved.write_text(capsys.readouterr().out)
    assert cli.main(["render", str(saved), "--format", format]) == check_code
    captured = capsys.readouterr()
    assert captured.out == check_output
    assert captured.err == ""


@pytest.mark.parametrize("format", RENDERERS)
@pytest.mark.parametrize("text,diff", [("`f()`", None), ("`missing()`", None), ("slow", None),
                                       ("`f()`", "--- a/urls.py\n+++ b/urls.py\n")])
def test_real_check_json_can_be_rendered_later(tmp_path, capsys, format, text, diff):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "urls.py").write_text("def f():\n    pass\n")
    source = tmp_path / "input.txt"
    source.write_text(text)
    sarif = tmp_path / "check.sarif"
    args = ["check", "--repo", str(repo), "--report", str(source), "--no-model", "--sarif", str(sarif)]
    if diff is not None:
        patch = tmp_path / "change.diff"
        patch.write_text(diff)
        args += ["--diff", str(patch)]
    check_code = cli.main(args)
    markdown = capsys.readouterr().out
    expected = markdown if format == "markdown" else sarif.read_text()
    assert cli.main([*args, "--json"]) == check_code
    saved = tmp_path / "saved.json"
    saved.write_text(capsys.readouterr().out)
    assert cli.main(["render", str(saved), "--format", format]) == check_code
    assert capsys.readouterr().out == expected


def malformed_reports() -> list[tuple[str, str]]:
    data = json.loads(report.to_json(saved_verdict(), None))
    cases = [("invalid-json", "{"), ("trailing-content", json.dumps(data) + " {}"),
             ("array", "[]"), ("null", "null"), ("scalar", '"report"'),
             ("sarif-schema", report.to_sarif(saved_verdict(), None)),
             ("duplicate-key", json.dumps(data)[:-1] + ', "verdict": "unverified"}'),
             ("deeply-nested", "[" * 2000 + "0" + "]" * 2000)]
    for key in data:
        changed = dict(data)
        del changed[key]
        cases.append((f"missing-{key}", json.dumps(changed)))
    for key, value in [("schema_version", 999), ("$schema", "unknown"), ("extra", True),
                       ("verdict", "supported"), ("verdict", "unknown"), ("verdict", None),
                       ("extraction_model", 1), ("revision", []), ("report_truncated", "false"),
                       ("report_truncated", 0), ("report_truncated", None),
                       ("evidence", {}), ("evidence", None), ("evidence", [None]),
                       ("evidence", ["row"])]:
        changed = dict(data, **{key: value})
        cases.append((f"invalid-{key}-{value}", json.dumps(changed)))
    for key in data["evidence"][0]:
        changed = json.loads(json.dumps(data))
        del changed["evidence"][0][key]
        cases.append((f"missing-evidence-{key}", json.dumps(changed)))
        changed = json.loads(json.dumps(data))
        changed["evidence"][0][key] = None
        cases.append((f"invalid-evidence-{key}", json.dumps(changed)))
    for key, value in [("status", "unknown"), ("basis", "guessed"), ("extra", "field")]:
        changed = json.loads(json.dumps(data))
        changed["evidence"][0][key] = value
        cases.append((f"unknown-evidence-{key}", json.dumps(changed)))
    for constant in ["NaN", "Infinity", "-Infinity"]:
        cases.append((f"non-json-{constant}", json.dumps(data).replace('"extraction_model": null',
                                                                      f'"extraction_model": {constant}')))
    return cases


@pytest.mark.parametrize("format", RENDERERS)
@pytest.mark.parametrize("name,payload", malformed_reports(), ids=lambda value: value if len(value) < 80 else None)
def test_render_rejects_malformed_or_unknown_schema_json(tmp_path, capsys, format, name, payload):
    saved = tmp_path / "invalid.json"
    saved.write_text(payload)
    assert cli.main(["render", str(saved), "--format", format]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("problem", ["missing", "directory", "encoding", "denied"])
def test_render_reports_unreadable_input_as_exit_2(tmp_path, monkeypatch, capsys, problem):
    saved = tmp_path / "unreadable.json"
    if problem == "directory":
        saved.mkdir()
    elif problem == "encoding":
        saved.write_bytes(b"\xff\xfe")
    elif problem == "denied":
        def denied(*args, **kwargs):
            raise PermissionError("report is unreadable")
        monkeypatch.setattr(Path, "read_text", denied)
    assert cli.main(["render", str(saved), "--format", "markdown"]) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("error:")


@pytest.mark.parametrize("format", RENDERERS)
def test_render_never_runs_commands_checks_models_or_executors(tmp_path, monkeypatch, capsys, format):
    marker = tmp_path / "MARKER"
    payload = (f"touch {marker}; $(touch {marker}); `touch {marker}`; "
               f"__import__('os').system('touch {marker}')")
    evidence = [Evidence(Claim(kind, payload, payload), Status.UNVERIFIED, payload, payload, "inferred")
                for kind in KINDS]
    verdict = Verdict(evidence, revision=payload, report_truncated=True)
    saved = save(tmp_path, verdict, payload)

    def forbidden(*args, **kwargs):
        pytest.fail("render attempted to execute or investigate report data")

    monkeypatch.setattr(cli, "run_check", forbidden)
    monkeypatch.setattr(cli, "git_revision", forbidden)
    monkeypatch.setattr(cli.checks, "check_symbol", forbidden)
    monkeypatch.setattr(cli.extract, "extract_claims", forbidden)
    monkeypatch.setattr(cli.extract, "regex_extract", forbidden)
    monkeypatch.setattr(cli.sandbox, "run_repro", forbidden)
    monkeypatch.setattr(cli.k8s, "KubernetesExecutor", forbidden)
    monkeypatch.setattr(cli.revisions, "verify_fix", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    assert cli.main(["render", str(saved), "--format", format]) == 2
    assert capsys.readouterr().out == RENDERERS[format](verdict, payload)
    assert not marker.exists()


@pytest.mark.parametrize("format", RENDERERS)
def test_render_module_entry_point(tmp_path, format):
    verdict = saved_verdict(Status.REFUTED, "inferred", revision=SHA, truncated=True)
    saved = save(tmp_path, verdict, "gemma3:1b")
    done = subprocess.run([sys.executable, "-m", "claimcheck", "render", str(saved), "--format", format],
                          cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert done.returncode == 1
    assert done.stdout == RENDERERS[format](verdict, "gemma3:1b")
    assert done.stderr == ""


def test_render_is_in_help_and_requires_a_known_format():
    parser = cli.build_parser()
    assert "render" in parser.format_help()
    render = parser._subparsers._group_actions[0].choices["render"]
    assert "--format" in render.format_help()
    for args in [["render"], ["render", "report.json"], ["render", "report.json", "--format", "json"],
                 ["render", "report.json", "--format", "markdown", "--repro", "touch MARKER"]]:
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(args)
        assert exc.value.code == 2
