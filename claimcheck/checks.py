"""Deterministic checks. These never call a model: they look at the repo and the diff."""

import ast
import bisect
import functools
import io
import re
import tokenize
from pathlib import Path

from .models import Claim, Evidence, Status

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"}
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx"}
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


# --- JS/TS: a small tokenizer, so that comments, strings, template literals and regex literals
# never look like code, and a scan that knows whether a name sits in a class body, an object
# literal, a type or ordinary code. `name(...) {` is a definition only as a class or object
# member; anywhere else it is a call.

_JS_PUNCT = sorted(">>>= ... === !== **= <<= >>= >>> &&= ||= ??= => ?. ?? == != <= >= && || ++ -- += -= *= /= %= "
                   "&= |= ^= ** << >>".split(), key=len, reverse=True)
_JS_IDENT = re.compile(r"#?[A-Za-z_$\u0080-￿][\w$\u0080-￿]*")
_JS_NUMBER = re.compile(r"\.?\d[\w.]*")
_JS_REGEX_AFTER = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do",
                   "else", "yield", "await"}
_JS_OBJECT_AFTER = {"return", "yield", "await", "default", "in", "of", "case", "throw", "typeof", "void"}
_JS_CLASS_MODIFIERS = {"static", "async", "get", "set", "public", "private", "protected", "readonly", "abstract",
                       "override", "declare", "accessor"}
_JS_OBJECT_MODIFIERS = {"async", "get", "set"}
_JS_STATEMENT_WORDS = {"const", "let", "var", "function", "class", "export", "import", "interface", "type", "enum",
                       "return", "declare", "abstract"}
_JS_BODY_AFTER = {")", "]", "}", ">"}  # a `{` after these (or a word) in a return type is the body


def _js_string_end(text: str, i: int) -> int:
    """End of the '...' or "..." starting at i. They cannot span lines, so a stray quote (JSX
    text like `don't`) swallows at most the rest of its line."""
    quote, j = text[i], i + 1
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        if text[j] == "\n":
            return j
        j += 1
    return len(text)


def _js_template_end(text: str, j: int) -> tuple[int, bool]:
    """Scan a template literal chunk from j: (end, True) if it stops at `${`, (end, False) at the closing backtick."""
    while j < len(text):
        if text[j] == "\\":
            j += 2
        elif text[j] == "`":
            return j + 1, False
        elif text.startswith("${", j):
            return j + 2, True
        else:
            j += 1
    return len(text), False


def _js_regex_end(text: str, i: int) -> int | None:
    """End of a regex literal at i, or None if this `/` is not one (regex literals cannot span lines)."""
    j, in_class = i + 1, False
    while j < len(text) and text[j] != "\n":
        char = text[j]
        if char == "\\":
            j += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "/":
            j += 1
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            return j
        j += 1
    return None


def _js_regex_allowed(tokens: list) -> bool:
    if not tokens:
        return True
    kind, value, _ = tokens[-1]
    if kind == "id":
        return value in _JS_REGEX_AFTER
    return kind == "punct" and value not in (")", "]", "}")


@functools.lru_cache(maxsize=64)
def _js_scan(text: str) -> tuple[tuple, tuple]:
    """Tokens (kind, value, offset) and comment spans (start, end) of JS/TS source.
    Kinds: id, num, str (strings and template literals), regex, punct."""
    tokens, comments, braces = [], [], []  # braces: "{" or "`" (a template's `${`)
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            comments.append((i, end))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            comments.append((i, end))
            i = end
        elif char in "'\"":
            end = _js_string_end(text, i)
            tokens.append(("str", text[i + 1:end - 1], i))
            i = end
        elif char == "`" or (char == "}" and braces and braces[-1] == "`"):
            if char == "}":
                braces.pop()
            end, opened = _js_template_end(text, i + 1)
            if opened:
                braces.append("`")
            tokens.append(("str", "", i))
            i = end
        elif char == "/" and _js_regex_allowed(tokens) and (end := _js_regex_end(text, i)):
            tokens.append(("regex", text[i:end], i))
            i = end
        elif match := _JS_IDENT.match(text, i):
            tokens.append(("id", match.group(), i))
            i = match.end()
        elif match := _JS_NUMBER.match(text, i):
            tokens.append(("num", match.group(), i))
            i = match.end()
        else:
            punct = next((p for p in _JS_PUNCT if text.startswith(p, i)), char)
            if punct == "{":
                braces.append("{")
            elif punct == "}" and braces:
                braces.pop()
            tokens.append(("punct", punct, i))
            i += len(punct)
    return tuple(tokens), tuple(comments)


def _js_without_comments(text: str) -> str:
    """The text with each comment blanked out; line breaks are kept so line numbers still hold."""
    parts, last = [], 0
    for start, end in _js_scan(text)[1]:
        parts += [text[last:start], re.sub(r"[^\n]", " ", text[start:end])]
        last = end
    return "".join(parts) + text[last:]


class _Frame:
    """An open bracket: what its contents are, and whether the next token starts a member."""

    def __init__(self, kind: str, start: bool = False):
        self.kind = kind  # block, class, object, type (interfaces, enums, type literals) or paren
        self.start = start


class _JsScan:
    def __init__(self, text: str):
        self.toks = _js_scan(text)[0]
        self.line_starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.close = {}  # index of an opening bracket -> index of its closing one
        opened = []
        for index, (kind, value, _) in enumerate(self.toks):
            if kind == "punct" and value in "([{":
                opened.append(index)
            elif kind == "punct" and value in ")]}" and opened:
                self.close[opened.pop()] = index

    def tok(self, j: int) -> tuple:
        return self.toks[j] if 0 <= j < len(self.toks) else ("eof", "", 0)

    def is_(self, j: int, *values: str) -> bool:
        kind, value, _ = self.tok(j)
        return kind in ("punct", "id") and value in values

    def is_name(self, j: int) -> bool:
        return self.tok(j)[0] == "id"

    def line(self, j: int) -> int:
        return bisect.bisect_right(self.line_starts, self.tok(j)[2])

    def skip_angles(self, j: int) -> int:
        """From a `<` at j, the index just past its matching `>` (type parameters)."""
        depth = 0
        while j < len(self.toks):
            if self.is_(j, "<"):
                depth += 1
            elif self.tok(j)[0] == "punct" and set(self.tok(j)[1]) == {">"}:
                depth -= len(self.tok(j)[1])
            elif self.is_(j, "(", "[", "{") and j in self.close:
                j = self.close[j]
            j += 1
            if depth <= 0:
                return j
        return j

    def after_type(self, j: int) -> int:
        """From the first token of a type annotation, the index of what follows it: a `{` that
        opens a body, an `=>`, or the token that ends the annotation."""
        while j < len(self.toks):
            kind, value, _ = self.tok(j)
            if kind == "punct" and value == "{":
                prev_kind, prev_value, _ = self.tok(j - 1)
                if prev_kind in ("id", "str", "num") or prev_value in _JS_BODY_AFTER:
                    return j
            if kind == "punct" and value in ("=>", ";", ",", ")", "]", "}", "="):
                return j
            if kind == "punct" and value in ("(", "[", "{") and j in self.close:
                j = self.close[j]
            elif kind == "punct" and value == "<":
                j = self.skip_angles(j) - 1
            j += 1
        return j

    def is_function_value(self, v: int) -> bool:
        """Whether the expression at v is a function: `function`, `async function`, or an arrow."""
        if self.is_(v, "async") and not self.is_(v + 1, ",", "}", ";"):
            v += 1
        if self.is_(v, "function"):
            return True
        if self.is_name(v):
            return self.is_(v + 1, "=>")
        if self.is_(v, "(") and v in self.close:
            after = self.close[v] + 1
            return self.is_(after, "=>") or (self.is_(after, ":") and self.is_(self.after_type(after + 1), "=>"))
        return False

    def member(self, i: int, frame_kind: str) -> int | None:
        """The index of the name a class or object member at i defines, if it defines one."""
        modifiers = _JS_CLASS_MODIFIERS if frame_kind == "class" else _JS_OBJECT_MODIFIERS
        j = i
        while self.is_(j, *modifiers) and (self.is_name(j + 1) or self.is_(j + 1, "*")):
            j += 1
        if self.is_(j, "*"):
            j += 1
        if not self.is_name(j):
            return None  # computed, string and numeric keys are not searched
        if frame_kind == "class":
            return j if self.is_(j + 1, "(", "<", "=", ":", ";", "?", "!", "}") else None
        if self.is_(j + 1, "("):  # method shorthand: needs a body, or it is a call or a type
            after = self.close.get(j + 1, len(self.toks)) + 1
            if self.is_(after, ":"):
                after = self.after_type(after + 1)
            return j if self.is_(after, "{") else None
        if j == i and self.is_(j + 1, ":") and self.is_function_value(j + 2):
            return j
        return None

    def skip_decorator(self, i: int) -> int:
        j = i + 1
        while self.is_name(j) and self.is_(j + 1, "."):
            j += 2
        j += 1
        if self.is_(j, "(") and j in self.close:
            j = self.close[j] + 1
        return j

    def brace_kind(self, i: int, frame: _Frame) -> str:
        """What a `{` at i opens when no class, interface or enum header claimed it."""
        if frame.kind == "type":
            return "type"
        kind, value, _ = self.tok(i - 1)
        if kind == "eof" or kind in ("str", "num", "regex"):
            return "block"
        if kind == "id":
            return "object" if value in _JS_OBJECT_AFTER else "block"
        if value == ":":
            return "object" if frame.kind == "object" else "type"  # else an annotation, label or case
        if value in ("<", "|", "&"):
            return "type"
        return "block" if value in (")", ";", "{", "}", "=>", ">") else "object"

    def definitions(self):
        """(name, line) for each definition in the file."""
        stack = [_Frame("block")]
        pending = None  # (kind, depth): the next `{` at this depth opens a class, interface or enum body
        alias = None  # depth of a `type X = ...` being read: brackets opened in it hold types
        i = 0
        while i < len(self.toks):
            kind, value, _ = self.toks[i]
            frame = stack[-1]
            if frame.start and frame.kind in ("class", "object"):
                if frame.kind == "class" and self.is_(i, "@"):
                    i = self.skip_decorator(i)
                    continue
                frame.start = False
                if (name := self.member(i, frame.kind)) is not None:
                    yield self.toks[name][1], self.line(name)
            after_dot = self.is_(i - 1, ".", "?.")
            if alias is not None and len(stack) == alias and (self.is_(i, ";") or (
                    kind == "id" and value in _JS_STATEMENT_WORDS and not after_dot)):
                alias = None
            if kind == "id" and not after_dot:
                yield from self.keyword(i, stack)
                if value == "class":
                    pending = ("class", len(stack))
                elif value in ("interface", "enum") and self.is_name(i + 1) and \
                        self.is_(i + 2, "{", "<", "extends"):
                    pending = ("type", len(stack))
                elif value == "type" and self.is_name(i + 1) and self.is_(i + 2, "=", "<"):
                    alias = len(stack)
                if value in ("class", "interface") and self.is_name(i + 1) and self.is_(i + 2, "<"):
                    i = self.skip_angles(i + 2)
                    continue
            elif kind == "punct" and value in "([{":
                typed = frame.kind == "type" or alias is not None
                if value != "{":
                    stack.append(_Frame("type" if typed else "paren"))
                elif pending and pending[1] == len(stack):
                    stack.append(_Frame(pending[0], start=pending[0] == "class"))
                    pending = None
                else:
                    brace = "type" if typed else self.brace_kind(i, frame)
                    stack.append(_Frame(brace, start=brace == "object"))
            elif kind == "punct" and value in ")]}":
                if len(stack) > 1:
                    stack.pop()
                if alias is not None and len(stack) < alias:
                    alias = None
                if value == "}" and stack[-1].kind == "class":
                    stack[-1].start = True
            elif kind == "punct" and value in (";", ","):
                if value == ";" and pending and pending[1] == len(stack):
                    pending = None
                if (value == ";" and frame.kind == "class") or (value == "," and frame.kind == "object"):
                    frame.start = True
            i += 1

    def keyword(self, i: int, stack: list):
        """Definitions introduced by a keyword: function, class, interface, enum, type, const/let/var."""
        value = self.toks[i][1]
        j = i + 1
        if value == "function":
            j += self.is_(j, "*")
            if self.is_name(j):
                yield self.toks[j][1], self.line(j)
        elif value == "class":
            if self.is_name(j) and not self.is_(j, "extends", "implements"):
                yield self.toks[j][1], self.line(j)
        elif value in ("interface", "enum"):
            if self.is_name(j) and self.is_(j + 1, "{", "<", "extends"):
                yield self.toks[j][1], self.line(j)
        elif value == "type":
            if self.is_name(j) and self.is_(j + 1, "=", "<"):
                yield self.toks[j][1], self.line(j)
        elif value in ("const", "let", "var"):
            if self.is_name(j) and self.is_(j + 1, "=", ":", "!"):
                yield self.toks[j][1], self.line(j)


def _js_definitions(path: Path):
    yield from _JsScan(path.read_text(errors="replace")).definitions()


def _code_lines(path: Path) -> list[str]:
    """The file's lines without comments: a name left in a comment says nothing about the code.
    Strings are kept, because setattr-style code names things in strings."""
    text = path.read_text(errors="replace")
    lines = text.split("\n")
    if path.suffix != ".py":
        return _js_without_comments(text).split("\n")
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
    for path in _source_files(repo):
        if path.suffix == ".py":
            hits += [(path, line) for found, line in _py_definitions(path) if found == name]
        else:
            hits += [(path, line) for line in sorted({line for found, line in _js_definitions(path) if found == name})]
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
