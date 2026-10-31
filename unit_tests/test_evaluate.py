import json
import shutil
from pathlib import Path

import pytest

from claimcheck import evaluate, sandbox
from claimcheck.evaluate import SKIP_NO_ISOLATION, Case, CaseError, CaseResult, Run

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "eval" / "cases"
CASE_NAMES = sorted(p.name for p in CASES.iterdir() if p.is_dir()) if CASES.is_dir() else []

# Cases the tool gets wrong today. Only misses that leave a claim unverified may be listed here:
# a wrong confirmed or wrong refuted answer must fail.
KNOWN_MISSES: dict[str, str] = {}


def case(claims, overall, repro=None, name="c"):
    return Case(name=name, repo=Path("repo"), report="", diff=None, repro=repro, expect="",
                claims=claims, overall=overall, truth="written from the repo")


# --- scoring on hand-made results ------------------------------------------------------------

def test_wrong_confirmed_is_counted():
    result = CaseResult(case({"symbol:f": "unverified"}, "unverified"), [Run({"symbol:f": "confirmed"}, "supported")])
    summary = evaluate.summarize([result])
    assert (summary.wrong_confirmed, summary.wrong_refuted) == (1, 0)
    assert (summary.claims_correct, summary.verdicts_correct) == (0, 0)


def test_wrong_refuted_is_counted():
    result = CaseResult(case({"symbol:f": "unverified"}, "unverified"), [Run({"symbol:f": "refuted"}, "refuted")])
    summary = evaluate.summarize([result])
    assert (summary.wrong_confirmed, summary.wrong_refuted) == (0, 1)


def test_unverified_when_the_truth_is_unverified_is_correct():
    result = CaseResult(case({"pr_tests": "unverified"}, "unverified"), [Run({"pr_tests": "unverified"}, "unverified")])
    summary = evaluate.summarize([result])
    assert result.correct
    assert (summary.claims_correct, summary.claims_total) == (1, 1)
    assert (summary.verdicts_correct, summary.scored) == (1, 1)
    assert (summary.unverified, summary.expected_unverified) == (1, 1)
    assert (summary.wrong_confirmed, summary.wrong_refuted) == (0, 0)


def test_skipped_cases_are_excluded_from_accuracy():
    skipped = CaseResult(case({"repro": "confirmed"}, "supported", repro="python3 poc.py"), [], SKIP_NO_ISOLATION)
    scored = CaseResult(case({"symbol:f": "confirmed"}, "supported"), [Run({"symbol:f": "confirmed"}, "supported")])
    summary = evaluate.summarize([skipped, scored])
    assert (summary.cases, summary.skipped, summary.scored) == (2, 1, 1)
    assert (summary.claims_correct, summary.claims_total) == (1, 1)
    assert summary.verdicts_correct == 1
    assert summary.repeatable == 1


def test_missing_and_extra_claims_are_misses_but_not_wrong_answers():
    result = CaseResult(case({"symbol:a": "confirmed"}, "supported"), [Run({"symbol:b": "unverified"}, "unverified")])
    summary = evaluate.summarize([result])
    assert result.got("symbol:a") == ["missing"]
    assert (summary.claims_correct, summary.claims_total) == (0, 2)
    assert (summary.wrong_confirmed, summary.wrong_refuted) == (0, 0)


def test_extra_claim_that_flips_the_verdict_is_a_wrong_refuted():
    result = CaseResult(case({}, "unverified"), [Run({"symbol:prose": "refuted"}, "refuted")])
    assert evaluate.summarize([result]).wrong_refuted == 1


def test_repeatability_requires_identical_runs():
    same = CaseResult(case({"symbol:f": "confirmed"}, "supported"), [Run({"symbol:f": "confirmed"}, "supported")] * 3)
    differ = CaseResult(case({"symbol:f": "confirmed"}, "supported"),
                        [Run({"symbol:f": "confirmed"}, "supported"), Run({"symbol:f": "unverified"}, "unverified")])
    assert same.repeatable and not differ.repeatable
    assert evaluate.summarize([same, differ]).repeatable == 1


def test_a_wrong_answer_in_any_run_counts():
    result = CaseResult(case({"symbol:f": "unverified"}, "unverified"),
                        [Run({"symbol:f": "unverified"}, "unverified"), Run({"symbol:f": "refuted"}, "refuted")])
    summary = evaluate.summarize([result])
    assert not result.correct
    assert summary.wrong_refuted == 1 and summary.claims_correct == 0


# --- loading and validating cases ------------------------------------------------------------

VALID = {"truth": "parse_url is defined in urls.py", "expected": {"claims": {"symbol:parse_url": "confirmed"}, "overall": "supported"}}


def make_case(root: Path, name="c", data=None, report="Crash in `parse_url()`", diff=None) -> Path:
    path = root / name
    (path / "repo").mkdir(parents=True)
    (path / "repo" / "urls.py").write_text("def parse_url(s):\n    return s\n")
    (path / "report.txt").write_text(report)
    (path / "case.json").write_text(json.dumps(VALID if data is None else data))
    if diff is not None:
        (path / "pr.diff").write_text(diff)
    return path


def test_loads_a_valid_case(tmp_path):
    loaded = evaluate.load_case(make_case(tmp_path, diff="+++ b/urls.py\n",
                                          data={**VALID, "expected": {"claims": {"symbol:parse_url": "confirmed", "pr_tests": "unverified"},
                                                                      "overall": "unverified"}}))
    assert loaded.name == "c"
    assert loaded.claims == {"symbol:parse_url": "confirmed", "pr_tests": "unverified"}
    assert (loaded.overall, loaded.repro, loaded.expect) == ("unverified", None, "")
    assert loaded.diff == "+++ b/urls.py\n"
    assert loaded.report == "Crash in `parse_url()`"


def _with(**changes):
    data = json.loads(json.dumps(VALID))
    for key, value in changes.items():
        if key in ("claims", "overall"):
            data["expected"][key] = value
        elif value is None:
            data.pop(key)
        else:
            data[key] = value
    return data


@pytest.mark.parametrize("data,message", [
    (_with(truth=None), "truth"),
    (_with(overall="maybe"), "overall"),
    (_with(claims={"symbol:parse_url": "true"}), "status"),
    (_with(claims={"function:parse_url": "confirmed"}), "claim"),
    (_with(overall="unverified"), "does not follow"),
    (_with(repro="python3 poc.py"), "repro"),
    (_with(claims={"symbol:parse_url": "confirmed", "repro": "confirmed"}), "repro"),
    (_with(claims={"symbol:parse_url": "confirmed", "pr_tests": "confirmed"}), "pr.diff"),
    (_with(surprise=1), "unknown"),
    ([], "object"),
])
def test_bad_case_json_is_rejected(tmp_path, data, message):
    with pytest.raises(CaseError, match=message):
        evaluate.load_case(make_case(tmp_path, data=data))


def test_diff_without_a_pr_tests_claim_is_rejected(tmp_path):
    with pytest.raises(CaseError, match="pr_tests"):
        evaluate.load_case(make_case(tmp_path, diff="+++ b/urls.py\n"))


def test_unparseable_case_json_and_missing_repo_are_rejected(tmp_path):
    path = make_case(tmp_path, "broken")
    (path / "case.json").write_text("{not json")
    with pytest.raises(CaseError, match="JSON"):
        evaluate.load_case(path)
    path = make_case(tmp_path, "norepo")
    (path / "repo" / "urls.py").unlink()
    (path / "repo").rmdir()
    with pytest.raises(CaseError, match="repo"):
        evaluate.load_case(path)


def test_load_cases_is_sorted_and_rejects_a_directory_without_case_json(tmp_path):
    make_case(tmp_path, "b")
    make_case(tmp_path, "a")
    assert [c.name for c in evaluate.load_cases(tmp_path)] == ["a", "b"]
    (tmp_path / "c").mkdir()
    with pytest.raises(CaseError, match="case.json"):
        evaluate.load_cases(tmp_path)


# --- running cases through the real pipeline -------------------------------------------------

def test_run_case_uses_the_real_pipeline(tmp_path):
    result = evaluate.run_case(evaluate.load_case(make_case(tmp_path)), runs=2, isolated=True)
    assert result.runs == [Run({"symbol:parse_url": "confirmed"}, "supported")] * 2
    assert result.correct and not result.skipped


def test_repro_cases_are_skipped_without_isolation(tmp_path):
    data = {"truth": "poc.py raises", "repro": "python3 poc.py", "expect": "boom",
            "expected": {"claims": {"repro": "confirmed"}, "overall": "supported"}}
    loaded = evaluate.load_case(make_case(tmp_path, data=data, report="it crashes"))
    result = evaluate.run_case(loaded, runs=3, isolated=False)
    assert result.skipped == SKIP_NO_ISOLATION == "skipped (no sandbox isolation)"
    assert result.runs == []


# --- the markdown report ---------------------------------------------------------------------

META = {"date": "2026-11-01", "platform": "TestOS-1.0-arm64", "python": "3.12.0", "revision": "abc1234",
        "isolation": "sandbox-exec", "runs": "3"}


def test_markdown_has_metadata_metrics_and_every_case():
    ok = CaseResult(case({"symbol:f": "confirmed"}, "supported", name="ok_case"), [Run({"symbol:f": "confirmed"}, "supported")])
    bad = CaseResult(case({"symbol:g": "unverified"}, "unverified", name="bad_case"), [Run({"symbol:g": "refuted"}, "refuted")])
    skipped = CaseResult(case({"repro": "confirmed"}, "supported", repro="x", name="repro_case"), [], SKIP_NO_ISOLATION)
    text = evaluate.to_markdown([ok, bad, skipped], META)
    for expected in ("2026-11-01", "TestOS-1.0-arm64", "Python 3.12.0", "`abc1234`", "sandbox-exec",
                     "| Per-claim accuracy | 1/2 (50%) |",
                     "| Overall-verdict accuracy | 1/2 (50%) |",
                     "| Wrong confirmed (cases) | 0 |",
                     "| Wrong refuted (cases) | 1 |",
                     "| Unverified rate (verdicts) | 0/2 (0%); ground truth 1/2 (50%) |",
                     "| Repeatability (identical across 3 runs) | 2/2 (100%) |",
                     "| Skipped (no sandbox isolation) | 1 |",
                     "| bad_case | `symbol:g` | unverified | refuted | wrong refuted |",
                     "| ok_case | verdict | supported | supported | ok |",
                     "| repro_case | | | | skipped (no sandbox isolation) |"):
        assert expected in text, expected


def test_markdown_shows_every_run_when_runs_differ():
    flaky = CaseResult(case({"symbol:f": "confirmed"}, "supported", name="flaky"),
                       [Run({"symbol:f": "confirmed"}, "supported"), Run({"symbol:f": "unverified"}, "unverified")])
    assert "| flaky | `symbol:f` | confirmed | confirmed / unverified | miss |" in evaluate.to_markdown([flaky], META)


def test_main_writes_the_report_and_fails_on_a_wrong_refuted(tmp_path):
    cases, out = tmp_path / "cases", tmp_path / "docs" / "EVAL.md"
    make_case(cases, "a")
    assert evaluate.main(["--cases", str(cases), "--runs", "1", "--out", str(out)]) == 0
    text = out.read_text()
    assert "| Per-claim accuracy | 1/1 (100%) |" in text and "Git revision:" in text
    make_case(cases, "b", report="RCE in `parse_url_v2()`",
              data={"truth": "deliberately wrong for this test", "expected": {"claims": {"symbol:parse_url_v2": "unverified"}, "overall": "unverified"}})
    assert evaluate.main(["--cases", str(cases), "--runs", "1", "--out", str(out)]) == 1


def test_main_reports_a_bad_case_and_exits_2(tmp_path, capsys):
    make_case(tmp_path, "a", data=_with(overall="maybe"))
    assert evaluate.main(["--cases", str(tmp_path)]) == 2
    assert "overall" in capsys.readouterr().err


def test_main_skips_repro_cases_when_the_machine_has_no_isolation(tmp_path, monkeypatch, capsys):
    data = {"truth": "poc.py raises", "repro": "python3 poc.py", "expect": "boom",
            "expected": {"claims": {"repro": "confirmed"}, "overall": "supported"}}
    make_case(tmp_path, "r", data=data, report="it crashes")
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: None)
    assert evaluate.main(["--cases", str(tmp_path), "--runs", "1"]) == 0
    assert "| r | | | | skipped (no sandbox isolation) |" in capsys.readouterr().out


# --- the real cases --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_results():
    isolated = sandbox.detect_isolation() is not None
    return evaluate.evaluate(evaluate.load_cases(CASES), runs=2, isolated=isolated)


def test_every_case_loads_and_runs(real_results):
    assert len(CASE_NAMES) >= 12
    assert [r.case.name for r in real_results] == CASE_NAMES


def test_no_case_gets_a_wrong_confirmed_or_wrong_refuted(real_results):
    summary = evaluate.summarize(real_results)
    wrong = [r.case.name for r in real_results if r.wrong_confirmed or r.wrong_refuted]
    assert (summary.wrong_confirmed, summary.wrong_refuted) == (0, 0), wrong


def test_real_cases_are_repeatable(real_results):
    assert [r.case.name for r in real_results if not r.repeatable] == []


def test_real_cases_are_right_from_under_a_build_directory(tmp_path):
    """Where a checkout lives must not change what is found in it. Repro cases are skipped: they
    test the sandbox, not the static search."""
    moved = tmp_path / "build" / "cases"
    shutil.copytree(CASES, moved)
    results = evaluate.evaluate(evaluate.load_cases(moved), runs=1, isolated=False)
    assert [r.case.name for r in results if not r.skipped and r.case.name not in KNOWN_MISSES and not r.correct] == []


@pytest.mark.parametrize("name", [
    pytest.param(n, marks=pytest.mark.xfail(reason=KNOWN_MISSES[n], strict=True)) if n in KNOWN_MISSES else n
    for n in CASE_NAMES
])
def test_case_matches_ground_truth(name, real_results):
    result = next(r for r in real_results if r.case.name == name)
    if result.skipped:
        pytest.skip(result.skipped)
    assert not (result.wrong_confirmed or result.wrong_refuted), name
    assert result.correct, [(key, result.case.claims.get(key), result.got(key)) for key in result.claim_keys()]
