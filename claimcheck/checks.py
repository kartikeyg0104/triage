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


def _mentions(repo: Path, name: str) -> tuple[Path, int] | None:
    """First place the name appears at all, even if it is not a definition."""
    word = re.compile(rf"\b{re.escape(name)}\b")
    for path in _source_files(repo):
        for number, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if word.search(line):
                return path, number
    return None


def check_symbol(repo: Path, name: str) -> Evidence:
    """A static search can show a definition exists. It can only suggest one does not:
    code may define names dynamically (setattr, globals(), __getattr__, re-exports)."""
    claim = Claim("symbol", f"{name}() exists", name)
    hits = find_symbol(repo, name)
    if hits:
        path, line = hits[0]
        return Evidence(claim, Status.CONFIRMED, f"{name} is defined", f"{path.relative_to(repo)}:{line}", "observed")
    where = _mentions(repo, name)
    if where:
        path, line = where
        return Evidence(claim, Status.UNVERIFIED,
                        f"{name} is referenced but no static definition was found; it may be defined dynamically",
                        f"{path.relative_to(repo)}:{line}", "inferred")
    count = sum(1 for _ in _source_files(repo))
    return Evidence(claim, Status.REFUTED, f"{name} does not appear in any of {count} source files (static search)",
                    repo.resolve().name, "inferred")


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
    """Whether the diff touches test files. Not touching them does not show the change is
    untested: existing tests may already cover it."""
    claim = Claim("pr_tests", "the PR changes tests alongside its source change")
    files = changed_files(diff)
    code = [f for f in files if Path(f).suffix in SOURCE_SUFFIXES and not _is_test(f)]
    tests = [f for f in files if _is_test(f)]
    if not code:
        return Evidence(claim, Status.UNVERIFIED, "the diff changes no source files", "diff", "observed")
    if tests:
        return Evidence(claim, Status.CONFIRMED, f"{len(tests)} test file(s) changed: {', '.join(tests)}", "diff", "observed")
    return Evidence(claim, Status.UNVERIFIED,
                    f"no test file changed with {', '.join(code)}; existing tests may still cover it (coverage was not measured)",
                    "diff", "inferred")
