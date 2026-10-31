"""Save the measured baseline and its methodology together without altering ground truth."""

import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from claimcheck import evaluate, sandbox

ROOT = Path(__file__).resolve().parents[3]

NOTES = """
## Expanded report shapes (issue #84)

Eight new cases extend the original 19. Their expectations were written from the fixture
source; the runtime truth tests check imports, behavior, absent attributes, and actual
process output independently of claimcheck's verdicts.

| Case | Report shape | Independent truth check |
|---|---|---|
| 20_python_subpackage | Two top-level Python packages, nested modules and cross-package imports | Import all four names; parse two rows and observe the missing last row in the downstream summary |
| 21_reexported_name | A public function re-exported from a package root | Public and implementation functions are identical; the BOM remains in the parsed key |
| 22_class_dot_method | Instance and class methods written as Class.method() | Both methods are callable; a malformed header raises ValueError; validate is absent |
| 23_ts_generics | Generic classes, type aliases, functions, arrow functions and nested return types | Execute the TypeScript with Node; check exports, methods, cache behavior and the absent evictAll |
| 24_pr_fix_with_tests | A bug fix touching source, changelog and parametrized regression tests | Extract the five tests from the diff: two fail before the fix; patch applies; all five pass afterward |
| 25_repro_stderr_only | An apparently flaky CI failure whose diagnostic is on stderr | Three executions all exit 3 with the checksum failure on stderr and only progress on stdout |
| 26_repro_missing_dependency | A repro requiring a declared third-party dependency | Missing rapidfuzz prevents the function from running; a separate import stub exposes the empty-list failure |
| 27_mixed_claims | Several claims of mixed truth in one report | Check an existing function and class, an absent function, a caller-supplied hook, and the failing assertion |

The first complete unit-test run was red because case 27 had no case.json (1 failure,
30 errors). The new runtime truth tests were also run before completing the fixtures:
3 failed and 13 passed. They identified the missing second top-level package and the
unfinished mixed-claims fixture. The saved-baseline test failed before baseline.json
was created. No production code was changed.

### Reproduce the measurements

Use a Python environment with claimcheck installed from this checkout and pytest available.
Put that environment's bin directory first on PATH so sandbox commands using python3 use
the same environment. Run on a machine with working sandbox-exec or unshare network isolation.

```sh
unset CLAIMCHECK_K8S_CONTEXT
python -m pytest -q -rs unit_tests
python eval/cases/27_mixed_claims/record_baseline.py
python -m pytest -q -rs eval/cases/27_mixed_claims/test_ground_truth.py
```

record_baseline.py runs the real regex pipeline and local sandbox three times per case,
then writes eval/baseline.json and the generated report above plus these methodology notes.
The JSON contains the expected claims, all individual runs, summary metrics and every miss.
The truth tests validate the saved JSON against case.json and the generated report, as well
as executing the eight fixtures. They leave the fixture repositories unchanged.

### Limits and the observed miss

Case 26 exposes an extraction miss: the quoted ValueError message contains max(), which
the regex extractor turns into an extra symbol:max claim. The report makes no repository
symbol claim about this Python builtin. The tool returns unverified because max is only
referenced in the source. This extra claim is counted as a miss in the generated table and
baseline, while the actual repro correctly remains unverified for the missing dependency.
The original report and expected claims are retained; this miss is not relabeled as correct.

These are synthetic repos modeled on common reports, not a representative sample of real
projects. Symbol confirmations establish that names are defined; they do not establish all
of a report's narrative bug assertions. Case 23 deliberately includes incorrect behavioral
allegations about existing generic functions, and its truth tests show the actual behavior.
Class.method() extraction checks the method suffix, not class ownership; a TypeScript type
alias exists in source but is erased at runtime. PR test confirmation establishes that the
diff changes tests, while the separate truth test establishes that those tests expose the
bug and pass after the fix. The flaky-looking repro is deterministic and does not measure
intermittent failures. Case 26 requires an environment without rapidfuzz; the import stub
does not validate that library. This report measures regex extraction, not model extraction
or Kubernetes execution. Kubernetes and image integration tests require their own setup.
"""


def main() -> int:
    isolation = sandbox.detect_isolation()
    if isolation is None:
        raise SystemExit("network isolation is required to record every reproduction case")
    cases = evaluate.load_cases(ROOT / "eval" / "cases")
    meta = evaluate.machine_info(3, isolation)
    meta["python_executable"] = sys.executable
    meta["repro_python"] = shutil.which("python3") or "not found"
    results = evaluate.evaluate(cases, runs=3, isolated=True)
    summary = evaluate.summarize(results)
    misses = []
    for result in results:
        for key in result.claim_keys():
            if not result.claim_correct(key):
                misses.append({"case": result.case.name, "claim": key, "expected": result.expected(key),
                               "got": result.got(key)})
        if not result.verdict_correct:
            misses.append({"case": result.case.name, "claim": "verdict", "expected": result.case.overall,
                           "got": [run.overall for run in result.runs]})
    baseline = {
        "schema_version": 1,
        "meta": meta,
        "summary": asdict(summary),
        "misses": misses,
        "results": [
            {"name": result.case.name,
             "expected": {"claims": result.case.claims, "overall": result.case.overall},
             "runs": [asdict(run) for run in result.runs], "skipped": result.skipped}
            for result in results
        ],
    }
    (ROOT / "eval" / "baseline.json").write_text(json.dumps(baseline, indent=2) + "\n")
    (ROOT / "docs" / "EVALUATION.md").write_text(evaluate.to_markdown(results, meta) + NOTES)
    print(f"{summary.cases} cases, {summary.claims_correct}/{summary.claims_total} claims, "
          f"{summary.verdicts_correct}/{summary.scored} verdicts; "
          f"wrong confirmed {summary.wrong_confirmed}, wrong refuted {summary.wrong_refuted}, "
          f"{len(misses)} misses, {summary.skipped} skipped")
    return int(bool(summary.wrong_confirmed or summary.wrong_refuted))


if __name__ == "__main__":
    sys.exit(main())
