"""Deterministic checks. These never call a model: they look at the repo and the diff."""

import ast
import io
import re
import tokenize
from pathlib import Path

from .models import Claim, Evidence, Status

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
_JS_DEF = r"(?:function\s+{n}\b|(?:const|let|var)\s+{n}\s*=|class\s+{n}\b|\b{n}\s*\([^)]*\)\s*\{{)"
# Code that creates names from strings at runtime, which no static search can see.
_DYNAMIC_PY = [
    ("setattr()", re.compile(r"\bsetattr\s*\(")),
    ("globals()", re.compile(r"\bglobals\(\)\s*(?:\[.*\]\s*=(?!=)|\.update\s*\()")),
    ("module-level __getattr__", re.compile(r"^def\s+__getattr__\s*\(")),
    ("exec()", re.compile(r"(?<![.\w])exec\s*\(")),
]
_DYNAMIC_JS = [
    ("exports[...] =", re.compile(r"\bexports\s*\[.*\]\s*=(?!=)")),
    ("Object.defineProperty()", re.compile(r"\bObject\.definePropert(?:y|ies)\s*\(")),
]


def _source_files(repo: Path):
    """Skip dirs are judged inside the repo: a checkout that lives under build/ or venv/ is still searched."""
    for path in repo.rglob("*"):
        if path.is_file() and path.suffix in SOURCE_SUFFIXES and not (set(path.relative_to(repo).parts) & SKIP_DIRS):
            yield path


def _strip_js_comment(line: str) -> str:
    """Drop `// ...` and one-line `/* ... */`, leaving quoted strings alone."""
    kept, quote, i = [], None, 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\":
                kept.append(line[i:i + 2])
                i += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif line.startswith("//", i):
            break
        elif line.startswith("/*", i):
            end = line.find("*/", i + 2)
            if end < 0:
                break
            kept.append(" ")
            i = end + 2
            continue
        kept.append(char)
        i += 1
    return "".join(kept)


def _code_lines(path: Path) -> list[str]:
    """The file's lines without comments: a name left in a comment says nothing about the code.
    Strings are kept, because setattr-style code names things in strings."""
    text = path.read_text(errors="replace")
    lines = text.split("\n")
    if path.suffix != ".py":
        return [_strip_js_comment(line) for line in lines]
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT:
                row, column = token.start
                lines[row - 1] = lines[row - 1][:column]
    except (tokenize.TokenError, SyntaxError):
        return text.split("\n")  # unparseable: keep every mention rather than guess
    return lines


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
            for number, line in enumerate(_code_lines(path), 1):
                if js_pattern.search(line):
                    hits.append((path, number))
    return hits


def _mentions(repo: Path, name: str) -> tuple[Path, int] | None:
    """First place the name appears in code or a string, even if it is not a definition."""
    word = re.compile(rf"\b{re.escape(name)}\b")
    for path in _source_files(repo):
        for number, line in enumerate(_code_lines(path), 1):
            if word.search(line):
                return path, number
    return None


def _dynamic_definition(repo: Path) -> tuple[Path, int, str] | None:
    """First place the repo creates names at runtime, e.g. setattr(mod, 'export_' + fmt, fn)."""
    for path in _source_files(repo):
        patterns = _DYNAMIC_PY if path.suffix == ".py" else _DYNAMIC_JS
        for number, line in enumerate(_code_lines(path), 1):
            for label, pattern in patterns:
                if pattern.search(line):
                    return path, number, label
    return None


def check_symbol(repo: Path, name: str) -> Evidence:
    """A static search can show a definition exists. It can only suggest one does not:
    code may define names dynamically (setattr, globals(), __getattr__, re-exports), so absence
    is refuted only when the repo has no such mechanism."""
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
    dynamic = _dynamic_definition(repo)
    if dynamic:
        path, line, label = dynamic
        return Evidence(claim, Status.UNVERIFIED,
                        f"{name} has no static definition, but this code creates names dynamically ({label}); "
                        "it may exist at runtime",
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
