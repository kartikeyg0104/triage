"""deploy/kind/fetch-calico.sh must refuse a Calico manifest whose SHA-256 does not match the pinned value."""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KIND = ROOT / "deploy" / "kind"
FETCH = KIND / "fetch-calico.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None
    or shutil.which("curl") is None
    or (shutil.which("shasum") is None and shutil.which("sha256sum") is None),
    reason="needs sh, curl and shasum or sha256sum",
)

GOOD = b"apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: calico-config\n"


def fetch(url: str, sha: str, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(FETCH), url, sha, str(out)], capture_output=True, text=True, timeout=60)


def test_good_file_is_written(tmp_path):
    src = tmp_path / "calico.yaml"
    src.write_bytes(GOOD)
    out = tmp_path / "out" / "calico.yaml"
    out.parent.mkdir()
    proc = fetch(src.as_uri(), hashlib.sha256(GOOD).hexdigest(), out)
    assert proc.returncode == 0, proc.stderr
    assert out.read_bytes() == GOOD


def test_tampered_file_is_refused(tmp_path):
    src = tmp_path / "calico.yaml"
    src.write_bytes(GOOD + b"# injected\n")
    out = tmp_path / "out.yaml"
    proc = fetch(src.as_uri(), hashlib.sha256(GOOD).hexdigest(), out)
    assert proc.returncode != 0
    assert "mismatch" in proc.stderr.lower()
    assert hashlib.sha256(GOOD).hexdigest() in proc.stderr
    assert not out.exists(), "an unverified manifest must not be left at the output path"


def test_download_failure_is_refused(tmp_path):
    out = tmp_path / "out.yaml"
    proc = fetch((tmp_path / "missing.yaml").as_uri(), "0" * 64, out)
    assert proc.returncode != 0
    assert not out.exists()


def test_malformed_pin_is_refused(tmp_path):
    src = tmp_path / "calico.yaml"
    src.write_bytes(GOOD)
    proc = fetch(src.as_uri(), "", tmp_path / "out.yaml")
    assert proc.returncode != 0


def test_pinned_hash_file_is_well_formed():
    pin = (KIND / "calico.sha256").read_text().strip()
    assert re.fullmatch(r"[0-9a-f]{64}", pin), pin


def test_setup_applies_only_the_verified_local_file():
    setup = (KIND / "setup.sh").read_text()
    assert "fetch-calico.sh" in setup
    assert "calico.sha256" in setup
    assert not re.search(r"apply -f \"?https?://", setup), "setup.sh must not apply a remote URL directly"
