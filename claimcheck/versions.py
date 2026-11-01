"""Compare reported releases with static metadata, without running repository code."""

import ast
import configparser
import io
import json
import os
import re
import tokenize
import unicodedata
from pathlib import Path

try:
    import tomllib as _tomllib
except ImportError:
    _tomllib = None

from .checks import SKIP_DIRS
from .issue_form import parse_issue_form
from .models import Claim, Evidence, Status

_VERSION_TEXT = (r"[vV]?\d+\.\d+(?:\.\d+)?(?:(?:a|b|rc)\d+)?(?:\.(?:post|dev)\d+)*"
                 r"(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?(?:\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?")
_VERSION = re.compile(_VERSION_TEXT)
_REPORT_VERSION = re.compile(r"(?<![\w./+-])(" + _VERSION_TEXT + r")(?![\w/+]|[.-]\w)")
_SKIP_DIRS = SKIP_DIRS | {"test", "tests", "unit_tests", "__tests__"}


def _normalise(value: object) -> str | None:
    if not isinstance(value, str) or not _VERSION.fullmatch(value.strip()):
        return None
    return value.strip().lstrip("vV")


def _reported_versions(text: str) -> list[str]:
    """Form commands are untrusted instructions, so only the version and description are claims."""
    form = parse_issue_form(text)
    if not form.is_empty():
        preamble = re.split(r"(?m)^### ", text, maxsplit=1)[0]
        text = "\n".join((form.version, form.description, preamble))
    return list(dict.fromkeys(match.group(1).lstrip("vV") for match in _REPORT_VERSION.finditer(text)
                              if not match.start() or unicodedata.category(text[match.start() - 1]) != "Sc"))


def _project_version(text: str) -> object:
    """Python 3.10 has no TOML library; tokenize literal assignments so strings cannot fake a table."""
    if _tomllib is not None:
        project = _tomllib.loads(text).get("project", {})
        return project.get("version") if isinstance(project, dict) else None
    section, statement, version = "", [], None
    ignored = {tokenize.COMMENT, tokenize.NL, tokenize.INDENT, tokenize.DEDENT}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
            values = [part.string for part in statement]
            if values and values[0] == "[" and values[-1] == "]":
                section = "".join(values[1:-1]).strip("\"'")
            elif (section == "project" and len(values) == 3 and values[0].strip("\"'") == "version"
                  and values[1] == "=" and statement[2].type == tokenize.STRING):
                if version is not None:
                    raise ValueError("duplicate version")
                version = ast.literal_eval(values[2])
            statement = []
        elif token.type not in ignored:
            statement.append(token)
    return version


def _manifest_version(name: str, text: str) -> object:
    if name == "pyproject.toml":
        return _project_version(text)
    if name == "setup.cfg":
        config = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#", ";"))
        config.read_string(text)
        return config.get("metadata", "version", fallback=None)
    data = json.loads(text)
    return data.get("version") if isinstance(data, dict) else None


def _read(path: Path, repo: Path, problems: list[str]) -> str | None:
    """Skip symlinks and special files rather than reading outside the checkout or blocking."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        problems.append(f"could not read {path.relative_to(repo)}")
        return None


def _declarations(repo: Path) -> tuple[list[tuple[str, str]], list[str]]:
    declarations: list[tuple[str, str]] = []
    problems: list[str] = []
    for name in ("pyproject.toml", "setup.cfg", "package.json"):
        text = _read(repo / name, repo, problems)
        if text is None:
            continue
        try:
            version = _normalise(_manifest_version(name, text))
        except (ValueError, SyntaxError, configparser.Error, tokenize.TokenError):
            problems.append(f"could not parse {name}")
            continue
        if version is not None:
            declarations.append((version, name))
    if declarations:
        return declarations, problems

    def walk_error(error: OSError) -> None:
        problems.append("could not scan all Python source files")

    for root, directories, files in os.walk(repo, onerror=walk_error):
        directories[:] = sorted(name for name in directories if name not in _SKIP_DIRS
                                and not (Path(root) / name).is_symlink())
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix != ".py":
                continue
            text = _read(path, repo, problems)
            if text is None:
                continue
            try:
                nodes = ast.parse(text).body
            except (SyntaxError, ValueError):
                problems.append(f"could not parse {path.relative_to(repo)}")
                continue
            for node in nodes:
                if isinstance(node, ast.Assign):
                    targets, value = node.targets, node.value
                elif isinstance(node, ast.AnnAssign):
                    targets, value = [node.target], node.value
                else:
                    continue
                if (any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets)
                        and isinstance(value, ast.Constant) and (version := _normalise(value.value))):
                    declarations.append((version, f"{path.relative_to(repo)}:{node.lineno}"))
    return declarations, problems


def check_versions(repo: Path, report_text: str) -> list[Evidence]:
    """Matching metadata is observed; a different release cannot refute the report's reproduction."""
    reported = _reported_versions(report_text)
    if not reported:
        return []
    declarations, problems = _declarations(repo)
    if not declarations:
        return []
    declared = {version for version, _ in declarations}
    sources = ", ".join(f"{version} ({source})" for version, source in declarations)
    rows = []
    for version in reported:
        claim = Claim("version", f"report names version {version}", version)
        status = Status.UNVERIFIED
        if problems:
            detail = (f"report version {version}; repository declares {sources}; "
                      f"version check incomplete: {'; '.join(problems)}")
        elif len(declared) != 1:
            detail = f"report version {version}; repository version declarations disagree: {sources}"
        elif version in declared:
            status = Status.CONFIRMED
            detail = (f"report version {version} matches repository declared version {sources}; "
                      "metadata only, reproduction was not checked")
        else:
            detail = (f"report version {version} differs from repository declared version {sources}; "
                      "an older or different release remains unverified")
        rows.append(Evidence(claim, status, detail, declarations[0][1], "observed"))
    return rows
