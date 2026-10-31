import json
import shutil
import subprocess
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


# --- the model (Gemma) path, with a fake model -----------------------------------------------
# The real model path needs Ollama, which CI does not have; these tests replace the generate
# function that extract_claims calls. docs/EVALUATION_GEMMA.md is the real run.

MODEL_REPORT = "Crash in `parse_url()` when UrlParser gets an empty string"


def fake_model(monkeypatch, reply=None, error=None):
    """Make extract_claims call a fake generate function instead of Ollama."""
    from claimcheck import extract

    real = extract.extract_claims
    calls = []

    def generate(prompt):
        calls.append(prompt)
        if error is not None:
            raise error
        return json.dumps(reply)

    monkeypatch.setattr(extract, "extract_claims", lambda report, generate_=generate: real(report, generate_))
    return calls


def model_case(tmp_path, name="m"):
    path = make_case(tmp_path, name, report=MODEL_REPORT,
                     data={"truth": "parse_url and UrlParser are both defined in urls.py",
                           "expected": {"claims": {"symbol:parse_url": "confirmed"}, "overall": "supported"}})
    (path / "repo" / "urls.py").write_text("def parse_url(s):\n    return s\n\nclass UrlParser:\n    pass\n")
    return evaluate.load_case(path)


def test_model_run_records_that_the_model_was_used(tmp_path, monkeypatch):
    calls = fake_model(monkeypatch, {"symbols": ["UrlParser"], "repro_command": "", "expected_error": ""})
    result = evaluate.run_case(model_case(tmp_path), runs=2, isolated=True, use_model=True)
    assert len(calls) == 2
    assert [run.model for run in result.runs] == ["gemma3:1b"] * 2
    assert result.runs[0].claims == {"symbol:UrlParser": "confirmed", "symbol:parse_url": "confirmed"}


def test_regex_run_records_no_model(tmp_path, monkeypatch):
    calls = fake_model(monkeypatch, {"symbols": ["UrlParser"]})
    result = evaluate.run_case(model_case(tmp_path), runs=1, isolated=True)
    assert calls == [] and result.runs[0].model is None


def test_a_run_that_fell_back_to_regex_is_not_reported_as_the_model(tmp_path, monkeypatch):
    import urllib.error
    fake_model(monkeypatch, error=urllib.error.URLError("connection refused"))
    result = evaluate.run_case(model_case(tmp_path), runs=1, isolated=True, use_model=True)
    assert result.runs[0].model is None
    text = evaluate.to_markdown([result], {**META, "runs": "1", "model": "gemma3:1b"})
    assert "- Extraction: gemma3:1b via Ollama in 0/1 runs; 1 run fell back to regex extraction (m)" in text
    assert "gemma3:1b was not used in every run" in text


def test_differing_names_are_reported_per_case(tmp_path, monkeypatch):
    fake_model(monkeypatch, {"symbols": ["UrlParser", "not_in_report"]})
    same = evaluate.load_case(make_case(tmp_path, "same"))
    results = [evaluate.run_case(model_case(tmp_path), runs=1, isolated=True, use_model=True),
               evaluate.run_case(same, runs=1, isolated=True, use_model=True)]
    assert evaluate.name_differences(results[0]) == (["parse_url"], ["UrlParser", "parse_url"], ["UrlParser"], [])
    assert evaluate.name_differences(results[1]) is None
    text = evaluate.to_markdown(results, {**META, "runs": "1", "model": "gemma3:1b"})
    assert "## Names that differ from regex extraction" in text
    assert "| m | parse_url | UrlParser, parse_url | UrlParser | |" in text
    assert "| same |" not in text.split("## Names that differ from regex extraction")[1].split("##")[0]
    # The extra name is not in the truth, so the case is a miss, and it is shown as one.
    assert "| m | `symbol:UrlParser` | not claimed | confirmed | miss |" in text


def _metric_labels(text):
    table = text.split("## Results")[1].split("\n\n")[1]
    return [row.split("|")[1].strip() for row in table.splitlines()[2:]]


def test_model_metrics_have_the_same_shape_as_the_regex_run(tmp_path, monkeypatch):
    fake_model(monkeypatch, {"symbols": ["UrlParser"]})
    loaded = model_case(tmp_path)
    regex = evaluate.to_markdown(evaluate.evaluate([loaded], runs=1, isolated=True), {**META, "runs": "1"})
    model_results = evaluate.evaluate([loaded], runs=1, isolated=True, use_model=True)
    model = evaluate.to_markdown(model_results, {**META, "runs": "1", "model": "gemma3:1b"})
    assert _metric_labels(model) == _metric_labels(regex) and len(_metric_labels(regex)) == 7
    assert type(evaluate.summarize(model_results)) is evaluate.Summary
    assert "use_model=True" in model and "use_model=False" in regex
    assert "--model gemma3:1b" in model and "--model" not in regex
    assert "## Names that differ" not in regex


def test_main_with_the_model_uses_it_and_says_so(tmp_path, monkeypatch):
    fake_model(monkeypatch, {"symbols": ["UrlParser"]})
    monkeypatch.setattr(evaluate, "ollama_unavailable", lambda model: "")
    cases, out = tmp_path / "cases", tmp_path / "EVAL_GEMMA.md"
    model_case(cases)
    assert evaluate.main(["--cases", str(cases), "--runs", "2", "--model", "gemma3:1b", "--out", str(out)]) == 0
    text = out.read_text()
    assert "# claimcheck evaluation: gemma3:1b extraction" in text
    assert "- Extraction: gemma3:1b via Ollama in 2/2 runs" in text
    assert "fell back" not in text


def test_main_with_the_model_unreachable_skips_the_whole_run(tmp_path, monkeypatch, capsys):
    calls = fake_model(monkeypatch, {"symbols": ["UrlParser"]})
    monkeypatch.setattr(evaluate, "ollama_unavailable", lambda model: "Ollama not reachable at http://localhost:11434")
    cases, out = tmp_path / "cases", tmp_path / "EVAL_GEMMA.md"
    model_case(cases, "a")
    model_case(cases, "b")
    assert evaluate.main(["--cases", str(cases), "--model", "gemma3:1b", "--out", str(out)]) == 0
    text = out.read_text()
    assert calls == []
    assert "**Skipped: Ollama not reachable at http://localhost:11434. No case was run with gemma3:1b.**" in text
    assert "| Skipped (Ollama unreachable) | 2 |" in text
    assert "| a | | | | skipped (Ollama unreachable) |" in text
    assert "via Ollama in" not in text
    assert "skipped" in capsys.readouterr().out


def test_main_rejects_a_model_extract_does_not_use(tmp_path, capsys):
    make_case(tmp_path, "a")
    with pytest.raises(SystemExit):
        evaluate.main(["--cases", str(tmp_path), "--model", "llama3"])
    assert "gemma3:1b" in capsys.readouterr().err


class _Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def test_ollama_unavailable_checks_the_server_and_the_model(monkeypatch):
    import urllib.error
    import urllib.request

    def refused(*args, **kwargs):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert "not reachable" in evaluate.ollama_unavailable("gemma3:1b")
    tags = json.dumps({"models": [{"name": "llama3:8b"}]}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response(tags))
    assert "gemma3:1b" in evaluate.ollama_unavailable("gemma3:1b")
    tags = json.dumps({"models": [{"name": "gemma3:1b"}]}).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Response(tags))
    assert evaluate.ollama_unavailable("gemma3:1b") == ""


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


# --- the JSON output and timing --------------------------------------------------------------

def _markdown_metrics(text):
    table = text.split("## Results")[1].split("\n\n")[1]
    return {row.split("|")[1].strip(): row.split("|")[2].strip() for row in table.splitlines()[2:]}


def _markdown_table(text, heading):
    table = text.split(heading)[1].split("\n\n")[1]
    return [[cell.strip() for cell in row.split("|")[1:-1]] for row in table.splitlines()[2:]]


def assert_json_matches_markdown(data, text):
    """Every number and per-case answer in the JSON is the one the markdown shows."""
    m, runs, ratio = data["metrics"], data["runs_per_case"], evaluate._ratio
    shown = _markdown_metrics(text)
    plural = f"{runs} run" + ("" if runs == 1 else "s")
    assert shown == {
        "Per-claim accuracy": ratio(m["claims_correct"], m["claims_total"]),
        "Overall-verdict accuracy": ratio(m["verdicts_correct"], m["scored"]),
        "Wrong confirmed (cases)": str(m["wrong_confirmed"]),
        "Wrong refuted (cases)": str(m["wrong_refuted"]),
        "Unverified rate (verdicts)": f"{ratio(m['unverified'], m['scored'])}; ground truth "
                                      f"{ratio(m['expected_unverified'], m['scored'])}",
        f"Repeatability (identical across {plural})": ratio(m["repeatable"], m["scored"]),
        next(k for k in shown if k.startswith("Skipped")): str(m["skipped"]),
    }
    assert f"- Cases: {m['cases']} ({m['scored']} scored, {m['skipped']} skipped)" in text
    rows = []
    for c in data["cases"]:
        if c["skipped"]:
            rows.append([c["name"], "", "", "", c["skipped"]])
            continue
        for claim in c["claims"]:
            assert len(claim["got"]) == runs and claim["correct"] == (claim["label"] == "ok")
            rows.append([c["name"], f"`{claim['key']}`", claim["expected"], evaluate._shown(claim["got"]), claim["label"]])
        o = c["overall"]
        assert len(o["got"]) == runs and [run["overall"] for run in c["runs"]] == o["got"]
        rows.append([c["name"], "verdict", o["expected"], evaluate._shown(o["got"]), o["label"]])
        assert all(run["seconds"] >= 0 for run in c["runs"])
        assert c["seconds"] == round(sum(run["seconds"] for run in c["runs"]), 3)
    assert _markdown_table(text, "## Per case") == rows
    t = data["timing"]
    assert t["runs"] == sum(len(c["runs"]) for c in data["cases"])
    assert t["total_seconds"] == round(sum(run["seconds"] for c in data["cases"] for run in c["runs"]), 3)
    assert f"- Wall-clock: {t['total_seconds']:.3f}s over {t['runs']} run" in text
    assert _markdown_table(text, "## Slowest cases") == [[s["case"], f"{s['seconds']:.3f}"] for s in t["slowest"]]
    by_name = {c["name"]: c["seconds"] for c in data["cases"]}
    assert [s["seconds"] for s in t["slowest"]] == sorted((by_name[s["case"]] for s in t["slowest"]), reverse=True)


def test_main_json_matches_the_markdown_on_real_cases(tmp_path):
    cases, out, js = tmp_path / "cases", tmp_path / "EVAL.md", tmp_path / "out" / "eval.json"
    for name in CASE_NAMES[:2]:
        shutil.copytree(CASES / name, cases / name)
    make_case(cases, "zz_wrong", report="RCE in `parse_url_v2()`",
              data={"truth": "deliberately wrong for this test",
                    "expected": {"claims": {"symbol:parse_url_v2": "unverified"}, "overall": "unverified"}})
    assert evaluate.main(["--cases", str(cases), "--runs", "2", "--out", str(out), "--json", str(js)]) == 1
    data = json.loads(js.read_text())
    assert data["schema_version"] == evaluate.JSON_SCHEMA_VERSION == 1
    assert data["runs_per_case"] == 2 and data["model"] is None
    assert [c["name"] for c in data["cases"]] == CASE_NAMES[:2] + ["zz_wrong"]
    assert data["metrics"]["wrong_refuted"] == 1
    wrong = data["cases"][-1]
    assert wrong["correct"] is False and wrong["wrong_refuted"] is True
    assert wrong["claims"][0] == {"key": "symbol:parse_url_v2", "expected": "unverified",
                                  "got": ["refuted", "refuted"], "label": "wrong refuted", "correct": False}
    assert set(data["generated"]) >= {"date", "platform", "python", "revision", "uncommitted", "isolation"}
    assert_json_matches_markdown(data, out.read_text())


def test_main_json_with_the_model_records_the_model_per_run(tmp_path, monkeypatch, capsys):
    fake_model(monkeypatch, {"symbols": ["UrlParser"]})
    monkeypatch.setattr(evaluate, "ollama_unavailable", lambda model: "")
    cases, js = tmp_path / "cases", tmp_path / "eval.json"
    model_case(cases, "a")
    make_case(cases, "b")
    assert evaluate.main(["--cases", str(cases), "--runs", "2", "--model", "gemma3:1b", "--json", str(js)]) == 0
    data = json.loads(js.read_text())
    assert data["model"] == "gemma3:1b"
    assert [run["model"] for c in data["cases"] for run in c["runs"]] == ["gemma3:1b"] * 4
    assert data["cases"][0]["runs"][0]["claims"] == {"symbol:UrlParser": "confirmed", "symbol:parse_url": "confirmed"}
    assert_json_matches_markdown(data, capsys.readouterr().out)


def test_main_json_when_the_model_is_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "ollama_unavailable", lambda model: "Ollama not reachable")
    cases, js = tmp_path / "cases", tmp_path / "eval.json"
    model_case(cases, "a")
    assert evaluate.main(["--cases", str(cases), "--model", "gemma3:1b", "--json", str(js),
                          "--out", str(tmp_path / "x.md")]) == 0
    data = json.loads(js.read_text())
    assert data["skip_reason"] == "Ollama not reachable"
    assert data["cases"][0]["skipped"] == "skipped (Ollama unreachable)" and data["cases"][0]["runs"] == []
    assert data["timing"] == {"total_seconds": 0.0, "runs": 0, "slowest": []}


def test_run_seconds_do_not_affect_repeatability():
    a = Run({"symbol:f": "confirmed"}, "supported", seconds=0.1)
    b = Run({"symbol:f": "confirmed"}, "supported", seconds=0.9)
    assert CaseResult(case({"symbol:f": "confirmed"}, "supported"), [a, b]).repeatable


def test_markdown_shows_total_time_and_the_slowest_cases():
    fast = CaseResult(case({"symbol:f": "confirmed"}, "supported", name="fast"),
                      [Run({"symbol:f": "confirmed"}, "supported", seconds=0.25)] * 2)
    slow = CaseResult(case({"symbol:f": "confirmed"}, "supported", name="slow"),
                      [Run({"symbol:f": "confirmed"}, "supported", seconds=1.5)] * 2)
    text = evaluate.to_markdown([fast, slow], {**META, "runs": "2"})
    assert "- Wall-clock: 3.500s over 4 runs" in text
    assert "| slow | 3.000 |\n| fast | 0.500 |" in text


# --- the executor (local sandbox or Kubernetes), with a fake executor -------------------------
# The real Kubernetes run needs a cluster: unit_tests/integration/test_evaluate_k8s.py.

REPRO_CASE = {"truth": "poc.py raises boom", "repro": "python3 poc.py", "expect": "boom",
              "expected": {"claims": {"repro": "confirmed"}, "overall": "supported"}}
K8S_VERIFIED = "kubernetes[ns-x/default runtime] (egress blocked, verified): python3 poc.py"
K8S_OPEN = "kubernetes[ns-x/default runtime] (egress open): python3 poc.py"


def fake_run_repro(source, status="confirmed", calls=None):
    from claimcheck.models import Claim, Evidence, Status

    def run_repro(repo, command, expect, timeout=30):
        if calls is not None:
            calls.append((Path(repo).name, command, expect))
        return Evidence(Claim("repro", f"`{command}` reproduces the reported failure", command), Status(status),
                        "fake executor", source)
    return run_repro


def fake_kubernetes(monkeypatch, source=K8S_VERIFIED, status="confirmed"):
    """Replace claimcheck.k8s.KubernetesExecutor; returns the list of executors created."""
    from claimcheck import k8s
    instances = []

    class FakeExecutor:
        def __init__(self, **kwargs):
            self.kwargs, self.calls = kwargs, []
            self.run_repro = fake_run_repro(source, status, self.calls)
            instances.append(self)

    monkeypatch.setattr(k8s, "KubernetesExecutor", FakeExecutor)
    return instances


def test_evaluate_runs_repros_through_the_given_executor(tmp_path):
    calls = []
    loaded = evaluate.load_case(make_case(tmp_path, data=REPRO_CASE, report="it crashes"))
    results = evaluate.evaluate([loaded], runs=2, isolated=True, run_repro=fake_run_repro(K8S_VERIFIED, calls=calls))
    assert calls == [("repo", "python3 poc.py", "boom")] * 2
    assert [run.repro_source for run in results[0].runs] == [K8S_VERIFIED] * 2
    assert results[0].correct
    assert evaluate.egress_verified(results) is True


def test_egress_is_verified_only_when_every_repro_run_says_so(tmp_path):
    loaded = evaluate.load_case(make_case(tmp_path, data=REPRO_CASE, report="it crashes"))
    plain = evaluate.load_case(make_case(tmp_path, "s"))
    verified = CaseResult(loaded, [Run({"repro": "confirmed"}, "supported", repro_source=K8S_VERIFIED)])
    opened = CaseResult(loaded, [Run({"repro": "unverified"}, "unverified", repro_source=K8S_OPEN)])
    no_repro = CaseResult(plain, [Run({"symbol:parse_url": "confirmed"}, "supported")])
    assert evaluate.egress_verified([verified, no_repro]) is True
    assert evaluate.egress_verified([verified, opened]) is False
    assert evaluate.egress_verified([no_repro]) is None


def test_main_with_kubernetes_uses_one_executor_and_records_it(tmp_path, monkeypatch):
    instances = fake_kubernetes(monkeypatch)
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: None)  # the local sandbox plays no part
    monkeypatch.setattr(sandbox, "run_repro", lambda *a, **k: pytest.fail("the local sandbox was used"))
    cases, out, js = tmp_path / "cases", tmp_path / "EVAL.md", tmp_path / "eval.json"
    make_case(cases, "a", data=REPRO_CASE, report="it crashes")
    make_case(cases, "b", data=REPRO_CASE, report="it crashes")
    make_case(cases, "c")
    argv = ["--cases", str(cases), "--runs", "2", "--executor", "kubernetes", "--kube-context", "kind-x",
            "--k8s-namespace", "ns-x", "--out", str(out), "--json", str(js)]
    assert evaluate.main(argv) == 0
    assert len(instances) == 1, "one executor per evaluation run, so the egress probe runs once"
    assert instances[0].kwargs == {"namespace": "ns-x", "context": "kind-x"}
    assert [call[1] for call in instances[0].calls] == ["python3 poc.py"] * 4
    data, text = json.loads(js.read_text()), out.read_text()
    assert data["executor"] == {"name": "kubernetes", "context": "kind-x", "namespace": "ns-x", "egress_verified": True}
    assert data["metrics"]["skipped"] == 0 and data["metrics"]["verdicts_correct"] == 3
    assert [run["repro_source"] for run in data["cases"][0]["runs"]] == [K8S_VERIFIED] * 2
    assert data["cases"][2]["runs"][0]["repro_source"] is None
    assert "--executor kubernetes --kube-context kind-x --k8s-namespace ns-x" in text
    assert "- Executor: kubernetes (context kind-x, namespace ns-x)" in text
    assert "- Egress verified: yes (4/4 repro runs)" in text
    assert "`claimcheck.k8s.KubernetesExecutor.run_repro`" in text and "local sandbox" not in text
    assert_json_matches_markdown(data, text)


def test_main_with_kubernetes_reports_egress_that_was_not_verified(tmp_path, monkeypatch):
    fake_kubernetes(monkeypatch, source=K8S_OPEN, status="unverified")
    cases, out, js = tmp_path / "cases", tmp_path / "EVAL.md", tmp_path / "eval.json"
    make_case(cases, "a", data=REPRO_CASE, report="it crashes")
    assert evaluate.main(["--cases", str(cases), "--runs", "1", "--executor", "kubernetes",
                          "--out", str(out), "--json", str(js)]) == 0
    data, text = json.loads(js.read_text()), out.read_text()
    assert data["executor"] == {"name": "kubernetes", "context": None, "namespace": "claimcheck", "egress_verified": False}
    assert "- Egress verified: no (0/1 repro runs)" in text
    assert data["cases"][0]["overall"]["label"] == "miss"


def test_main_uses_the_local_sandbox_by_default(tmp_path, monkeypatch):
    instances = fake_kubernetes(monkeypatch)
    calls = []
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("sandbox-exec", []))
    monkeypatch.setattr(sandbox, "run_repro", fake_run_repro("sandbox-exec: python3 poc.py", calls=calls))
    cases, out, js = tmp_path / "cases", tmp_path / "EVAL.md", tmp_path / "eval.json"
    make_case(cases, "a", data=REPRO_CASE, report="it crashes")
    assert evaluate.main(["--cases", str(cases), "--runs", "1", "--out", str(out), "--json", str(js)]) == 0
    assert instances == [] and len(calls) == 1
    data, text = json.loads(js.read_text()), out.read_text()
    assert data["executor"] == {"name": "local", "context": None, "namespace": None, "egress_verified": False}
    assert "- Executor: local sandbox" in text and "`claimcheck.sandbox.run_repro`" in text
    assert "--executor" not in text


def test_kubernetes_metrics_match_the_local_run(tmp_path, monkeypatch):
    fake_kubernetes(monkeypatch)
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("sandbox-exec", []))
    monkeypatch.setattr(sandbox, "run_repro", fake_run_repro("sandbox-exec: python3 poc.py"))
    cases = tmp_path / "cases"
    make_case(cases, "a", data=REPRO_CASE, report="it crashes")
    make_case(cases, "b")
    reports = []
    for executor in ("local", "kubernetes"):
        out, js = tmp_path / f"{executor}.md", tmp_path / f"{executor}.json"
        assert evaluate.main(["--cases", str(cases), "--runs", "2", "--executor", executor,
                              "--out", str(out), "--json", str(js)]) == 0
        reports.append((json.loads(js.read_text()), out.read_text()))
    assert reports[0][0]["metrics"] == reports[1][0]["metrics"]
    assert _metric_labels(reports[0][1]) == _metric_labels(reports[1][1])


@pytest.mark.parametrize("source", [None, K8S_OPEN, "sandbox-exec: echo 'egress blocked, verified'",
                                    "kubernetes[ns-x/default runtime] (egress not verified): " + K8S_VERIFIED])
def test_egress_report_requires_verified_executor_evidence(source):
    result = CaseResult(case({"repro": "unverified"}, "unverified", repro="python3 poc.py"),
                        [Run({"repro": "unverified"}, "unverified", repro_source=source)])
    assert evaluate.egress_verified([result]) is False


def test_egress_is_not_applicable_when_no_repros_ran(tmp_path, monkeypatch):
    instances = fake_kubernetes(monkeypatch)
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: pytest.fail("local isolation was checked"))
    cases, out, js = tmp_path / "cases", tmp_path / "EVAL.md", tmp_path / "eval.json"
    make_case(cases, "a")
    assert evaluate.main(["--cases", str(cases), "--executor", "kubernetes",
                          "--out", str(out), "--json", str(js)]) == 0
    assert instances[0].calls == []
    assert json.loads(js.read_text())["executor"]["egress_verified"] is None
    assert "- Egress verified: n/a (no repro runs)" in out.read_text()
    skipped = CaseResult(case({"repro": "confirmed"}, "supported", repro="python3 poc.py"), [], SKIP_NO_ISOLATION)
    assert evaluate.egress_verified([skipped]) is None


@pytest.mark.parametrize("error", [FileNotFoundError("kubectl unavailable"),
                                 subprocess.TimeoutExpired("kubectl", 120)])
def test_executor_setup_exception_leaves_repro_unverified(tmp_path, error):
    def unavailable(repo, command, expect):
        raise error

    loaded = evaluate.load_case(make_case(tmp_path, data=REPRO_CASE, report="it crashes"))
    results = evaluate.evaluate([loaded], runs=1, isolated=True, run_repro=unavailable)
    assert results[0].runs[0].claims == {"repro": "unverified"}
    assert results[0].runs[0].overall == "unverified"
    assert not results[0].wrong_confirmed and not results[0].wrong_refuted
    assert evaluate.egress_verified(results) is False


def test_kubernetes_with_model_keeps_executor_and_extraction_metadata(tmp_path, monkeypatch):
    instances = fake_kubernetes(monkeypatch)
    fake_model(monkeypatch, {"symbols": []})
    monkeypatch.setattr(evaluate, "ollama_unavailable", lambda model: "")
    cases, out, js = tmp_path / "cases", tmp_path / "EVAL.md", tmp_path / "eval.json"
    make_case(cases, "a", data=REPRO_CASE, report="it crashes")
    assert evaluate.main(["--cases", str(cases), "--runs", "1", "--executor", "kubernetes",
                          "--model", "gemma3:1b", "--out", str(out), "--json", str(js)]) == 0
    assert len(instances[0].calls) == 1
    data, text = json.loads(js.read_text()), out.read_text()
    assert data["cases"][0]["runs"][0]["model"] == "gemma3:1b"
    assert "use_model=True" in text and "`claimcheck.k8s.KubernetesExecutor.run_repro`" in text
    assert "local sandbox" not in text


def test_repro_source_does_not_change_verdict_repeatability():
    a = Run({"repro": "unverified"}, "unverified", repro_source=K8S_OPEN)
    b = Run({"repro": "unverified"}, "unverified", repro_source=K8S_VERIFIED)
    assert CaseResult(case({"repro": "unverified"}, "unverified", repro="python3 poc.py"), [a, b]).repeatable
