"""Version metadata can establish a match, but cannot disprove an older report."""

import json
from pathlib import Path

import pytest

from claimcheck import cli, extract, report
from claimcheck.models import Status


MANIFESTS = [
    ("pyproject.toml", '[project]\nname = "sample"\nversion = "2.4.1"\n'),
    ("setup.cfg", "[metadata]\nname = sample\nversion = 2.4.1\n"),
    ("package.json", '{"name": "sample", "version": "2.4.1"}\n'),
    ("src/sample/__init__.py", '__version__ = "2.4.1"\n'),
]


def write(repo: Path, name: str, text: str) -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def check(repo: Path, text: str):
    from claimcheck.versions import check_versions

    return check_versions(repo, text)


@pytest.mark.parametrize("name,content", MANIFESTS)
@pytest.mark.parametrize("version,status", [("2.4.1", Status.CONFIRMED), ("2.3.0", Status.UNVERIFIED)])
def test_each_manifest_compares_the_report_version(tmp_path, name, content, version, status):
    write(tmp_path, name, content)
    rows = check(tmp_path, f"Reproduced on {version}.")
    assert len(rows) == 1
    evidence = rows[0]
    assert evidence.claim.kind == "version"
    assert evidence.claim.target == version
    assert evidence.status is status
    assert evidence.basis == "observed"
    assert "2.4.1" in evidence.detail and version in evidence.detail
    assert evidence.source.split(":")[0] == name
    if status is Status.CONFIRMED:
        assert "reproduction" in evidence.detail.lower()


@pytest.mark.parametrize("text", ["", "It crashes.", "Reproduced on 2.4.1."])
def test_no_declared_version_adds_no_row(tmp_path, text):
    assert check(tmp_path, text) == []


def test_no_report_version_adds_no_row(tmp_path):
    write(tmp_path, *MANIFESTS[0])
    assert check(tmp_path, "It crashes.") == []


@pytest.mark.parametrize("value", ["2.4.1", "v2.4.1 (abc1234)", "```text\n2.4.1\n```"])
def test_issue_form_version_field(tmp_path, value):
    write(tmp_path, *MANIFESTS[1])
    rows = check(tmp_path, f"### Version or commit\n\n{value}\n\n### Description\n\nIt crashes.\n")
    assert len(rows) == 1
    assert rows[0].claim.target == "2.4.1"
    assert rows[0].status is Status.CONFIRMED


def test_issue_form_mismatch_stays_unverified(tmp_path):
    write(tmp_path, *MANIFESTS[0])
    rows = check(tmp_path, "### Version or commit\n\n2.3.0\n\n### Description\n\nIt crashes.\n")
    assert len(rows) == 1
    assert rows[0].status is Status.UNVERIFIED
    assert "2.3.0" in rows[0].detail and "2.4.1" in rows[0].detail


@pytest.mark.parametrize("value", ["_No response_", "abc1234", ""])
def test_empty_or_commit_only_form_version_adds_no_row(tmp_path, value):
    write(tmp_path, *MANIFESTS[0])
    assert check(tmp_path, f"### Version or commit\n\n{value}\n") == []


def test_issue_form_repeated_field_and_repro_do_not_override_version(tmp_path):
    write(tmp_path, *MANIFESTS[0])
    text = ("### Repro command\n\n```shell\necho 'version 9.9.9'\n```\n\n"
            "### Version or commit\n\nv2.4.1\n\n### Version or commit\n\n9.9.9\n\n"
            "### Description\n\nIt crashes.\n")
    rows = check(tmp_path, text)
    assert [row.claim.target for row in rows] == ["2.4.1"]
    assert rows[0].status is Status.CONFIRMED


def test_form_description_versions_are_also_checked_without_duplicates(tmp_path):
    write(tmp_path, *MANIFESTS[0])
    text = ("### Version or commit\n\nv2.4.1\n\n### Description\n\n"
            "Reproduced on 2.4.1 and on 2.3.0.\n")
    rows = check(tmp_path, text)
    assert [row.claim.target for row in rows] == ["2.4.1", "2.3.0"]
    assert [row.status for row in rows] == [Status.CONFIRMED, Status.UNVERIFIED]


@pytest.mark.parametrize("version", ["2.4", "2.4.1rc1", "2.4.1.dev2", "2.4.1.post1", "2.4.1-beta.2", "2.4.1+build.7"])
def test_release_suffixes_are_compared_as_complete_versions(tmp_path, version):
    write(tmp_path, "package.json", json.dumps({"version": version}))
    rows = check(tmp_path, f"Reproduced on v{version}.")
    assert len(rows) == 1
    assert rows[0].claim.target == version
    assert rows[0].status is Status.CONFIRMED
    rows = check(tmp_path, "Reproduced on 2.4.1.")
    assert rows[0].status is Status.UNVERIFIED


def test_versions_are_deduplicated_in_report_order(tmp_path):
    write(tmp_path, *MANIFESTS[0])
    rows = check(tmp_path, "Reproduced on 2.3.0, v2.4.1, and 2.3.0.")
    assert [row.claim.target for row in rows] == ["2.3.0", "2.4.1"]


@pytest.mark.parametrize("text", ["See https://example.test/2.4.1/notes", "Connect to 127.0.0.1.",
                                 "Read release-2.4.1.txt."])
def test_paths_and_addresses_are_not_version_claims(tmp_path, text):
    write(tmp_path, *MANIFESTS[0])
    assert check(tmp_path, text) == []


@pytest.mark.parametrize("currency", ["$", "€", "£", "₹", "¥"])
def test_currency_amounts_are_not_version_claims(tmp_path, currency):
    write(tmp_path, *MANIFESTS[2])
    assert check(tmp_path, f'It rounds 199 cents down to "{currency}1.98".') == []


@pytest.mark.parametrize("name,content", [
    ("pyproject.toml", '[tool.other]\nversion = "2.4.1"\n'),
    ("pyproject.toml", '[project]\nversion = 2\n'),
    ("pyproject.toml", '[project]\ndynamic = ["version"]\n'),
    ("pyproject.toml", 'description = """\n[project]\nversion = "2.4.1"\n"""\n'),
    ("setup.cfg", "[other]\nversion = 2.4.1\n"),
    ("setup.cfg", "[metadata]\nversion = attr: sample.__version__\n"),
    ("setup.cfg", "[metadata]\nversion = file: VERSION\n"),
    ("package.json", '{"dependencies": {"sample": "2.4.1"}}'),
    ("package.json", '{"version": 2}'),
    ("package.json", '[{"version": "2.4.1"}]'),
    ("sample.py", '# __version__ = "2.4.1"\n'),
    ("sample.py", 'text = \'__version__ = "2.4.1"\'\n'),
    ("sample.py", 'def f():\n    __version__ = "2.4.1"\n'),
    ("sample.py", 'class Sample:\n    __version__ = "2.4.1"\n'),
    ("sample.py", '__version__ = get_version()\n'),
])
def test_only_static_repository_version_declarations_count(tmp_path, name, content):
    write(tmp_path, name, content)
    assert check(tmp_path, "Reproduced on 2.4.1.") == []


def test_python_declaration_is_parsed_without_importing_the_package(tmp_path):
    marker = tmp_path / "executed"
    write(tmp_path, "sample/__init__.py", f'__version__: str = "2.4.1"\n'
          f'from pathlib import Path\nPath({str(marker)!r}).write_text("executed")\n')
    rows = check(tmp_path, "Reproduced on 2.4.1.")
    assert rows[0].status is Status.CONFIRMED
    assert rows[0].source == "sample/__init__.py:1"
    assert not marker.exists()


@pytest.mark.parametrize("directory", [".git", "node_modules", ".venv", "venv", "dist", "build", "unit_tests", "tests"])
def test_dependency_build_and_test_versions_are_ignored(tmp_path, directory):
    write(tmp_path, f'{directory}/sample.py', '__version__ = "2.4.1"\n')
    assert check(tmp_path, "Reproduced on 2.4.1.") == []


def test_repository_under_a_skip_directory_is_still_checked(tmp_path):
    repo = tmp_path / "build" / "sample"
    write(repo, *MANIFESTS[3])
    assert check(repo, "Reproduced on 2.4.1.")[0].status is Status.CONFIRMED


@pytest.mark.parametrize("name,content", [
    ("pyproject.toml", '[project\nversion = "2.4.1"\n'),
    ("setup.cfg", "[metadata\nversion = 2.4.1\n"),
    ("package.json", '{"version": "2.4.1"'),
    ("sample.py", '__version__ = "2.4.1"\nnot valid python !!!\n'),
])
def test_malformed_metadata_never_confirms_or_refutes(tmp_path, name, content):
    write(tmp_path, name, content)
    assert all(row.status is Status.UNVERIFIED for row in check(tmp_path, "Reproduced on 2.4.1."))


def test_unreadable_manifest_never_confirms_or_refutes(tmp_path, monkeypatch):
    path = write(tmp_path, *MANIFESTS[0])
    real_read = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == path:
            raise PermissionError("cannot read manifest")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert all(row.status is Status.UNVERIFIED for row in check(tmp_path, "Reproduced on 2.4.1."))


def test_symlink_outside_repository_does_not_declare_its_version(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = write(tmp_path, "outside.py", '__version__ = "2.4.1"\n')
    (repo / "sample.py").symlink_to(outside)
    assert check(repo, "Reproduced on 2.4.1.") == []


def test_conflicting_manifests_are_unverified(tmp_path):
    write(tmp_path, *MANIFESTS[0])
    write(tmp_path, "setup.cfg", "[metadata]\nversion = 2.3.0\n")
    rows = check(tmp_path, "Reproduced on 2.4.1.")
    assert len(rows) == 1
    assert rows[0].status is Status.UNVERIFIED
    assert "2.4.1" in rows[0].detail and "2.3.0" in rows[0].detail
    assert "pyproject.toml" in rows[0].detail and "setup.cfg" in rows[0].detail


def test_unreadable_manifest_does_not_make_a_fallback_match_confirmed(tmp_path, monkeypatch):
    path = write(tmp_path, *MANIFESTS[0])
    write(tmp_path, *MANIFESTS[3])
    real_read = Path.read_text

    def read_text(self, *args, **kwargs):
        if self == path:
            raise PermissionError("cannot read manifest")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    rows = check(tmp_path, "Reproduced on 2.4.1.")
    assert rows and rows[0].status is Status.UNVERIFIED


@pytest.mark.parametrize("content,expected", [
    ('[project]\nversion = "2.4.1" # release\n', Status.CONFIRMED),
    ("[project]\nversion = '2.4.1'\n[tool.other]\nversion = '9.9.9'\n", Status.CONFIRMED),
    ('["project"]\n"version" = "2.4.1"\n', Status.CONFIRMED),
    ('description = """\n[project]\nversion = "2.4.1"\n"""\n', None),
])
def test_pyproject_literal_reader_on_python_310(tmp_path, monkeypatch, content, expected):
    from claimcheck import versions

    monkeypatch.setattr(versions, "_tomllib", None)
    write(tmp_path, "pyproject.toml", content)
    rows = check(tmp_path, "Reproduced on 2.4.1.")
    assert [row.status for row in rows] == ([] if expected is None else [expected])


@pytest.mark.parametrize("name,content", MANIFESTS)
@pytest.mark.parametrize("version,code", [("2.4.1", 0), ("2.3.0", 2)])
def test_cli_reports_version_rows_and_conservative_exit_codes(tmp_path, capsys, name, content, version, code):
    repo = tmp_path / "repo"
    write(repo, name, content)
    report_path = write(tmp_path, "report.txt", f"Reproduced on {version}.")
    args = ["check", "--repo", str(repo), "--report", str(report_path), "--no-model"]
    assert cli.main([*args, "--json"]) == code
    data = json.loads(capsys.readouterr().out)
    assert len(data["evidence"]) == 1
    row = data["evidence"][0]
    assert row["kind"] == "version" and row["basis"] == "observed"
    assert row["status"] == ("confirmed" if code == 0 else "unverified")
    assert "2.4.1" in row["detail"] and version in row["detail"]
    assert cli.main(args) == code
    assert version in capsys.readouterr().out


def test_run_check_reads_versions_independently_of_model_extraction(tmp_path, monkeypatch):
    write(tmp_path, *MANIFESTS[0])
    monkeypatch.setattr(extract, "extract_claims", lambda text: extract.Extraction(model="fake"))
    verdict, model = cli.run_check(tmp_path, "Reproduced on 2.3.0.", None, None, "", True)
    assert model == "fake"
    assert len(verdict.evidence) == 1
    assert verdict.evidence[0].status is Status.UNVERIFIED


def test_run_check_sees_version_after_the_model_report_limit(tmp_path):
    write(tmp_path, *MANIFESTS[0])
    text = "padding\n" * extract.MAX_REPORT_CHARS + "Reproduced on 2.3.0."
    verdict, _ = cli.run_check(tmp_path, text, None, None, "", False)
    assert len(verdict.evidence) == 1
    assert verdict.evidence[0].status is Status.UNVERIFIED


def test_version_mismatch_is_a_sarif_warning(tmp_path):
    write(tmp_path, *MANIFESTS[0])
    verdict, model = cli.run_check(tmp_path, "Reproduced on 2.3.0.", None, None, "", False)
    results = json.loads(report.to_sarif(verdict, model))["runs"][0]["results"]
    assert len(results) == 1
    assert results[0]["ruleId"] == "claimcheck/version"
    assert results[0]["level"] == "warning"


def test_no_version_row_keeps_existing_symbol_check(tmp_path):
    write(tmp_path, "sample.py", "def parse_url(s):\n    return s\n")
    verdict, _ = cli.run_check(tmp_path, "Crash in parse_url().", None, None, "", False)
    assert [row.claim.kind for row in verdict.evidence] == ["symbol"]
    assert verdict.outcome == "supported"
