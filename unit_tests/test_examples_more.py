"""The JS and fix-verification examples: saved outputs must be what the real CLI produces today."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from claimcheck import sandbox

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


def normalise(text: str) -> str:
    text = re.sub(r"\b(sandbox-exec|unshare-net): ", "SANDBOX: ", text)
    text = re.sub(r"Tested revision: .*", "Tested revision: REV", text)
    text = re.sub(r"\b[0-9a-f]{40}\b", "SHA", text)
    return re.sub(r"\b[0-9a-f]{12}\b", "SHA", text)


def run_script(name: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHON": sys.executable}
    return subprocess.run(["sh", str(EXAMPLES / name)], cwd=ROOT, env=env, capture_output=True, text=True)


def test_js_example_inputs_exist():
    for name in ("js_repo/src/slug.js", "js_report.txt", "run_js.sh", "output_js.md",
                 "fix_verification.sh", "output_fix_verification.md", "README.md"):
        assert (EXAMPLES / name).is_file(), name


def test_js_saved_output_matches_real_run():
    proc = run_script("run_js.sh")
    assert proc.returncode == 1, proc.stderr  # refuted: one claimed function does not exist
    saved = (EXAMPLES / "output_js.md").read_text()
    assert normalise(proc.stdout) == normalise(saved)
    assert "✓ confirmed" in saved and "✗ refuted" in saved


@pytest.mark.skipif(sandbox.detect_isolation() is None, reason="needs sandbox isolation")
def test_fix_verification_saved_output_matches_real_run():
    proc = run_script("fix_verification.sh")
    assert proc.returncode == 0, proc.stdout + proc.stderr  # fix confirmed
    saved = (EXAMPLES / "output_fix_verification.md").read_text()
    assert normalise(proc.stdout) == normalise(saved)
    assert "fix confirmed" in saved


def test_readme_lists_every_example_script():
    readme = (EXAMPLES / "README.md").read_text()
    for script in EXAMPLES.glob("*.sh"):
        assert script.name in readme, script.name
