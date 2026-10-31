from pathlib import Path

from claimcheck.checks import changed_files, check_pr_tests, check_symbol, find_symbol
from claimcheck.models import Status


def make_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "urls.py").write_text("def parse_url(s):\n    return s\n\nclass Router:\n    pass\n")
    (tmp_path / "web.js").write_text("function fetchAll() {}\nconst helper = () => 1;\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("function ghost() {}\n")
    return tmp_path


def test_finds_python_function_and_class(tmp_path):
    repo = make_repo(tmp_path)
    assert find_symbol(repo, "parse_url")[0][1] == 1
    assert find_symbol(repo, "Router")[0][1] == 4


def test_finds_js_function_and_const(tmp_path):
    repo = make_repo(tmp_path)
    assert find_symbol(repo, "fetchAll")
    assert find_symbol(repo, "helper")


def test_ignores_vendored_dirs(tmp_path):
    assert find_symbol(make_repo(tmp_path), "ghost") == []


def test_missing_symbol_is_refuted(tmp_path):
    evidence = check_symbol(make_repo(tmp_path), "parse_url_v2")
    assert evidence.status is Status.REFUTED
    assert "does not appear" in evidence.detail
    assert evidence.basis == "inferred"


def test_existing_symbol_is_confirmed_with_location(tmp_path):
    evidence = check_symbol(make_repo(tmp_path), "parse_url")
    assert evidence.status is Status.CONFIRMED
    assert evidence.source == "pkg/urls.py:1"


DIFF_NO_TEST = "--- a/cli.py\n+++ b/cli.py\n@@ -1 +1 @@\n-a\n+b\n"
DIFF_WITH_TEST = DIFF_NO_TEST + "--- a/tests/test_cli.py\n+++ b/tests/test_cli.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_changed_files_parses_diff():
    assert changed_files(DIFF_WITH_TEST) == ["cli.py", "tests/test_cli.py"]


def test_pr_without_test_changes_is_unverified_not_refuted():
    evidence = check_pr_tests(DIFF_NO_TEST)
    assert evidence.status is Status.UNVERIFIED
    assert evidence.basis == "inferred"
    assert "existing tests may still cover it" in evidence.detail
    assert "cli.py" in evidence.detail


def test_pr_with_tests_is_confirmed():
    assert check_pr_tests(DIFF_WITH_TEST).status is Status.CONFIRMED


def test_docs_only_pr_is_unverified():
    assert check_pr_tests("--- a/README.md\n+++ b/README.md\n").status is Status.UNVERIFIED


def test_name_that_is_only_referenced_is_unverified(tmp_path):
    (tmp_path / "plugins.py").write_text("import sys\nsetattr(sys.modules[__name__], 'dyn_handler', lambda: 1)\nNAMES = ['dyn_handler']\n")
    evidence = check_symbol(tmp_path, "dyn_handler")
    assert evidence.status is Status.UNVERIFIED
    assert "dynamically" in evidence.detail
    assert evidence.source == "plugins.py:2"


def test_confirmed_symbol_is_observed(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    pass\n")
    assert check_symbol(tmp_path, "f").basis == "observed"
