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
    assert "not defined" in evidence.detail


def test_existing_symbol_is_confirmed_with_location(tmp_path):
    evidence = check_symbol(make_repo(tmp_path), "parse_url")
    assert evidence.status is Status.CONFIRMED
    assert evidence.source == "pkg/urls.py:1"


DIFF_NO_TEST = "--- a/cli.py\n+++ b/cli.py\n@@ -1 +1 @@\n-a\n+b\n"
DIFF_WITH_TEST = DIFF_NO_TEST + "--- a/tests/test_cli.py\n+++ b/tests/test_cli.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_changed_files_parses_diff():
    assert changed_files(DIFF_WITH_TEST) == ["cli.py", "tests/test_cli.py"]


def test_pr_without_tests_is_refuted():
    evidence = check_pr_tests(DIFF_NO_TEST)
    assert evidence.status is Status.REFUTED
    assert "cli.py" in evidence.detail


def test_pr_with_tests_is_confirmed():
    assert check_pr_tests(DIFF_WITH_TEST).status is Status.CONFIRMED


def test_docs_only_pr_is_unverified():
    assert check_pr_tests("--- a/README.md\n+++ b/README.md\n").status is Status.UNVERIFIED
