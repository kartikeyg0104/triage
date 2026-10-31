"""tools/check_eval_baseline.py: the CI gate that fails when the evaluation gets worse."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "check_eval_baseline.py"
BASELINE = ROOT / "eval" / "baseline.json"


def _load():
    spec = importlib.util.spec_from_file_location("check_eval_baseline", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load()


def results(cases=19, scored=19, claims_total=41, claims_correct=41, verdicts_correct=19,
            wrong_confirmed=0, wrong_refuted=0):
    """The fields of a claimcheck.evaluate --json file the check reads, plus a few it ignores."""
    return {
        "schema_version": 1,
        "model": None,
        "skip_reason": None,
        "runs_per_case": 2,
        "metrics": {"cases": cases, "scored": scored, "skipped": cases - scored, "claims_total": claims_total,
                    "claims_correct": claims_correct, "verdicts_correct": verdicts_correct,
                    "wrong_confirmed": wrong_confirmed, "wrong_refuted": wrong_refuted,
                    "unverified": 9, "expected_unverified": 9, "repeatable": scored},
        "timing": {"total_seconds": 12.5, "runs": 2 * scored, "slowest": []},
        "cases": [],
    }


def baseline(cases=19, scored=19, verdict_accuracy=1.0, claim_accuracy=1.0):
    return {"schema_version": 1, "cases": cases, "scored": scored,
            "verdict_accuracy": verdict_accuracy, "claim_accuracy": claim_accuracy}


def run(checker, tmp_path, result, base, capsys):
    result_path, base_path = tmp_path / "eval-results.json", tmp_path / "baseline.json"
    result_path.write_text(result if isinstance(result, str) else json.dumps(result))
    base_path.write_text(base if isinstance(base, str) else json.dumps(base))
    code = checker.main([str(result_path), str(base_path)])
    out = capsys.readouterr()
    return code, out.out + out.err


# --- passes -----------------------------------------------------------------------------------

def test_passes_when_equal_to_baseline(checker, tmp_path, capsys):
    code, output = run(checker, tmp_path, results(), baseline(), capsys)
    assert code == 0, output
    assert "ok" in output.lower()


def test_passes_when_better_than_baseline(checker, tmp_path, capsys):
    base = baseline(cases=18, scored=18, verdict_accuracy=17 / 18, claim_accuracy=38 / 40)
    code, output = run(checker, tmp_path, results(), base, capsys)
    assert code == 0, output


def test_passes_when_accuracy_equals_a_non_round_baseline(checker, tmp_path, capsys):
    base = baseline(verdict_accuracy=18 / 19, claim_accuracy=40 / 41)
    code, output = run(checker, tmp_path, results(claims_correct=40, verdicts_correct=18), base, capsys)
    assert code == 0, output


# --- fails ------------------------------------------------------------------------------------

def test_fails_on_wrong_confirmed(checker, tmp_path, capsys):
    code, output = run(checker, tmp_path, results(wrong_confirmed=1), baseline(verdict_accuracy=0.5), capsys)
    assert code == 1
    assert "wrong confirmed" in output.lower()


def test_fails_on_wrong_refuted(checker, tmp_path, capsys):
    code, output = run(checker, tmp_path, results(wrong_refuted=2), baseline(verdict_accuracy=0.5), capsys)
    assert code == 1
    assert "wrong refuted" in output.lower()


def test_fails_on_verdict_accuracy_drop(checker, tmp_path, capsys):
    code, output = run(checker, tmp_path, results(verdicts_correct=18), baseline(), capsys)
    assert code == 1
    assert "verdict accuracy" in output.lower()
    assert "18/19" in output


def test_fails_on_claim_accuracy_drop(checker, tmp_path, capsys):
    code, output = run(checker, tmp_path, results(claims_correct=40), baseline(), capsys)
    assert code == 1
    assert "per-claim accuracy" in output.lower()
    assert "40/41" in output


def test_fails_on_fewer_cases_than_baseline(checker, tmp_path, capsys):
    code, output = run(checker, tmp_path, results(cases=18, scored=18, claims_total=39, claims_correct=39,
                                                  verdicts_correct=18), baseline(), capsys)
    assert code == 1
    assert "18 cases" in output and "19" in output


def test_fails_on_fewer_scored_cases_than_baseline(checker, tmp_path, capsys):
    # Cases skipped for lack of sandbox isolation drop out of every rate, so they could hide a regression.
    code, output = run(checker, tmp_path, results(scored=15, claims_total=35, claims_correct=35,
                                                  verdicts_correct=15), baseline(), capsys)
    assert code == 1
    assert "scored" in output.lower()


def test_reports_every_failure_at_once(checker, tmp_path, capsys):
    code, output = run(checker, tmp_path, results(wrong_confirmed=1, wrong_refuted=1, verdicts_correct=17), baseline(),
                       capsys)
    assert code == 1
    lowered = output.lower()
    assert "wrong confirmed" in lowered and "wrong refuted" in lowered and "verdict accuracy" in lowered


# --- malformed input --------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "{not json", "[1, 2]", '"just a string"'])
def test_fails_on_malformed_results_json(checker, tmp_path, capsys, text):
    code, output = run(checker, tmp_path, text, baseline(), capsys)
    assert code == 2
    assert "eval-results.json" in output


def test_fails_on_results_missing_metrics(checker, tmp_path, capsys):
    broken = results()
    del broken["metrics"]["wrong_refuted"]
    code, output = run(checker, tmp_path, broken, baseline(), capsys)
    assert code == 2
    assert "wrong_refuted" in output


def test_fails_on_results_with_non_integer_metric(checker, tmp_path, capsys):
    broken = results()
    broken["metrics"]["claims_correct"] = "41"
    code, output = run(checker, tmp_path, broken, baseline(), capsys)
    assert code == 2
    assert "claims_correct" in output


def test_fails_on_boolean_metric(checker, tmp_path, capsys):
    broken = results()
    broken["metrics"]["wrong_confirmed"] = False
    code, output = run(checker, tmp_path, broken, baseline(), capsys)
    assert code == 2
    assert "wrong_confirmed" in output


def test_fails_on_unknown_schema_version(checker, tmp_path, capsys):
    broken = results()
    broken["schema_version"] = 2
    code, output = run(checker, tmp_path, broken, baseline(), capsys)
    assert code == 2
    assert "schema_version" in output


def test_fails_on_skipped_run(checker, tmp_path, capsys):
    skipped = results(scored=0, claims_total=0, claims_correct=0, verdicts_correct=0)
    skipped["skip_reason"] = "Ollama not reachable"
    code, output = run(checker, tmp_path, skipped, baseline(), capsys)
    assert code == 1
    assert "Ollama not reachable" in output


@pytest.mark.parametrize("text", ["{not json", "[]", json.dumps({"cases": 19}),
                                  json.dumps(baseline(claim_accuracy="1.0")),
                                  json.dumps(baseline(verdict_accuracy=1.5))])
def test_fails_on_malformed_baseline(checker, tmp_path, capsys, text):
    code, output = run(checker, tmp_path, results(), text, capsys)
    assert code == 2
    assert "baseline.json" in output


def test_fails_on_missing_file(checker, tmp_path, capsys):
    base_path = tmp_path / "baseline.json"
    base_path.write_text(json.dumps(baseline()))
    code = checker.main([str(tmp_path / "absent.json"), str(base_path)])
    output = capsys.readouterr().err
    assert code == 2
    assert "absent.json" in output


# --- the real files ---------------------------------------------------------------------------

def test_committed_baseline_is_valid_and_has_no_timings(checker):
    data = json.loads(BASELINE.read_text())
    assert checker.load_baseline(BASELINE) is not None
    assert not any("second" in key or "timing" in key for key in data)
    assert data["cases"] == len([p for p in (ROOT / "eval" / "cases").iterdir() if p.is_dir()])


def test_script_runs_from_the_command_line(tmp_path):
    result_path = tmp_path / "eval-results.json"
    result_path.write_text(json.dumps(results(cases=10_000, scored=10_000, claims_total=10, claims_correct=10,
                                              verdicts_correct=10_000)))
    done = subprocess.run([sys.executable, str(SCRIPT), str(result_path), str(BASELINE)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr

    result_path.write_text(json.dumps(results(cases=10_000, scored=10_000, claims_total=10, claims_correct=10,
                                              verdicts_correct=10_000, wrong_refuted=1)))
    done = subprocess.run([sys.executable, str(SCRIPT), str(result_path), str(BASELINE)],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 1
    assert "wrong refuted" in (done.stdout + done.stderr).lower()


def test_script_rejects_wrong_arguments():
    done = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 2
