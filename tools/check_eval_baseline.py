"""Fail when a claimcheck evaluation is worse than the committed baseline.

    python tools/check_eval_baseline.py eval-results.json eval/baseline.json

eval-results.json is what `python -m claimcheck.evaluate --json` writes (schema version 1, see
claimcheck/evaluate.py). eval/baseline.json holds what a real run measured:

    schema_version     1
    cases, scored      how many cases there were and how many were scored (not skipped)
    verdict_accuracy   verdicts_correct / scored, from 0 to 1
    claim_accuracy     claims_correct / claims_total, from 0 to 1

The check fails (exit 1) if any case got a wrong confirmed or wrong refuted answer, if there are
fewer cases or fewer scored cases than the baseline (skipped cases drop out of every rate and could
hide a regression), or if either accuracy is below the baseline. Unreadable or malformed input
exits 2. Timings are not compared: they measure the machine, not the tool.

To raise the baseline after an improvement, copy the numbers this prints from a real run.
"""

import json
import sys
from pathlib import Path

SCHEMA_VERSION = 1
TOLERANCE = 1e-9  # the same counts give the same float; this only absorbs rounding in a hand-written baseline
METRICS = ("cases", "scored", "claims_total", "claims_correct", "verdicts_correct", "wrong_confirmed", "wrong_refuted")


class InputError(ValueError):
    """A results or baseline file that cannot be read as what it claims to be."""


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except OSError as error:
        raise InputError(f"{path}: cannot read ({error.strerror or error})") from None
    except ValueError as error:
        raise InputError(f"{path}: not valid JSON ({error})") from None
    if not isinstance(data, dict):
        raise InputError(f"{path}: must be a JSON object, not {type(data).__name__}")
    return data


def _count(data: dict, key: str, path: Path, where: str = "") -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputError(f"{path}: {where}{key} must be a non-negative integer, not {value!r}")
    return value


def _schema(data: dict, path: Path) -> None:
    if data.get("schema_version") != SCHEMA_VERSION:
        raise InputError(f"{path}: schema_version must be {SCHEMA_VERSION}, not {data.get('schema_version')!r}")


def load_results(path: Path) -> dict:
    data = _read(path)
    _schema(data, path)
    metrics = data.get("metrics")
    if not isinstance(metrics, dict):
        raise InputError(f"{path}: needs a 'metrics' object")
    counts = {key: _count(metrics, key, path, "metrics.") for key in METRICS}
    if counts["scored"] > counts["cases"] or counts["claims_correct"] > counts["claims_total"] \
            or counts["verdicts_correct"] > counts["scored"]:
        raise InputError(f"{path}: metrics do not add up: {counts}")
    skip = data.get("skip_reason")
    if skip is not None and not isinstance(skip, str):
        raise InputError(f"{path}: skip_reason must be a string or null, not {skip!r}")
    return counts | {"skip_reason": skip}


def load_baseline(path: Path) -> dict:
    data = _read(path)
    _schema(data, path)
    base = {key: _count(data, key, path) for key in ("cases", "scored")}
    for key in ("verdict_accuracy", "claim_accuracy"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise InputError(f"{path}: {key} must be a number from 0 to 1, not {value!r}")
        base[key] = float(value)
    return base


def _rate(part: int, whole: int) -> float:
    return part / whole if whole else 0.0


def _shown(part: int, whole: int) -> str:
    return f"{part}/{whole} ({_rate(part, whole):.2%})"


def compare(results: dict, base: dict) -> list[str]:
    """Every way the results are worse than the baseline; empty if they are not."""
    problems = []
    if results["skip_reason"]:
        problems.append(f"the whole evaluation was skipped: {results['skip_reason']}")
    if results["wrong_confirmed"]:
        problems.append(f"{results['wrong_confirmed']} case(s) with a wrong confirmed answer (must be 0)")
    if results["wrong_refuted"]:
        problems.append(f"{results['wrong_refuted']} case(s) with a wrong refuted answer (must be 0)")
    if results["cases"] < base["cases"]:
        problems.append(f"{results['cases']} cases, fewer than the baseline's {base['cases']}")
    if results["scored"] < base["scored"]:
        problems.append(f"{results['scored']} scored cases, fewer than the baseline's {base['scored']} "
                        f"({results['cases'] - results['scored']} skipped)")
    verdicts = _rate(results["verdicts_correct"], results["scored"])
    if verdicts + TOLERANCE < base["verdict_accuracy"]:
        problems.append(f"verdict accuracy {_shown(results['verdicts_correct'], results['scored'])} is below "
                        f"the baseline {base['verdict_accuracy']:.2%}")
    claims = _rate(results["claims_correct"], results["claims_total"])
    if claims + TOLERANCE < base["claim_accuracy"]:
        problems.append(f"per-claim accuracy {_shown(results['claims_correct'], results['claims_total'])} is below "
                        f"the baseline {base['claim_accuracy']:.2%}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: python tools/check_eval_baseline.py EVAL_RESULTS_JSON BASELINE_JSON", file=sys.stderr)
        return 2
    results_path, base_path = map(Path, args)
    try:
        results, base = load_results(results_path), load_baseline(base_path)
    except InputError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    problems = compare(results, base)
    measured = (f"{results['cases']} cases ({results['scored']} scored), "
                f"verdict accuracy {_shown(results['verdicts_correct'], results['scored'])}, "
                f"per-claim accuracy {_shown(results['claims_correct'], results['claims_total'])}, "
                f"wrong confirmed {results['wrong_confirmed']}, wrong refuted {results['wrong_refuted']}")
    if problems:
        print(f"Evaluation is worse than {base_path}:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"Measured: {measured}", file=sys.stderr)
        return 1
    print(f"Evaluation ok against {base_path}: {measured}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
