import os
import signal
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
        "import socket, sys\ns = socket.socket()\ns.settimeout(2)\n"
        f"try:\n    s.connect(('127.0.0.1', {port}))\nexcept OSError as e:\n    print('BLOCKED', e)\n    sys.exit(1)\nprint('CONNECTED')\n"
    )
    evidence = sandbox.run_repro(tmp_path, "python3 net.py", "BLOCKED")
    server.close()
    assert "CONNECTED" not in evidence.detail
    assert evidence.status is Status.CONFIRMED  # the script saw its connect fail inside the sandbox


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


HOST_SECRET = "BEGIN-HOST-PRIVATE-KEY-7f3a9c"


@pytest.fixture
def repo_beside_secret(tmp_path):
    """The secret lives outside the repo, the way ~/.ssh/id_rsa lives outside a checkout."""
    secret = tmp_path / "id_rsa"
    secret.write_text(HOST_SECRET + "\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo, secret


@needs_isolation
@pytest.mark.parametrize("absolute", [True, False])
def test_symlink_to_host_file_is_not_leaked(repo_beside_secret, absolute):
    repo, secret = repo_beside_secret
    (repo / "leak").symlink_to(secret if absolute else "../id_rsa")
    evidence = sandbox.run_repro(repo, "cat leak; exit 1", HOST_SECRET)
    assert HOST_SECRET not in evidence.detail
    assert evidence.status is not Status.CONFIRMED
    assert "1 symlink pointing outside the repo was not copied" in evidence.detail


@needs_isolation
def test_symlink_to_host_directory_is_not_leaked(repo_beside_secret):
    repo, secret = repo_beside_secret
    (repo / "ssh").symlink_to(secret.parent)
    evidence = sandbox.run_repro(repo, "cat ssh/id_rsa; exit 1", HOST_SECRET)
    assert HOST_SECRET not in evidence.detail


@needs_isolation
def test_inside_repo_symlink_still_works(repo_beside_secret):
    repo, _ = repo_beside_secret
    (repo / "data.txt").write_text("inside-content\n")
    (repo / "alias").symlink_to("data.txt")
    evidence = sandbox.run_repro(repo, "cat alias; exit 1", "inside-content")
    assert evidence.status is Status.CONFIRMED
    assert "not copied" not in evidence.detail


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


# Resource limits (issue #49). Each test lowers one ceiling so it trips quickly and cheaply.

@needs_isolation
def test_cpu_spinner_is_stopped_by_a_resource_limit(tmp_path, monkeypatch):
    monkeypatch.setitem(sandbox.LIMITS, "cpu_seconds", 1)
    (tmp_path / "spin.py").write_text("while True:\n    pass\n")
    evidence = sandbox.run_repro(tmp_path, "python3 spin.py", "", timeout=8)
    assert evidence.status is Status.UNVERIFIED
    assert "resource limit" in evidence.detail
    assert "timed out" not in evidence.detail


@needs_isolation
def test_huge_file_write_is_stopped_by_a_resource_limit(tmp_path, monkeypatch):
    monkeypatch.setitem(sandbox.LIMITS, "file_bytes", 8 << 20)
    (tmp_path / "fill.py").write_text(
        "with open('big.bin', 'wb') as f:\n    for _ in range(64):\n        f.write(b'x' * (1 << 20))\n"
        "print('WROTE ALL')\nraise SystemExit(1)\n"
    )
    evidence = sandbox.run_repro(tmp_path, "python3 fill.py", "WROTE ALL")
    assert evidence.status is Status.UNVERIFIED
    assert "resource limit" in evidence.detail
    assert "WROTE ALL" not in evidence.detail


@needs_isolation
def test_memory_hog_is_stopped_where_the_os_enforces_it(tmp_path, monkeypatch):
    enforced, reason = sandbox.memory_limit_enforced()
    if not enforced:
        pytest.skip(f"memory limit not enforced here: {reason}")
    monkeypatch.setitem(sandbox.LIMITS, "memory_bytes", 512 << 20)
    (tmp_path / "hog.py").write_text("b = bytearray(2 << 30)\nprint('ALLOCATED')\nraise SystemExit(1)\n")
    evidence = sandbox.run_repro(tmp_path, "python3 hog.py", "ALLOCATED")
    assert evidence.status is Status.UNVERIFIED
    assert "resource limit" in evidence.detail
    assert "ALLOCATED" not in evidence.detail


@needs_isolation
def test_unenforced_memory_limit_is_recorded_when_a_run_is_stopped(tmp_path, monkeypatch):
    enforced, reason = sandbox.memory_limit_enforced()
    if enforced:
        pytest.skip("the OS enforces the memory limit, nothing to record")
    monkeypatch.setitem(sandbox.LIMITS, "cpu_seconds", 1)
    (tmp_path / "spin.py").write_text("while True:\n    pass\n")
    stopped = sandbox.run_repro(tmp_path, "python3 spin.py", "", timeout=8)
    timed_out = sandbox.run_repro(tmp_path, "sleep 5", "", timeout=1)
    assert "memory limit not enforced" in stopped.detail
    assert "memory limit not enforced" in timed_out.detail


@needs_isolation
@pytest.mark.skipif(os.geteuid() == 0, reason="the process limit does not apply to root")
def test_bounded_fork_loop_is_stopped_by_a_resource_limit(tmp_path, monkeypatch):
    monkeypatch.setitem(sandbox.LIMITS, "extra_processes", 32)
    (tmp_path / "forks.py").write_text(
        "import os, sys, time\n"
        "for _ in range(300):\n"
        "    try:\n        pid = os.fork()\n"
        "    except OSError as e:\n        print('FORK FAILED', e)\n        sys.exit(1)\n"
        "    if pid == 0:\n        os.close(0); os.close(1); os.close(2)\n        time.sleep(3)\n        os._exit(0)\n"
        "print('FORKED ALL')\nsys.exit(1)\n"
    )
    evidence = sandbox.run_repro(tmp_path, "python3 forks.py", "FORK FAILED")
    assert evidence.status is Status.UNVERIFIED
    assert "resource limit" in evidence.detail
    assert "FORKED ALL" not in evidence.detail


@needs_isolation
def test_normal_repro_still_confirms_under_default_limits(repo):
    evidence = sandbox.run_repro(repo, "for i in 1 2 3; do python3 ok.py; done; python3 boom.py", "crash on long url")
    assert evidence.status is Status.CONFIRMED
    assert "resource limit" not in evidence.detail


@pytest.mark.parametrize("code,output", [
    (-signal.SIGXCPU, ""),
    (128 + signal.SIGXCPU, ""),
    (-signal.SIGXFSZ, ""),
    (128 + signal.SIGXFSZ, ""),
    (-signal.SIGKILL, ""),
    (137, ""),
    (1, "BlockingIOError: [Errno 35] Resource temporarily unavailable"),
    (1, "sh: fork: Resource temporarily unavailable"),
    (1, "Traceback ... MemoryError"),
    (0, "MemoryError caught, carrying on"),
])
def test_limit_hits_are_recognised(code, output):
    assert sandbox.limit_hit(code, output)


@pytest.mark.parametrize("code,output", [(1, "KeyError: x"), (0, "fine"), (2, "usage: poc")])
def test_ordinary_failures_are_not_limit_hits(code, output):
    assert sandbox.limit_hit(code, output) is None
