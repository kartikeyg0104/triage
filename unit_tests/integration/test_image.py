"""Integration tests: the container image built from the Dockerfile.

Run with CLAIMCHECK_TEST_IMAGE=<image> after `docker build -t <image> .` (the CI `image` job does). Skipped otherwise.
Every container runs with a read-only root filesystem, as a non-root user.
"""

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

IMAGE = os.environ.get("CLAIMCHECK_TEST_IMAGE")
EXAMPLES = Path(__file__).resolve().parents[2] / "examples"

pytestmark = pytest.mark.skipif(not IMAGE, reason="set CLAIMCHECK_TEST_IMAGE to a built claimcheck image")


def docker(*args, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def run(*args):
    return docker("run", "--rm", "--read-only", "-v", f"{EXAMPLES}:/examples:ro", *args)


def test_help():
    proc = run(IMAGE, "--help")
    assert proc.returncode == 0, proc.stderr
    assert "check" in proc.stdout


def test_runs_as_non_root_without_compilers():
    probe = ("import json, os, shutil; "
             "print(json.dumps({'uid': os.getuid(), "
             "'tools': [t for t in ('gcc', 'cc', 'c++', 'make') if shutil.which(t)]}))")
    proc = run("--entrypoint", "python", IMAGE, "-c", probe)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"uid": 10001, "tools": []}


def test_check_example_is_refuted():
    proc = run(IMAGE, "check", "--no-model", "--repo", "/examples/sample_repo",
               "--report", "/examples/report.txt", "--diff", "/examples/pr.diff")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "refuted" in proc.stdout
    assert "parse_url_v2" in proc.stdout


def test_check_json_output():
    proc = run(IMAGE, "check", "--no-model", "--json", "--repo", "/examples/sample_repo",
               "--report", "/examples/report.txt")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["verdict"] == "refuted"


def test_check_reports_revision_of_a_mounted_git_checkout(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(EXAMPLES / "sample_repo", repo)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "sample"]):
        subprocess.run([*git, *args], check=True, capture_output=True)
    sha = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    proc = docker("run", "--rm", "--read-only", "-v", f"{repo}:/repo:ro", "-v", f"{EXAMPLES}:/examples:ro",
                  IMAGE, "check", "--no-model", "--json", "--repo", "/repo", "--report", "/examples/report.txt")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert json.loads(proc.stdout)["revision"] == sha


@pytest.fixture
def server():
    proc = docker("run", "-d", "--rm", "--read-only", "--tmpfs", "/tmp", "-p", "127.0.0.1::8080",
                  "-v", f"{EXAMPLES}:/examples:ro", "-e", f"CLAIMCHECK_API_TOKEN={TOKEN}", "--entrypoint", "python", IMAGE,
                  "-m", "claimcheck.server", "--db", "/tmp/claimcheck.db", "--repo-root", "/examples",
                  "--host", "0.0.0.0", "--port", "8080")
    assert proc.returncode == 0, proc.stderr
    container = proc.stdout.strip()
    try:
        port = docker("port", container, "8080/tcp").stdout.splitlines()[0].rsplit(":", 1)[1]
        yield f"http://127.0.0.1:{port}", container
    finally:
        docker("rm", "-f", container)


TOKEN = "smoke-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def get(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=AUTH), timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def post_json(url, body):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **AUTH})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def wait_until_up(base, container):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            return get(f"{base}/v1/investigations/does-not-exist")
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    pytest.fail("server did not answer: " + docker("logs", container).stderr)


def test_server_answers_404_for_unknown_investigation(server):
    base, container = server
    status, _ = wait_until_up(base, container)
    assert status == 404


def test_server_stores_an_investigation_under_tmp(server):
    base, container = server
    wait_until_up(base, container)
    status, body = post_json(f"{base}/v1/investigations",
                             {"repo": "sample_repo", "report": "RCE in parse_url_v2()"})
    assert status == 201, body
    assert body["result"]["verdict"] == "refuted"
    status, _ = get(f"{base}/v1/investigations/{body['id']}")
    assert status == 200
