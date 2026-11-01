import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from claimcheck import sandbox
from claimcheck.models import Claim, Status

needs_isolation = pytest.mark.skipif(sandbox.detect_isolation() is None, reason="no network isolation on this machine")
needs_bwrap = pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("bwrap"),
                                 reason="bubblewrap requires Linux and bwrap")
needs_macos = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"),
                                 reason="home-read confinement requires macOS and sandbox-exec")


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


@pytest.fixture
def linux_wrappers(monkeypatch):
    """Exercise Linux wrapper selection even on hosts that cannot create namespaces."""
    available = {"bwrap", "unshare"}
    probes = []
    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda tool: f"/usr/bin/{tool}" if tool in available else None)

    def probe(args, **kwargs):
        probes.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sandbox.subprocess, "run", probe)
    return available, probes


def test_linux_prefers_bwrap_and_mounts_only_readonly_system_dirs(linux_wrappers):
    _, probes = linux_wrappers
    name, prefix = sandbox.detect_isolation()
    assert name == "bwrap"
    assert prefix[0] == "bwrap"
    assert "--unshare-all" in prefix
    assert "--die-with-parent" in prefix
    assert prefix[prefix.index("--tmpfs") + 1] == "/tmp"
    assert prefix[prefix.index("--proc") + 1] == "/proc"
    assert prefix[prefix.index("--dev") + 1] == "/dev"
    mounts = [prefix[i + 1:i + 3] for i, arg in enumerate(prefix) if arg == "--ro-bind"]
    expected = [path for path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc") if Path(path).is_dir()]
    assert mounts == [[path, path] for path in expected]
    assert "--bind" not in prefix
    assert len(probes) == 1 and probes[0][0] == "bwrap"
    assert probes[0][-1] == "true"


def test_bwrap_omits_system_dirs_that_do_not_exist(linux_wrappers, monkeypatch):
    monkeypatch.setattr(Path, "is_dir", lambda path: str(path) in ("/usr", "/bin", "/etc"))
    name, prefix = sandbox.detect_isolation()
    assert name == "bwrap"
    assert "/lib" not in prefix and "/lib64" not in prefix and "/sbin" not in prefix


def test_linux_uses_unshare_when_bwrap_is_missing(linux_wrappers):
    available, probes = linux_wrappers
    available.remove("bwrap")
    assert sandbox.detect_isolation() == ("unshare-net", ["unshare", "-rn"])
    assert probes == [["unshare", "-rn", "true"]]


@pytest.mark.parametrize("failure", [1, OSError("cannot start wrapper"), subprocess.TimeoutExpired("bwrap", 10)])
def test_unusable_bwrap_does_not_fall_back_to_host_file_access(linux_wrappers, monkeypatch, failure):
    _, probes = linux_wrappers

    def probe(args, **kwargs):
        probes.append(args)
        if isinstance(failure, Exception):
            raise failure
        return subprocess.CompletedProcess(args, failure)

    monkeypatch.setattr(sandbox.subprocess, "run", probe)
    assert sandbox.detect_isolation() is None
    assert len(probes) == 1 and probes[0][0] == "bwrap"


def test_linux_refuses_when_both_wrappers_are_missing(linux_wrappers):
    available, probes = linux_wrappers
    available.clear()
    assert sandbox.detect_isolation() is None
    assert not probes


@pytest.fixture
def repro_process(monkeypatch):
    """Record the launch without running a fake isolation wrapper on the host."""
    launches = []
    result = {"returncode": 1, "stdout": "crash", "stderr": ""}

    class Process:
        def __init__(self, args, **kwargs):
            launches.append((args, kwargs))
            self.returncode = result["returncode"]

        def communicate(self, timeout=None):
            return result["stdout"], result["stderr"]

    monkeypatch.setattr(sandbox.subprocess, "Popen", Process)
    monkeypatch.setattr(sandbox, "_plan_limits", lambda: ([], [], []))
    monkeypatch.setattr(sandbox, "_kill_group", lambda proc: None)
    return launches, result


def test_bwrap_runs_in_the_bound_copy_with_a_private_home(repo, monkeypatch, repro_process):
    launches, _ = repro_process
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("bwrap", ["bwrap", "--unshare-all"]))
    evidence = sandbox.run_repro(repo, "python3 boom.py", "crash")
    args, options = launches[0]
    bind = args.index("--bind")
    assert args[bind + 1] == str(options["cwd"])
    assert Path(args[bind + 1]) != repo
    assert args[bind + 2] == "/repo"
    assert args[args.index("--chdir") + 1] == "/repo"
    assert args[-4:] == ["--", "sh", "-c", "python3 boom.py"]
    assert options["env"]["HOME"] == "/tmp"
    assert options["start_new_session"]
    assert evidence.status is Status.CONFIRMED
    assert evidence.source == "bwrap: python3 boom.py"


def test_unshare_launch_remains_unchanged(repo, monkeypatch, repro_process):
    launches, _ = repro_process
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("unshare-net", ["unshare", "-rn"]))
    evidence = sandbox.run_repro(repo, "python3 boom.py", "crash")
    args, options = launches[0]
    assert args == ["unshare", "-rn", "sh", "-c", "python3 boom.py"]
    assert options["env"]["HOME"] == str(options["cwd"].parent)
    assert evidence.status is Status.CONFIRMED
    assert evidence.source == "unshare-net: python3 boom.py"


def test_macos_probe_uses_the_home_read_deny_profile(tmp_path, monkeypatch):
    home = tmp_path / 'home "quoted" \\name'
    home.mkdir()
    alias = tmp_path / "home-alias"
    alias.symlink_to(home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(alias))
    monkeypatch.setattr(sandbox.sys, "platform", "darwin")
    monkeypatch.setattr(sandbox.shutil, "which", lambda tool: "/usr/bin/sandbox-exec")
    probes = []

    def probe(args, **kwargs):
        probes.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(sandbox.subprocess, "run", probe)
    name, prefix = sandbox.detect_isolation()
    assert name == "sandbox-exec"
    profile = prefix[prefix.index("-p") + 1]
    assert "(deny network*)" in profile
    assert '(deny file-read* (subpath (param "HOST_HOME")))' in profile
    assert '(allow file-read* (subpath (param "WORKDIR")))' in profile
    assert f"HOST_HOME={home.resolve()}" in prefix
    assert "WORKDIR=/dev/null" in prefix
    assert str(home) not in profile  # Paths are data, never sandbox profile syntax.
    assert probes == [prefix + ["true"]]


def test_macos_launch_allows_only_its_work_directory_under_home(repo, monkeypatch, repro_process):
    launches, _ = repro_process
    monkeypatch.setenv("HOME", str(repo.parent))
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("sandbox-exec", ["sandbox-exec", "-p", "probe"]))
    evidence = sandbox.run_repro(repo, "python3 boom.py", "crash")
    args, options = launches[0]
    profile = args[args.index("-p") + 1]
    assert '(deny file-read* (subpath (param "HOST_HOME")))' in profile
    assert '(allow file-read* (subpath (param "WORKDIR")))' in profile
    assert f"HOST_HOME={repo.parent.resolve()}" in args
    assert f"WORKDIR={options['cwd'].parent.resolve()}" in args
    assert options["env"]["HOME"] == str(options["cwd"].parent)
    assert args[-3:] == ["sh", "-c", "python3 boom.py"]
    assert evidence.status is Status.CONFIRMED
    assert evidence.source == "sandbox-exec: python3 boom.py"


@pytest.mark.parametrize("code", [0, 1])
@pytest.mark.parametrize("output", [
    "sandbox-exec: sandbox_apply: Operation not permitted",
    "sandbox-exec: string compilation failed: undefined parameter",
    "cat: /home/private/id_rsa: Operation not permitted",
    "PermissionError: [Errno 1] Operation not permitted: '/home/private/id_rsa'",
])
def test_macos_setup_or_read_denial_never_gives_a_verdict(repo, monkeypatch, repro_process, code, output):
    _, result = repro_process
    result.update(returncode=code, stdout="", stderr=output)
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("sandbox-exec", ["sandbox-exec"]))
    evidence = sandbox.run_repro(repo, "true", "Operation not permitted")
    assert evidence.status is Status.UNVERIFIED
    assert "setup or environment failure" in evidence.detail
    assert evidence.source.startswith("sandbox-exec:")


@pytest.fixture
def macos_home(tmp_path, monkeypatch):
    """Use a home-like directory so confinement tests never touch real host secrets."""
    home = tmp_path / 'home "quoted" \\name'
    home.mkdir()
    repo = home / "repo"
    repo.mkdir()
    work = home / "tmp"
    work.mkdir()
    private = home / ".ssh"
    private.mkdir()
    secret = private / "id_rsa"
    secret.write_text(HOST_SECRET + "\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sandbox.tempfile, "tempdir", str(work))
    return repo, secret


@needs_isolation
@needs_macos
@pytest.mark.parametrize("reader", ["cat", "python"])
@pytest.mark.parametrize("location", ["private", "original", "work-sibling", "alias"])
def test_macos_cannot_read_home_files_outside_its_copy(macos_home, reader, location):
    """Absolute paths and aliases must not bypass the copy's filesystem boundary."""
    repo, secret = macos_home
    if location == "original":
        secret = repo / "original.txt"
        secret.write_text(HOST_SECRET + "\n")
    elif location == "work-sibling":
        secret = repo.parent / "tmp" / "sibling.txt"
        secret.write_text(HOST_SECRET + "\n")
    elif location == "alias":
        alias = repo.parent.parent / "home-alias"
        alias.symlink_to(repo.parent, target_is_directory=True)
        secret = alias / ".ssh" / "id_rsa"
    assert secret.read_text().strip() == HOST_SECRET
    if reader == "cat":
        command = f"cat {shlex.quote(str(secret))}; exit 1"
    else:
        (repo / "read.py").write_text(f"from pathlib import Path\nprint(Path({str(secret)!r}).read_text())\n")
        command = "python3 read.py"
    evidence = sandbox.run_repro(repo, command, HOST_SECRET)
    assert evidence.source.startswith("sandbox-exec:"), evidence.detail
    assert HOST_SECRET not in evidence.detail
    assert evidence.status is Status.UNVERIFIED
    assert "Operation not permitted" in evidence.detail or "Permission denied" in evidence.detail


@needs_isolation
@needs_macos
def test_macos_copy_and_work_directory_remain_readable_and_writable(macos_home):
    repo, _ = macos_home
    (repo / "inside.txt").write_text("inside-content\n")
    (repo / "normal.py").write_text(
        "import json, os, tempfile\nfrom pathlib import Path\n"
        "assert Path('inside.txt').read_text().strip() == 'inside-content'\n"
        "Path('created.txt').write_text('copy-write')\n"
        "assert Path('created.txt').read_text() == 'copy-write'\n"
        "scratch = Path(os.environ['HOME']) / 'scratch.txt'\n"
        "scratch.write_text('work-write')\nassert scratch.read_text() == 'work-write'\n"
        "with tempfile.TemporaryFile() as f:\n    f.write(b'temp-write'); f.seek(0); assert f.read() == b'temp-write'\n"
        "raise RuntimeError(json.dumps('normal-repro'))\n"
    )
    evidence = sandbox.run_repro(repo, "python3 normal.py", "normal-repro")
    assert evidence.source.startswith("sandbox-exec:"), evidence.detail
    assert evidence.status is Status.CONFIRMED, evidence.detail
    assert not (repo / "created.txt").exists()
    assert not list((repo.parent / "tmp").glob("claimcheck-*"))


def test_bwrap_repo_path_is_normalised_in_evidence(repo, monkeypatch, repro_process):
    _, result = repro_process
    result["stdout"] = "RuntimeError: /repo/boom.py"
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("bwrap", ["bwrap"]))
    evidence = sandbox.run_repro(repo, "python3 boom.py", "RuntimeError")
    assert "<repo>/boom.py" in evidence.detail
    assert "/repo/" not in evidence.detail


@pytest.mark.parametrize("code", [0, 1])
def test_bwrap_setup_failure_never_gives_a_verdict(repo, monkeypatch, repro_process, code):
    _, result = repro_process
    result.update(returncode=code, stdout="", stderr="bwrap: Creating new namespace failed: Operation not permitted")
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("bwrap", ["bwrap"]))
    evidence = sandbox.run_repro(repo, "true", "Operation not permitted")
    assert evidence.status is Status.UNVERIFIED
    assert "setup" in evidence.detail
    assert "bwrap" in evidence.source


def test_wrapper_disappearing_after_detection_is_unverified(repo, monkeypatch, repro_process):
    def missing(*args, **kwargs):
        raise FileNotFoundError("wrapper disappeared")

    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("bwrap", ["bwrap"]))
    monkeypatch.setattr(sandbox.subprocess, "Popen", missing)
    evidence = sandbox.run_repro(repo, "true", "wrapper disappeared")
    assert evidence.status is Status.UNVERIFIED
    assert "refused" in evidence.detail
    assert "bwrap" in evidence.source


@needs_bwrap
@pytest.mark.parametrize("location", ["sibling", "home"])
def test_bwrap_cannot_read_a_host_file_outside_the_copy(tmp_path, location):
    """Direct absolute reads must be blocked too, without relying on symlink filtering."""
    repo = tmp_path / "repo"
    repo.mkdir()
    parent = Path.home() if location == "home" else tmp_path
    with sandbox.tempfile.TemporaryDirectory(prefix="claimcheck-host-", dir=parent) as host:
        secret = Path(host) / "id_rsa"
        secret.write_text(HOST_SECRET + "\n")
        evidence = sandbox.run_repro(repo, f"cat {shlex.quote(str(secret))}; exit 1", HOST_SECRET)
    assert evidence.source.startswith("bwrap:")
    assert HOST_SECRET not in evidence.detail
    assert evidence.status is Status.UNVERIFIED
    assert "No such file or directory" in evidence.detail


@needs_bwrap
def test_bwrap_copy_and_private_tmp_are_writable(tmp_path):
    (tmp_path / "inside.txt").write_text("inside-content\n")
    evidence = sandbox.run_repro(tmp_path,
                                 "cat inside.txt; touch created.txt /tmp/created.txt; "
                                 "test -f created.txt && test -f /tmp/created.txt", "")
    assert evidence.status is Status.REFUTED
    assert evidence.source.startswith("bwrap:")
    assert "inside-content" in evidence.detail
    assert not (tmp_path / "created.txt").exists()


@needs_bwrap
def test_bwrap_system_dirs_are_readonly(tmp_path):
    evidence = sandbox.run_repro(tmp_path,
                                 "python3 -c 'import os; assert os.statvfs(\"/usr\").f_flag & os.ST_RDONLY; "
                                 "print(\"READONLY\")'", "")
    assert evidence.source.startswith("bwrap:")
    assert evidence.status is Status.REFUTED
    assert "READONLY" in evidence.detail


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


def test_shell_cannot_fork_counts_as_a_limit_hit():
    # Linux dash reports a hit process limit as "sh: 1: Cannot fork"; seen on CI.
    assert sandbox.limit_hit(2, "sh: 1: Cannot fork")
