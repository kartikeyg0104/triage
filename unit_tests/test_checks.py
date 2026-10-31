from pathlib import Path

import pytest

from claimcheck.checks import changed_files, check_pr_tests, check_symbol, find_symbol
from claimcheck.models import Status


def make_repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir(parents=True)
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


# --- a repo that itself lives under a directory named like a skip dir -----------------------

def test_repo_under_a_build_directory_is_still_scanned(tmp_path):
    repo = make_repo(tmp_path / "build" / "repo")
    evidence = check_symbol(repo, "parse_url")
    assert evidence.status is Status.CONFIRMED
    assert evidence.source == "pkg/urls.py:1"
    assert find_symbol(repo, "ghost") == []  # node_modules inside the repo is still skipped


def test_refuted_count_is_of_files_inside_the_repo_under_a_skip_dir(tmp_path):
    repo = make_repo(tmp_path / "venv" / "dist" / "repo")
    assert "any of 2 source files" in check_symbol(repo, "parse_url_v2").detail


# --- names created from strings at runtime ------------------------------------------------

DYNAMIC = [
    ("plugins.py", "import sys\nfor fmt in ('csv',):\n    setattr(sys.modules[__name__], 'export_' + fmt, print)\n", 3),
    ("plugins.py", "for fmt in ('csv',):\n    globals()['export_' + fmt] = print\n", 2),
    ("plugins.py", "import importlib\n\n\ndef __getattr__(name):\n    return importlib.import_module(name)\n", 4),
    ("plugins.py", "SOURCE = 'def export_' + 'csv(): pass'\nexec(SOURCE)\n", 2),
    ("formats.js", "for (const s of ['Csv']) {\n  module.exports['to' + s] = () => 1;\n}\n", 2),
    ("formats.js", "const names = ['Csv'];\nnames.forEach((s) => { exports['to' + s] = () => 1; });\n", 2),
    ("formats.js", "const api = {};\n['Csv'].forEach((s) => Object.defineProperty(api, 'to' + s, { value: 1 }));\n", 2),
]


@pytest.mark.parametrize("filename,source,line", DYNAMIC)
def test_name_with_a_dynamic_definition_mechanism_is_unverified(tmp_path, filename, source, line):
    (tmp_path / filename).write_text(source)
    evidence = check_symbol(tmp_path, "export_tsv")
    assert evidence.status is Status.UNVERIFIED
    assert evidence.basis == "inferred"
    assert "dynamically" in evidence.detail
    assert evidence.source == f"{filename}:{line}"


def test_dynamic_mechanism_only_in_a_comment_does_not_count(tmp_path):
    (tmp_path / "a.py").write_text("# do not use setattr(module, name, fn) or exec(code) here\ndef f():\n    pass\n")
    (tmp_path / "b.js").write_text("// module.exports['to' + s] = fn was removed\nfunction g() {}\n")
    assert check_symbol(tmp_path, "export_tsv").status is Status.REFUTED


def test_regex_exec_in_js_is_not_a_dynamic_definition(tmp_path):
    (tmp_path / "match.js").write_text("const m = /a+/.exec(text);\n")
    assert check_symbol(tmp_path, "toTsv").status is Status.REFUTED


# --- mentions in comments are not references ------------------------------------------------

def test_name_only_in_a_python_comment_is_refuted(tmp_path):
    (tmp_path / "auth.py").write_text("# validate_token was removed in 2.0\ndef check_session(t):\n    return t  # not validate_token\n")
    evidence = check_symbol(tmp_path, "validate_token")
    assert evidence.status is Status.REFUTED
    assert "does not appear" in evidence.detail


def test_name_only_in_js_comments_is_refuted(tmp_path):
    (tmp_path / "slug.js").write_text("// truncateSlg was renamed\nconst a = 1; /* truncateSlg */ const b = 2;\n")
    assert check_symbol(tmp_path, "truncateSlg").status is Status.REFUTED


@pytest.mark.parametrize("filename,source,line", [
    ("a.py", "COLOR = '#fff'  # white\nLABEL = '# dyn_handler'\n", 2),
    ("a.py", 'TEMPLATE = """\n# dyn_handler\n"""\n', 2),
    ("a.js", "const url = 'http://x/dyn_handler';\n", 1),
    ("a.js", "/* old */ register(dyn_handler);\n", 1),
])
def test_mentions_in_code_or_strings_still_count(tmp_path, filename, source, line):
    (tmp_path / filename).write_text(source)
    evidence = check_symbol(tmp_path, "dyn_handler")
    assert evidence.status is Status.UNVERIFIED
    assert evidence.source == f"{filename}:{line}"


def test_commented_out_js_definition_is_not_a_definition(tmp_path):
    (tmp_path / "slug.js").write_text("// function truncateSlg(s) { return s; }\n")
    assert find_symbol(tmp_path, "truncateSlg") == []
