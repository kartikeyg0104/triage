"""Deterministic checks. These never call a model: they look at the repo and the diff."""

import ast
import re
from pathlib import Path

from .models import Claim, Evidence, Status

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
_JS_DEF = r"(?:function\s+{n}\b|(?:const|let|var)\s+{n}\s*=|class\s+{n}\b|\b{n}\s*\([^)]*\)\s*\{{)"


def _source_files(repo: Path):
    for path in repo.rglob("*"):
        if path.is_file() and path.suffix in SOURCE_SUFFIXES and not (set(path.parts) & SKIP_DIRS):
            yield path


def _py_definitions(path: Path):
    try:
        tree = ast.parse(path.read_text(errors="replace"))
    except SyntaxError:
        return
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno


def find_symbol(repo: Path, name: str) -> list[tuple[Path, int]]:
    """Return (file, line) for every definition of `name` in the repo."""
    hits = []
    js_pattern = re.compile(_JS_DEF.format(n=re.escape(name)))
    for path in _source_files(repo):
        if path.suffix == ".py":
            hits += [(path, line) for found, line in _py_definitions(path) if found == name]
        else:
            for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if js_pattern.search(line):
                    hits.append((path, number))
    return hits


def check_symbol(repo: Path, name: str) -> Evidence:
    claim = Claim("symbol", f"{name}() exists", name)
    hits = find_symbol(repo, name)
    if hits:
        path, line = hits[0]
        return Evidence(claim, Status.CONFIRMED, f"{name} is defined", f"{path.relative_to(repo)}:{line}")
    return Evidence(claim, Status.REFUTED, f"{name} is not defined anywhere in the repo", str(repo.name))


def changed_files(diff: str) -> list[str]:
    """File paths touched by a unified diff (the 'b/' side, ignoring deletions)."""
    files = []
    for match in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE):
        files.append(match.group(1).strip())
    return files


def _is_test(path: str) -> bool:
    p = Path(path)
    return (
        p.name.startswith("test_")
        or p.stem.endswith(("_test", ".test", ".spec"))
        or any(part in {"test", "tests", "unit_tests", "__tests__", "spec"} for part in p.parts)
    )


def check_pr_tests(diff: str) -> Evidence:
    """A PR that changes source but touches no test file is flagged."""
    claim = Claim("pr_tests", "the PR adds tests for its change")
    files = changed_files(diff)
    code = [f for f in files if Path(f).suffix in SOURCE_SUFFIXES and not _is_test(f)]
    tests = [f for f in files if _is_test(f)]
    if not code:
        return Evidence(claim, Status.UNVERIFIED, "the diff changes no source files", "diff")
    if tests:
        return Evidence(claim, Status.CONFIRMED, f"{len(tests)} test file(s) changed: {', '.join(tests)}", "diff")
    return Evidence(claim, Status.REFUTED, f"no test covers the change to {', '.join(code)}", "diff")
