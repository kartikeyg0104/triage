"""The saved example output must be what the CLI really produces today."""

import re
import subprocess
import sys
from pathlib import Path

import pytest

from claimcheck import sandbox

ROOT = Path(__file__).resolve().parent.parent


def normalise(text: str) -> str:
    text = re.sub(r"`(sandbox-exec|unshare-net|bwrap): ", "`SANDBOX: ", text)
    return re.sub(r"Tested revision: .*", "Tested revision: REV", text)


@pytest.mark.skipif(sandbox.detect_isolation() is None, reason="needs sandbox isolation")
def test_saved_output_matches_real_run():
    proc = subprocess.run(
        [sys.executable, "-m", "claimcheck", "check", "--repo", "examples/sample_repo", "--report", "examples/report.txt",
         "--diff", "examples/pr.diff", "--repro", "python3 poc.py", "--expect", "buffer overflow in parse_url", "--no-model"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 1  # refuted: parse_url_v2 does not exist
    assert normalise(proc.stdout) == normalise((ROOT / "examples/output_no_model.md").read_text())


def test_example_inputs_exist():
    for name in ("report.txt", "pr.diff", "sample_repo/urls.py", "sample_repo/poc.py", "output_gemma.md"):
        assert (ROOT / "examples" / name).is_file()
