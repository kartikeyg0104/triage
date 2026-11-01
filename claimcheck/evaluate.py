"""Measure claimcheck against cases whose right answers are known.

    python -m claimcheck.evaluate [--cases eval/cases] [--runs 3] [--out docs/EVALUATION.md]
    python -m claimcheck.evaluate --model gemma3:1b [--runs 3] [--out docs/EVALUATION_GEMMA.md]
    python -m claimcheck.evaluate --executor kubernetes [--kube-context kind-claimcheck]

A case is a tiny repo, a report, an optional PR diff and a case.json written from what is
actually true about that repo. Each case goes through the real pipeline (regex extraction and
the selected executor) and every claim and verdict is compared with the truth. Wrong confirmed
and wrong refuted are the numbers that matter: an unverified answer costs a maintainer a look,
a wrong definite answer misleads them.

With --model the same cases go through model extraction (Gemma via Ollama, with the regex names
merged in). Every run records whether the model was really used or extraction fell back to
regex, and if Ollama cannot be reached nothing is run and the whole report says skipped.

--json PATH also writes the results as JSON (the markdown is still written to --out or stdout,
and both are made from the same numbers). Schema version 1:

    schema_version   1; bumped when a field changes meaning or is removed
    generated        date, platform, python, revision, uncommitted (bool), isolation
    model            the --model name, or null for regex extraction
    executor         name (local or kubernetes), context, namespace (null for local),
                     egress_verified (bool, or null if no repro ran)
    skip_reason      why the whole run was skipped (Ollama unreachable), or null
    runs_per_case    --runs
    metrics          cases, scored, skipped, claims_total, claims_correct, verdicts_correct,
                     wrong_confirmed, wrong_refuted (cases), unverified, expected_unverified,
                     repeatable: the counts behind the markdown Results table
    timing           total_seconds (sum of every run's wall-clock time), runs (how many),
                     slowest: [{case, seconds}] the slowest cases, all runs of each summed
    cases            [{name, skipped (reason or null), correct, repeatable, wrong_confirmed,
                     wrong_refuted, seconds, claims: [{key, expected, got (one per run), label,
                     correct}], overall: {expected, got, label, correct},
                     runs: [{seconds, model, claims: {key: status}, overall, repro_source}]}]

Labels are those of the markdown Per case table: ok, miss, wrong confirmed, wrong refuted.
Seconds are wall-clock and rounded to milliseconds.
"""

import argparse
import datetime
import json
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import cli, extract, k8s, sandbox
from .models import Claim, Evidence, Status, Verdict

SKIP_NO_ISOLATION = "skipped (no sandbox isolation)"
SKIP_NO_MODEL = "skipped (Ollama unreachable)"
MISSING = "missing"  # the truth lists a claim the tool did not produce
NOT_CLAIMED = "not claimed"  # the tool produced a claim the report does not make
OUTCOMES = ("supported", "refuted", "unverified")
JSON_SCHEMA_VERSION = 1
SLOWEST = 5  # cases listed under the timing
ROOT = Path(__file__).resolve().parent.parent
_STATUSES = tuple(s.value for s in Status)
_CLAIM_KEY = re.compile(r"^(pr_tests|repro|version|symbol:[A-Za-z_][A-Za-z0-9_]*)$")
_CASE_KEYS = {"truth", "repro", "expect", "expected"}
# The executor may add an image note after the egress note: "(image python:3.12-slim; ...)".
_VERIFIED_EGRESS = re.compile(r"^kubernetes\[[^\]\n]+\] \(egress blocked, verified\)(?: \(image [^()\n]*\))?: ")
ReproRunner = Callable[[Path, str, str], Evidence]


class CaseError(ValueError):
    """A case.json that cannot be trusted as ground truth."""


@dataclass(frozen=True)
class Case:
    name: str
    repo: Path
    report: str
    diff: str | None
    repro: str | None
    expect: str
    claims: dict[str, str]  # claim key ("symbol:name", "pr_tests", "repro") -> true status
    overall: str
    truth: str  # why those are the right answers


@dataclass(frozen=True)
class Run:
    """What the tool said about a case, once."""

    claims: dict[str, str]
    overall: str
    model: str | None = None  # the model run_check reports it used; None is regex extraction
    seconds: float = field(default=0.0, compare=False)  # wall-clock; not part of what the tool said
    repro_source: str | None = field(default=None, compare=False)

    @property
    def names(self) -> list[str]:
        return sorted(key.removeprefix("symbol:") for key in self.claims if key.startswith("symbol:"))


@dataclass
class CaseResult:
    case: Case
    runs: list[Run] = field(default_factory=list)
    skipped: str = ""

    @property
    def repeatable(self) -> bool:
        return all(run == self.runs[0] for run in self.runs)

    @property
    def seconds(self) -> float:
        return _seconds(sum(run.seconds for run in self.runs))

    def claim_keys(self) -> list[str]:
        keys = list(self.case.claims)
        for run in self.runs:
            keys += [key for key in run.claims if key not in keys]
        return keys

    def expected(self, key: str) -> str:
        return self.case.claims.get(key, NOT_CLAIMED)

    def got(self, key: str) -> list[str]:
        return [run.claims.get(key, MISSING) for run in self.runs]

    def claim_correct(self, key: str) -> bool:
        """Right in every run: a tool that is right only sometimes is not right."""
        return all(got == self.expected(key) for got in self.got(key))

    @property
    def verdict_correct(self) -> bool:
        return all(run.overall == self.case.overall for run in self.runs)

    @property
    def correct(self) -> bool:
        return self.verdict_correct and all(self.claim_correct(key) for key in self.claim_keys())

    def _wrong(self, status: str, outcome: str) -> bool:
        """A definite answer the truth does not support, in any run. Claims the report does not
        make have no truth to contradict; they count only if they flip the verdict."""
        for run in self.runs:
            if run.overall == outcome != self.case.overall:
                return True
            if any(run.claims.get(key) == status != truth for key, truth in self.case.claims.items()):
                return True
        return False

    @property
    def wrong_confirmed(self) -> bool:
        return self._wrong("confirmed", "supported")

    @property
    def wrong_refuted(self) -> bool:
        return self._wrong("refuted", "refuted")


@dataclass(frozen=True)
class Summary:
    cases: int
    skipped: int
    claims_total: int
    claims_correct: int
    verdicts_correct: int
    wrong_confirmed: int  # cases
    wrong_refuted: int  # cases
    unverified: int  # scored cases whose verdict was unverified
    expected_unverified: int  # scored cases whose true verdict is unverified
    repeatable: int

    @property
    def scored(self) -> int:
        return self.cases - self.skipped


def summarize(results: list[CaseResult]) -> Summary:
    """Skipped cases are left out of every rate: they measure the machine, not the tool."""
    scored = [r for r in results if not r.skipped]
    judged = [(r, key) for r in scored for key in r.claim_keys()]
    return Summary(
        cases=len(results),
        skipped=len(results) - len(scored),
        claims_total=len(judged),
        claims_correct=sum(r.claim_correct(key) for r, key in judged),
        verdicts_correct=sum(r.verdict_correct for r in scored),
        wrong_confirmed=sum(r.wrong_confirmed for r in scored),
        wrong_refuted=sum(r.wrong_refuted for r in scored),
        unverified=sum(r.runs[0].overall == "unverified" for r in scored),
        expected_unverified=sum(r.case.overall == "unverified" for r in scored),
        repeatable=sum(r.repeatable for r in scored),
    )


def _implied_outcome(claims: dict[str, str]) -> str:
    return Verdict([Evidence(Claim(key, key), Status(status), "") for key, status in claims.items()]).outcome


def load_case(path: Path) -> Case:
    """Read and check one case. Anything ambiguous is an error: a sloppy truth file would make
    every number it feeds meaningless."""
    def fail(message: str) -> CaseError:
        return CaseError(f"{path.name}: {message}")

    try:
        data = json.loads((path / "case.json").read_text())
    except FileNotFoundError:
        raise fail("no case.json") from None
    except ValueError as error:
        raise fail(f"case.json is not valid JSON: {error}") from None
    if not isinstance(data, dict):
        raise fail("case.json must be a JSON object")
    if unknown := set(data) - _CASE_KEYS:
        raise fail(f"unknown key(s) in case.json: {', '.join(sorted(unknown))}")
    truth = data.get("truth")
    if not isinstance(truth, str) or not truth.strip():
        raise fail("case.json needs a 'truth' string saying why the expected answers are right")
    expected = data.get("expected")
    if not isinstance(expected, dict) or not isinstance(expected.get("claims"), dict):
        raise fail("case.json needs 'expected': {'claims': {...}, 'overall': ...}")
    claims = expected["claims"]
    for key, status in claims.items():
        if not _CLAIM_KEY.match(key):
            raise fail(f"unknown claim {key!r}; use symbol:<name>, pr_tests, repro or version")
        if status not in _STATUSES:
            raise fail(f"claim {key!r} has status {status!r}; use one of {', '.join(_STATUSES)}")
    overall = expected.get("overall")
    if overall not in OUTCOMES:
        raise fail(f"overall must be one of {', '.join(OUTCOMES)}, not {overall!r}")
    if overall != (implied := _implied_outcome(claims)):
        raise fail(f"overall {overall!r} does not follow from the claims, which make it {implied!r}")

    repro, expect = data.get("repro"), data.get("expect", "")
    if not (repro is None or isinstance(repro, str)) or not isinstance(expect, str):
        raise fail("'repro' must be a string or null and 'expect' a string")
    if repro and "repro" not in claims:
        raise fail("has a repro command but no expected 'repro' claim")
    if "repro" in claims and not repro:
        raise fail("expects a 'repro' claim but gives no repro command")
    if not (path / "repo").is_dir():
        raise fail("no repo/ directory")
    if not (path / "report.txt").is_file():
        raise fail("no report.txt")
    diff_path = path / "pr.diff"
    diff = diff_path.read_text() if diff_path.is_file() else None
    if diff is not None and "pr_tests" not in claims:
        raise fail("has a pr.diff but no expected 'pr_tests' claim")
    if diff is None and "pr_tests" in claims:
        raise fail("expects a 'pr_tests' claim but has no pr.diff")
    return Case(path.name, path / "repo", (path / "report.txt").read_text(), diff, repro or None, expect,
                dict(claims), overall, truth.strip())


def load_cases(root: Path) -> list[Case]:
    if not root.is_dir():
        raise CaseError(f"cases directory not found: {root}")
    return [load_case(path) for path in sorted(root.iterdir()) if path.is_dir()]


def _claim_key(claim: Claim) -> str:
    return f"symbol:{claim.target}" if claim.kind == "symbol" else claim.kind


def run_once(case: Case, use_model: bool = False, run_repro: ReproRunner | None = None) -> Run:
    start = time.perf_counter()
    runner = run_repro if run_repro is not None else sandbox.run_repro

    def guarded_repro(repo: Path, command: str, expect: str) -> Evidence:
        try:
            return runner(repo, command, expect)
        except (OSError, subprocess.SubprocessError) as error:
            claim = Claim("repro", f"`{command}` reproduces the reported failure", command)
            return Evidence(claim, Status.UNVERIFIED, f"executor setup failure: {error}", "executor setup failure")

    verdict, model = cli.run_check(case.repo, case.report, case.diff, case.repro, case.expect, use_model=use_model,
                                   run_repro=guarded_repro)
    seconds = _seconds(time.perf_counter() - start)  # rounded once, so every total adds up from what is shown
    source = next((e.source for e in verdict.evidence if e.claim.kind == "repro"), None)
    return Run({_claim_key(e.claim): e.status.value for e in verdict.evidence}, verdict.outcome, model, seconds, source)


def run_case(case: Case, runs: int, isolated: bool, use_model: bool = False,
             run_repro: ReproRunner | None = None) -> CaseResult:
    """Without isolation the tool rightly refuses to run a repro. Scoring that refusal against
    the truth would measure the machine, so the case is skipped instead."""
    if case.repro and not isolated:
        return CaseResult(case, [], SKIP_NO_ISOLATION)
    return CaseResult(case, [run_once(case, use_model, run_repro) for _ in range(runs)])


def evaluate(cases: list[Case], runs: int, isolated: bool, use_model: bool = False,
             run_repro: ReproRunner | None = None) -> list[CaseResult]:
    return [run_case(case, runs, isolated, use_model, run_repro) for case in cases]


def _egress_counts(results: list[CaseResult]) -> tuple[int, int]:
    repro_runs = [run for result in results if result.case.repro for run in result.runs]
    verified = sum(bool(_VERIFIED_EGRESS.match(run.repro_source or "")) for run in repro_runs)
    return verified, len(repro_runs)


def egress_verified(results: list[CaseResult]) -> bool | None:
    """Only executor evidence of a successful probe verifies egress; no repro runs means n/a."""
    verified, total = _egress_counts(results)
    return verified == total if total else None


def _executor_info(results: list[CaseResult], meta: dict[str, str]) -> dict:
    return {"name": meta.get("executor", "local"), "context": meta.get("kube_context"),
            "namespace": meta.get("k8s_namespace"), "egress_verified": egress_verified(results)}


def ollama_unavailable(model: str, timeout: int = 5) -> str:
    """Why the model cannot be used, or "" if Ollama answers and has it pulled."""
    base = extract.OLLAMA_URL.removesuffix("/api/generate")
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=timeout) as response:
            tags = json.loads(response.read())
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as error:
        return f"Ollama not reachable at {base} ({error})"
    models = tags.get("models", []) if isinstance(tags, dict) else []
    if model not in [m.get("name") for m in models if isinstance(m, dict)]:
        return f"Ollama at {base} does not have {model} (run `ollama pull {model}`)"
    return ""


def name_differences(result: CaseResult) -> tuple[list[str], list[str], list[str], list[str]] | None:
    """(regex names, model run names, added by the model, dropped by the model), or None when the
    model run checked exactly the names regex extraction finds. Names from every run are pooled."""
    regex = sorted(c.target for c in extract.regex_extract(result.case.report).claims)
    model = sorted({name for run in result.runs for name in run.names})
    if not result.runs or model == regex:
        return None
    return regex, model, [n for n in model if n not in regex], [n for n in regex if n not in model]


def _ratio(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.0%})" if whole else "0/0 (n/a)"


def _seconds(value: float) -> float:
    return round(value, 3)


def timing(results: list[CaseResult]) -> dict:
    """Wall-clock totals, shared by the markdown and the JSON so the two cannot disagree."""
    ran = sorted((r for r in results if r.runs), key=lambda r: r.seconds, reverse=True)
    return {
        "total_seconds": _seconds(sum(run.seconds for r in results for run in r.runs)),
        "runs": sum(len(r.runs) for r in results),
        "slowest": [{"case": r.case.name, "seconds": r.seconds} for r in ran[:SLOWEST]],
    }


def _label(expected: str, got: list[str]) -> str:
    if all(g == expected for g in got):
        return "ok"
    wrong = {g for g in got if g != expected}
    if expected != NOT_CLAIMED and wrong & {"confirmed", "supported"}:
        return "wrong confirmed"
    if expected != NOT_CLAIMED and "refuted" in wrong:
        return "wrong refuted"
    return "miss"


def _shown(got: list[str]) -> str:
    return got[0] if len(set(got)) == 1 else " / ".join(got)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _case_rows(result: CaseResult) -> list[tuple[str, str, list[str]]]:
    rows = [(f"`{key}`", result.expected(key), result.got(key)) for key in result.claim_keys()]
    return rows + [("verdict", result.case.overall, [run.overall for run in result.runs])]


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _model_lines(results: list[CaseResult], model: str) -> list[str]:
    """What extraction really ran, counted from the model value run_check returned for each run."""
    done = [(r.case.name, run) for r in results for run in r.runs]
    if not done:
        return []
    fell_back = [name for name, run in done if run.model != model]
    line = f"- Extraction: {model} via Ollama in {len(done) - len(fell_back)}/{len(done)} runs"
    if not fell_back:
        return [line]
    line += f"; {_plural(len(fell_back), 'run')} fell back to regex extraction ({', '.join(dict.fromkeys(fell_back))})"
    return [line, "", f"**Warning: {model} was not used in every run. Runs that fell back measure regex extraction, "
                      "not the model, and are still counted below.**"]


def _difference_lines(results: list[CaseResult]) -> list[str]:
    rows = [(r.case.name, d) for r in results if not r.skipped and (d := name_differences(r))]
    lines = ["", "## Names that differ from regex extraction", "",
             "Names the model run checked that `claimcheck.extract.regex_extract` does not find in the report, or "
             "the reverse. Names from every run are pooled.", ""]
    if not rows:
        return lines + ["None: the model run checked exactly the names regex extraction finds."]
    lines += ["| Case | Regex names | Model run names | Added by model | Dropped by model |", "|---|---|---|---|---|"]
    for name, columns in rows:
        cells = [name] + [", ".join(names) for names in columns]
        lines.append("|" + "|".join(f" {cell} " if cell else " " for cell in cells) + "|")
    return lines


def to_markdown(results: list[CaseResult], meta: dict[str, str]) -> str:
    s, t = summarize(results), timing(results)
    executor = _executor_info(results, meta)
    model = meta.get("model")
    model_skipped = any(r.skipped == SKIP_NO_MODEL for r in results)
    runs = meta["runs"] + (" run" if meta["runs"] == "1" else " runs")
    revision = f"`{meta['revision']}`" + (" with uncommitted changes" if meta.get("uncommitted") else "")
    command = "python -m claimcheck.evaluate" + (f" --model {model}" if model else "") + f" --runs {meta['runs']}"
    if executor["name"] == "kubernetes":
        command += " --executor kubernetes"
        if executor["context"]:
            command += f" --kube-context {executor['context']}"
        command += f" --k8s-namespace {executor['namespace']}"
        executor_name = f"kubernetes (context {executor['context'] or 'current'}, namespace {executor['namespace']})"
        repro_pipeline = "Kubernetes (`claimcheck.k8s.KubernetesExecutor.run_repro`)"
    else:
        executor_name = "local sandbox"
        repro_pipeline = "the local sandbox (`claimcheck.sandbox.run_repro`)"
    verified, total = _egress_counts(results)
    egress = f"{'yes' if executor['egress_verified'] else 'no'} ({verified}/{total} repro runs)" if total else "n/a (no repro runs)"
    if model:
        pipeline = (f"- Pipeline: `claimcheck.cli.run_check` with `use_model=True` ({model} extraction through "
                    f"`claimcheck.extract.extract_claims`, regex names merged in) and {repro_pipeline}")
    else:
        pipeline = f"- Pipeline: `claimcheck.cli.run_check` with `use_model=False` (regex extraction) and {repro_pipeline}"
    lines = [
        "# claimcheck evaluation" + (f": {model} extraction" if model else ""),
        "",
        f"Generated by `{command}` from a real run; rerun it rather than editing this file.",
        "",
        f"- Date: {meta['date']}",
        f"- Machine: {meta['platform']}, Python {meta['python']}",
        f"- Git revision: {revision}",
        f"- Sandbox: {meta['isolation']}",
        f"- Executor: {executor_name}",
        f"- Egress verified: {egress}",
        f"- Cases: {s.cases} ({s.scored} scored, {s.skipped} skipped), {runs} each",
        pipeline,
        f"- Wall-clock: {t['total_seconds']:.3f}s over {_plural(t['runs'], 'run')} (sum of per-run times)",
    ]
    if model:
        lines += _model_lines(results, model)
    if model_skipped:
        lines += ["", f"**Skipped: {meta.get('skip_reason', 'Ollama unreachable')}. No case was run with {model}.**"]
    lines += [
        "",
        "## Results",
        "",
        "| Metric | Result |",
        "|---|---|",
        f"| Per-claim accuracy | {_ratio(s.claims_correct, s.claims_total)} |",
        f"| Overall-verdict accuracy | {_ratio(s.verdicts_correct, s.scored)} |",
        f"| Wrong confirmed (cases) | {s.wrong_confirmed} |",
        f"| Wrong refuted (cases) | {s.wrong_refuted} |",
        f"| Unverified rate (verdicts) | {_ratio(s.unverified, s.scored)}; ground truth {_ratio(s.expected_unverified, s.scored)} |",
        f"| Repeatability (identical across {runs}) | {_ratio(s.repeatable, s.scored)} |",
        f"| Skipped ({'Ollama unreachable' if model_skipped else 'no sandbox isolation'}) | {s.skipped} |",
        "",
        "Each case's expected answers come from what is true about its tiny repo, not from tool output. A claim counts "
        "as correct only if every run gave the true status. A claim the tool missed, or one it produced that the report "
        "does not make, is a miss. Wrong confirmed and wrong refuted count cases where any run called a claimed fact "
        "confirmed or refuted (or the verdict supported or refuted) against the truth. When the truth is itself "
        "unverified (the evidence cannot settle the claim), unverified is the correct answer. When the truth is "
        "definite, unverified is a miss, never a wrong answer.",
        "",
        "## Misses",
        "",
    ]
    misses = [r for r in results if not r.skipped and not r.correct]
    for r in misses:
        wrong = [f"{what} expected {exp}, got {_shown(got)}" for what, exp, got in _case_rows(r) if _label(exp, got) != "ok"]
        lines.append(f"- {r.case.name}: {'; '.join(wrong)}")
    if not misses:
        lines.append("None.")
    if model and not model_skipped:
        lines += _difference_lines(results)
    lines += ["", "## Slowest cases", ""]
    if t["slowest"]:
        lines += ["| Case | Seconds (all runs) |", "|---|---|"]
        lines += [f"| {slow['case']} | {slow['seconds']:.3f} |" for slow in t["slowest"]]
    else:
        lines.append("None: no case was run.")
    lines += ["", "## Per case", "", "| Case | Claim | Expected | Got | |", "|---|---|---|---|---|"]
    for r in results:
        if r.skipped:
            lines.append(f"| {r.case.name} | | | | {r.skipped} |")
            continue
        lines += [f"| {r.case.name} | {what} | {exp} | {_shown(got)} | {_label(exp, got)} |" for what, exp, got in _case_rows(r)]
    lines += ["", "## Ground truth", "", "| Case | Why the expected answers are right |", "|---|---|"]
    lines += [f"| {r.case.name} | {_cell(r.case.truth)} |" for r in results]
    return "\n".join(lines) + "\n"


def _answer(expected: str, got: list[str]) -> dict:
    label = _label(expected, got)
    return {"expected": expected, "got": got, "label": label, "correct": label == "ok"}


def _case_json(r: CaseResult) -> dict:
    return {
        "name": r.case.name,
        "skipped": r.skipped or None,
        "correct": not r.skipped and r.correct,
        "repeatable": not r.skipped and r.repeatable,
        "wrong_confirmed": not r.skipped and r.wrong_confirmed,
        "wrong_refuted": not r.skipped and r.wrong_refuted,
        "seconds": r.seconds,
        "claims": [] if r.skipped else [{"key": key, **_answer(r.expected(key), r.got(key))} for key in r.claim_keys()],
        "overall": None if r.skipped else _answer(r.case.overall, [run.overall for run in r.runs]),
        "runs": [{"seconds": run.seconds, "model": run.model, "claims": run.claims, "overall": run.overall,
                  "repro_source": run.repro_source}
                 for run in r.runs],
    }


def to_json(results: list[CaseResult], meta: dict[str, str]) -> dict:
    """The same results as to_markdown, as data; the schema is in the module docstring."""
    s = summarize(results)
    metrics = {name: getattr(s, name) for name in Summary.__dataclass_fields__}
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "generated": {key: meta.get(key, "") for key in ("date", "platform", "python", "revision", "isolation")}
                     | {"uncommitted": bool(meta.get("uncommitted"))},
        "model": meta.get("model"),
        "executor": _executor_info(results, meta),
        "skip_reason": meta.get("skip_reason"),
        "runs_per_case": int(meta["runs"]),
        "metrics": metrics | {"scored": s.scored},
        "timing": timing(results),
        "cases": [_case_json(r) for r in results],
    }


def _uncommitted() -> bool:
    try:
        done = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and bool(done.stdout.strip())


def machine_info(runs: int, isolation: tuple[str, list[str]] | None) -> dict[str, str]:
    """Where and on what code the numbers were measured; taken before any output is written."""
    return {
        "date": datetime.date.today().isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "revision": cli.git_revision(ROOT) or "unknown (not a git checkout)",
        "uncommitted": "yes" if _uncommitted() else "",
        "isolation": isolation[0] if isolation else "none (repro cases skipped)",
        "runs": str(runs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m claimcheck.evaluate",
                                     description="run claimcheck on cases with known answers and report its accuracy")
    parser.add_argument("--cases", type=Path, default=Path("eval/cases"))
    parser.add_argument("--runs", type=int, default=3, help="runs per case, to check the results repeat")
    parser.add_argument("--out", type=Path, help="write the markdown report here instead of stdout")
    parser.add_argument("--json", type=Path, help="also write the results as JSON here (schema in this module's docstring)")
    parser.add_argument("--model", help=f"extract through Ollama instead of regex; claimcheck.extract calls {extract.MODEL}")
    parser.add_argument("--executor", choices=["local", "kubernetes"], default="local",
                        help="where repros run: the local sandbox or Kubernetes Jobs")
    parser.add_argument("--kube-context", help="kubectl context to use")
    parser.add_argument("--k8s-namespace", default="claimcheck", help="namespace for Kubernetes Jobs")
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.model is not None and args.model != extract.MODEL:
        parser.error(f"--model {args.model}: claimcheck.extract only calls {extract.MODEL}")
    try:
        cases = load_cases(args.cases)
    except CaseError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    isolation = sandbox.detect_isolation() if args.executor == "local" else None
    meta = machine_info(args.runs, isolation)
    meta["executor"] = args.executor
    run_repro = sandbox.run_repro
    if args.executor == "kubernetes":
        run_repro = k8s.KubernetesExecutor(namespace=args.k8s_namespace, context=args.kube_context).run_repro
        meta["isolation"] = "kubernetes"
        meta["k8s_namespace"] = args.k8s_namespace
        if args.kube_context:
            meta["kube_context"] = args.kube_context
    unavailable = ollama_unavailable(args.model) if args.model else ""
    if args.model:
        meta["model"] = args.model
    if unavailable:
        meta["skip_reason"] = unavailable
        results = [CaseResult(case, [], SKIP_NO_MODEL) for case in cases]
    else:
        results = evaluate(cases, args.runs, args.executor == "kubernetes" or isolation is not None,
                           use_model=args.model is not None, run_repro=run_repro)
    text = to_markdown(results, meta)
    s = summarize(results)
    if unavailable:
        print(f"whole run skipped: {unavailable}", file=sys.stderr)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(to_json(results, meta), indent=2) + "\n")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text)
        used = ""
        if args.model and not unavailable:
            done = [run for r in results for run in r.runs]
            used = f", {args.model} used in {sum(run.model == args.model for run in done)}/{len(done)} runs"
        print(f"{args.out}: {s.cases} cases, claims {_ratio(s.claims_correct, s.claims_total)}, "
              f"verdicts {_ratio(s.verdicts_correct, s.scored)}, wrong confirmed {s.wrong_confirmed}, "
              f"wrong refuted {s.wrong_refuted}, skipped {s.skipped}{used}")
    else:
        print(text, end="")
    return 1 if s.wrong_confirmed or s.wrong_refuted else 0


if __name__ == "__main__":
    sys.exit(main())
