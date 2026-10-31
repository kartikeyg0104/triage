import socket
import threading

import pytest

from claimcheck import sandbox
from claimcheck.models import Claim, Status

needs_isolation = pytest.mark.skipif(sandbox.detect_isolation() is None, reason="no network isolation on this machine")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "boom.py").write_text("raise SystemExit('crash on long url')\n")
    (tmp_path / "ok.py").write_text("print('fine')\n")
    return tmp_path


def test_refuses_to_run_without_isolation(repo, monkeypatch):
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: None)
    evidence = sandbox.run_repro(repo, "python3 boom.py", "crash")
    assert evidence.status is Status.UNVERIFIED
    assert "refused" in evidence.detail


@needs_isolation
def test_reproduces_failure(repo):
    evidence = sandbox.run_repro(repo, "python3 boom.py", "crash on long url")
    assert evidence.status is Status.CONFIRMED


@needs_isolation
def test_not_reproduced_when_command_passes(repo):
    assert sandbox.run_repro(repo, "python3 ok.py", "crash").status is Status.REFUTED


@needs_isolation
def test_timeout_is_unverified(repo):
    assert sandbox.run_repro(repo, "sleep 5", "", timeout=1).status is Status.UNVERIFIED


@needs_isolation
def test_network_is_blocked(tmp_path):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    def accept_quietly():
        try:
            server.accept()
        except OSError:
            pass

    threading.Thread(target=accept_quietly, daemon=True).start()
    (tmp_path / "net.py").write_text(
        f"import socket\ns = socket.socket()\ns.settimeout(2)\ns.connect(('127.0.0.1', {port}))\nprint('CONNECTED')\n"
    )
    evidence = sandbox.run_repro(tmp_path, "python3 net.py", "")
    server.close()
    assert "CONNECTED" not in evidence.detail
    assert "Operation not permitted" in evidence.detail  # the connect was denied by the sandbox


@needs_isolation
def test_secrets_not_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "super-secret-value")
    (tmp_path / "env.py").write_text("import os\nprint(os.environ.get('GITHUB_TOKEN', 'absent'))\nraise SystemExit(1)\n")
    evidence = sandbox.run_repro(tmp_path, "python3 env.py", "absent")
    assert evidence.status is Status.CONFIRMED
    assert "super-secret-value" not in evidence.detail


@needs_isolation
def test_repo_is_copied_not_modified(tmp_path):
    (tmp_path / "w.py").write_text("open('created.txt','w').write('x')\nraise SystemExit(1)\n")
    sandbox.run_repro(tmp_path, "python3 w.py", "")
    assert not (tmp_path / "created.txt").exists()


@needs_isolation
def test_temp_path_is_not_leaked(tmp_path):
    (tmp_path / "p.py").write_text("import os\nraise RuntimeError(os.getcwd())\n")
    evidence = sandbox.run_repro(tmp_path, "python3 p.py", "RuntimeError")
    assert "/var/" not in evidence.detail and "/tmp/" not in evidence.detail
    assert "<repo>" in evidence.detail


CLAIM = Claim("repro", "r")


def test_nonzero_exit_without_expected_text_is_unverified():
    evidence = sandbox.classify(CLAIM, 1, "boom", "", "src")
    assert evidence.status is Status.UNVERIFIED
    assert "no expected failure text" in evidence.detail


def test_expected_text_confirms():
    assert sandbox.classify(CLAIM, 1, "Traceback: buffer overflow", "buffer overflow", "src").status is Status.CONFIRMED


def test_different_failure_is_unverified():
    assert sandbox.classify(CLAIM, 1, "KeyError: x", "buffer overflow", "src").status is Status.UNVERIFIED


def test_success_is_refuted():
    assert sandbox.classify(CLAIM, 0, "fine", "buffer overflow", "src").status is Status.REFUTED


@pytest.mark.parametrize("code,output", [
    (127, "sh: poc: command not found"),
    (1, "ModuleNotFoundError: No module named 'numpy'"),
    (1, "python3: can't open file: No such file or directory"),
    (126, ""),
])
def test_setup_failures_are_never_confirmed_or_refuted(code, output):
    evidence = sandbox.classify(CLAIM, code, output + " buffer overflow", "buffer overflow", "src")
    assert evidence.status is Status.UNVERIFIED
    assert "setup or environment failure" in evidence.detail
