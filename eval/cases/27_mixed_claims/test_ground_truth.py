"""Execute the new fixtures so their truth does not depend on claimcheck's answers."""

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from claimcheck import evaluate

CASES = Path(__file__).resolve().parent.parent
NEW_CASES = [
    "20_python_subpackage",
    "21_reexported_name",
    "22_class_dot_method",
    "23_ts_generics",
    "24_pr_fix_with_tests",
    "25_repro_stderr_only",
    "26_repro_missing_dependency",
    "27_mixed_claims",
]


def copy_fixture(name: str, tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    shutil.copytree(CASES / name / "repo", repo)
    return repo


def run_python(repo: Path, source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", source], cwd=repo, capture_output=True,
                          text=True, timeout=30, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})


@pytest.mark.parametrize("name", NEW_CASES)
def test_new_case_has_consistent_expectations(name: str) -> None:
    case = evaluate.load_case(CASES / name)
    assert case.truth and case.claims


def test_multi_package_imports_and_dropped_row(tmp_path: Path) -> None:
    repo = copy_fixture("20_python_subpackage", tmp_path)
    done = run_python(repo, """
from inventory.io.readers import StockRow, read_stock_rows
from inventory.core.totals import total_quantity
from reporting.summary import build_summary

rows = read_stock_rows(["sku,quantity", "apple,2", "pear,3"])
assert len(rows) == 1 and isinstance(rows[0], StockRow)
assert total_quantity(rows) == 2
assert build_summary(rows) == {"rows": 1, "quantity": 2}
""")
    assert done.returncode == 0, done.stderr


def test_reexport_is_callable_and_keeps_bom(tmp_path: Path) -> None:
    repo = copy_fixture("21_reexported_name", tmp_path)
    done = run_python(repo, """
import tomlette
from tomlette.impl import parse

assert tomlette.parse is parse and callable(tomlette.parse)
assert tomlette.parse("\\ufefftitle = x") == {"\\ufefftitle": "x"}
""")
    assert done.returncode == 0, done.stderr


def test_class_methods_and_absent_validation(tmp_path: Path) -> None:
    repo = copy_fixture("22_class_dot_method", tmp_path)
    done = run_python(repo, """
from headers import HeaderParser

assert callable(HeaderParser.parse_header) and callable(HeaderParser.from_lines)
assert not hasattr(HeaderParser, "validate")
assert HeaderParser().parse_header("Accept: text/plain") == ("accept", "text/plain")
try:
    HeaderParser.from_lines(["Accept: text/plain", "broken"])
except ValueError:
    pass
else:
    raise AssertionError("a header without a colon should expose the reported crash")
""")
    assert done.returncode == 0, done.stderr


def test_typescript_generics_at_runtime(tmp_path: Path) -> None:
    repo = copy_fixture("23_ts_generics", tmp_path)
    node = shutil.which("node")
    if not node:
        pytest.skip("needs Node with --experimental-transform-types")
    source = (repo / "src" / "cache.ts").read_text()
    assert "export type Result<T, E = Error>" in source
    done = subprocess.run([node, "--experimental-transform-types", "--input-type=module", "-e", """
import assert from 'node:assert/strict';
import * as cache from './src/cache.ts';

for (const name of ['LruCache', 'memoize', 'mapValues', 'InMemoryRepo']) {
  assert.equal(typeof cache[name], 'function');
}
assert.equal(typeof cache.InMemoryRepo.prototype.findById, 'function');
assert.equal('Result' in cache, false);
assert.equal(cache.LruCache.prototype.evictAll, undefined);
assert.equal(cache.InMemoryRepo.prototype.evictAll, undefined);
const lru = new cache.LruCache(2);
lru.set('a', {id: 'a'});
lru.set('b', {id: 'b'});
lru.get('a');
lru.set('c', {id: 'c'});
assert.equal(lru.get('b'), undefined);
assert.equal(lru.get('a').id, 'a');
let calls = 0;
const memo = cache.memoize((values) => { calls++; return values.length; });
assert.equal(memo([1, 2]), 2);
assert.equal(memo([1, 2]), 2);
assert.equal(calls, 1);
const key = Symbol('hidden');
assert.deepEqual(cache.mapValues({visible: 2, [key]: 3}, (x) => x * 2), {visible: 4});
assert.equal((await new cache.InMemoryRepo().findById('missing')).ok, false);
"""], cwd=repo, capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


def test_pr_regressions_fail_before_fix_and_pass_after(tmp_path: Path) -> None:
    """A test path in a diff should correspond to tests that expose the stated bug."""
    name = "24_pr_fix_with_tests"
    repo = copy_fixture(name, tmp_path)
    diff = (CASES / name / "pr.diff").read_text()
    added_tests = diff.split("+++ b/tests/test_pages.py\n", 1)[1].splitlines()[1:]
    tests = repo / "tests" / "test_pages.py"
    tests.parent.mkdir()
    tests.write_text("\n".join(line[1:] for line in added_tests if line.startswith("+")) + "\n")
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests)]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    before = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=30, env=env)
    assert before.returncode == 1 and "2 failed, 3 passed" in before.stdout, before.stdout + before.stderr
    tests.unlink()
    tests.parent.rmdir()
    patch = subprocess.run(["patch", "-p1", "--batch"], input=diff, cwd=repo,
                           capture_output=True, text=True, timeout=30)
    assert patch.returncode == 0, patch.stdout + patch.stderr
    after = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=30, env=env)
    assert after.returncode == 0 and "5 passed" in after.stdout, after.stdout + after.stderr


def test_flaky_looking_repro_fails_consistently_on_stderr(tmp_path: Path) -> None:
    repo = copy_fixture("25_repro_stderr_only", tmp_path)
    for _ in range(3):
        done = run_python(repo, "import runpy; runpy.run_path('poc.py', run_name='__main__')")
        assert done.returncode == 3
        assert "block 1: 0 bytes" in done.stdout
        assert "checksum mismatch" not in done.stdout
        assert "checksum mismatch in block 1" in done.stderr


def test_missing_dependency_prevents_the_bug_from_running(tmp_path: Path) -> None:
    repo = copy_fixture("26_repro_missing_dependency", tmp_path)
    done = subprocess.run([sys.executable, "-S", "poc.py"], cwd=repo, capture_output=True,
                          text=True, timeout=30, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 1
    assert "ModuleNotFoundError: No module named 'rapidfuzz'" in done.stderr
    assert "ValueError" not in done.stderr
    with_dependency = run_python(repo, """
import runpy
import sys
from types import ModuleType, SimpleNamespace

module = ModuleType("rapidfuzz")
module.fuzz = SimpleNamespace(ratio=lambda left, right: 100)
sys.modules["rapidfuzz"] = module
runpy.run_path("poc.py", run_name="__main__")
""")
    assert with_dependency.returncode == 1 and "ValueError: max()" in with_dependency.stderr


def test_mixed_true_false_and_unresolved_claims(tmp_path: Path) -> None:
    repo = copy_fixture("27_mixed_claims", tmp_path)
    done = run_python(repo, """
from accounts import identities
from accounts.identities import AccountRecord, normalize_handle

assert callable(normalize_handle) and callable(AccountRecord)
assert normalize_handle("Alice/TEAM") == "alice/TEAM"
assert not hasattr(identities, "strip_suffix")
assert not hasattr(identities, "send_welcome")
record = AccountRecord("Alice/TEAM")
assert record.address == "alice/TEAM"
record.created()
calls = []
AccountRecord("Alice/TEAM", {"send_welcome": calls.append}).created()
assert calls == ["alice/TEAM"]
""")
    assert done.returncode == 0, done.stderr
    repro = run_python(repo, "import runpy; runpy.run_path('poc.py', run_name='__main__')")
    assert repro.returncode == 1 and "AssertionError: alice/TEAM" in repro.stderr
    data = json.loads((CASES / "27_mixed_claims" / "case.json").read_text())
    assert set(data["expected"]["claims"].values()) == {"confirmed", "refuted", "unverified"}


def test_saved_baseline_matches_truth_and_report() -> None:
    """Keep the saved measurements and visible misses tied to the unchanged truth files."""
    root = CASES.parent.parent
    baseline = json.loads((root / "eval" / "baseline.json").read_text())
    cases = evaluate.load_cases(CASES)
    assert len(cases) >= 27
    assert baseline["meta"]["runs"] == "3"
    assert [row["name"] for row in baseline["results"]] == [case.name for case in cases]
    results = []
    misses = []
    for case, row in zip(cases, baseline["results"]):
        assert row["expected"] == {"claims": case.claims, "overall": case.overall}
        assert not row["skipped"] and len(row["runs"]) == 3
        result = evaluate.CaseResult(case, [evaluate.Run(**run) for run in row["runs"]])
        results.append(result)
        for key in result.claim_keys():
            if not result.claim_correct(key):
                misses.append({"case": case.name, "claim": key, "expected": result.expected(key),
                               "got": result.got(key)})
        if not result.verdict_correct:
            misses.append({"case": case.name, "claim": "verdict", "expected": case.overall,
                           "got": [run.overall for run in result.runs]})
    summary = evaluate.summarize(results)
    assert baseline["summary"] == asdict(summary)
    assert baseline["misses"] == misses
    assert (summary.wrong_confirmed, summary.wrong_refuted, summary.skipped) == (0, 0, 0)
    report = (root / "docs" / "EVALUATION.md").read_text()
    assert report.startswith(evaluate.to_markdown(results, baseline["meta"]))
