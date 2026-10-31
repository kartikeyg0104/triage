import os
import stat

import pytest

from claimcheck import repo_copy

SECRET = "host-secret-do-not-copy"


@pytest.fixture
def layout(tmp_path):
    """A repo next to (not inside) a directory holding a host secret."""
    repo = tmp_path / "repo"
    (repo / "sub").mkdir(parents=True)
    (repo / "a.txt").write_text("a\n")
    (repo / "sub" / "b.txt").write_text("b\n")
    outside = tmp_path / "outside"
    (outside / "dir").mkdir(parents=True)
    (outside / "secret.txt").write_text(SECRET)
    (outside / "dir" / "id_rsa").write_text(SECRET)
    return repo, outside, tmp_path / "copy"


def _skipped_paths(skipped):
    return {entry.path for entry in skipped}


def _no_secret_anywhere(dst):
    for dirpath, _, filenames in os.walk(dst):
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.isfile(path):
                assert SECRET not in open(path, errors="replace").read(), path


def test_absolute_link_out_is_dropped(layout):
    repo, outside, dst = layout
    (repo / "leak").symlink_to(outside / "secret.txt")
    skipped = repo_copy.copy_repo(repo, dst)
    assert not os.path.lexists(dst / "leak")
    assert "leak" in _skipped_paths(skipped)
    assert (dst / "a.txt").read_text() == "a\n"
    _no_secret_anywhere(dst)


def test_relative_dotdot_escape_is_dropped(layout):
    repo, _, dst = layout
    (repo / "sub" / "up").symlink_to("../../outside/secret.txt")
    skipped = repo_copy.copy_repo(repo, dst)
    assert not os.path.lexists(dst / "sub" / "up")
    assert os.path.join("sub", "up") in _skipped_paths(skipped)
    _no_secret_anywhere(dst)


def test_link_chain_that_escapes_is_dropped(layout):
    repo, outside, dst = layout
    (repo / "hop2").symlink_to(outside / "secret.txt")
    (repo / "hop1").symlink_to("hop2")
    skipped = repo_copy.copy_repo(repo, dst)
    assert not os.path.lexists(dst / "hop1")
    assert not os.path.lexists(dst / "hop2")
    assert {"hop1", "hop2"} <= _skipped_paths(skipped)


def test_chain_through_a_directory_link_that_escapes_is_dropped(layout):
    repo, outside, dst = layout
    (repo / "out").symlink_to(outside)
    (repo / "sub" / "via").symlink_to("../out/secret.txt")
    repo_copy.copy_repo(repo, dst)
    assert not os.path.lexists(dst / "sub" / "via")
    _no_secret_anywhere(dst)


def test_link_to_outside_directory_is_dropped_not_walked(layout):
    repo, outside, dst = layout
    (repo / "dirlink").symlink_to(outside / "dir")
    skipped = repo_copy.copy_repo(repo, dst)
    assert not os.path.lexists(dst / "dirlink")
    assert "dirlink" in _skipped_paths(skipped)
    _no_secret_anywhere(dst)


def test_inside_file_link_is_kept_as_a_relative_link(layout):
    repo, _, dst = layout
    (repo / "alias").symlink_to("sub/b.txt")
    skipped = repo_copy.copy_repo(repo, dst)
    assert skipped == []
    assert (dst / "alias").is_symlink()
    assert not os.path.isabs(os.readlink(dst / "alias"))
    assert (dst / "alias").read_text() == "b\n"


def test_absolute_link_inside_repo_points_into_the_copy_not_the_original(layout):
    repo, _, dst = layout
    (repo / "abs").symlink_to(repo / "a.txt")
    repo_copy.copy_repo(repo, dst)
    assert (dst / "abs").is_symlink()
    assert os.path.realpath(dst / "abs") == os.path.realpath(dst / "a.txt")


def test_inside_directory_links_are_not_followed_so_loops_terminate(layout):
    repo, _, dst = layout
    (repo / "sub" / "loop").symlink_to("..")
    (repo / "self").symlink_to(".")
    (repo / "subalias").symlink_to("sub")
    skipped = repo_copy.copy_repo(repo, dst)
    assert skipped == []
    for link in (dst / "sub" / "loop", dst / "self", dst / "subalias"):
        assert link.is_symlink()
    assert (dst / "subalias" / "b.txt").read_text() == "b\n"


def test_symlink_cycle_is_dropped(layout):
    repo, _, dst = layout
    (repo / "x").symlink_to("y")
    (repo / "y").symlink_to("x")
    skipped = repo_copy.copy_repo(repo, dst)
    assert not os.path.lexists(dst / "x") and not os.path.lexists(dst / "y")
    assert {"x", "y"} <= _skipped_paths(skipped)


def test_dangling_link_inside_is_kept_dangling(layout):
    repo, _, dst = layout
    (repo / "later").symlink_to("build/out.txt")
    skipped = repo_copy.copy_repo(repo, dst)
    assert skipped == []
    assert os.path.lexists(dst / "later") and not os.path.exists(dst / "later")
    assert not os.path.isabs(os.readlink(dst / "later"))


def test_dangling_link_outside_is_dropped(layout):
    repo, outside, dst = layout
    (repo / "nowhere").symlink_to(outside / "missing" / "file")
    skipped = repo_copy.copy_repo(repo, dst)
    assert not os.path.lexists(dst / "nowhere")
    assert "nowhere" in _skipped_paths(skipped)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_fifo_is_skipped(layout):
    repo, _, dst = layout
    os.mkfifo(repo / "pipe")
    skipped = repo_copy.copy_repo(repo, dst)
    assert not os.path.lexists(dst / "pipe")
    assert [(e.path, e.kind) for e in skipped] == [("pipe", repo_copy.SPECIAL)]


def test_ignore_list_is_kept(layout):
    repo, _, dst = layout
    for name in (".git", "node_modules", "__pycache__", ".venv"):
        (repo / name).mkdir()
        (repo / name / "x").write_text("x")
        (repo / "sub" / name).mkdir()
    repo_copy.copy_repo(repo, dst)
    for name in (".git", "node_modules", "__pycache__", ".venv"):
        assert not os.path.lexists(dst / name)
        assert not os.path.lexists(dst / "sub" / name)
    assert (dst / "sub" / "b.txt").exists()


def test_link_into_ignored_git_dir_does_not_reach_the_original(layout):
    repo, _, dst = layout
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text(SECRET)
    (repo / "cfg").symlink_to(repo / ".git" / "config")
    repo_copy.copy_repo(repo, dst)
    assert not os.path.exists(dst / "cfg")
    _no_secret_anywhere(dst)


def test_executable_bit_is_preserved(layout):
    repo, _, dst = layout
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    repo_copy.copy_repo(repo, dst)
    assert os.stat(dst / "run.sh").st_mode & stat.S_IXUSR


def test_hard_link_content_is_copied(layout):
    repo, _, dst = layout
    os.link(repo / "a.txt", repo / "hard.txt")
    assert repo_copy.copy_repo(repo, dst) == []
    assert (dst / "hard.txt").read_text() == "a\n"


def test_walk_yields_decisions_without_touching_disk(layout):
    repo, outside, _ = layout
    (repo / "leak").symlink_to(outside / "secret.txt")
    (repo / "alias").symlink_to("a.txt")
    decisions = {d.path: d for d in repo_copy.walk(repo)}
    assert decisions["a.txt"].kind == repo_copy.FILE
    assert decisions["sub"].kind == repo_copy.DIR
    assert decisions[os.path.join("sub", "b.txt")].kind == repo_copy.FILE
    assert decisions["alias"].kind == repo_copy.LINK and decisions["alias"].target == "a.txt"
    assert decisions["leak"].kind == repo_copy.OUTSIDE_LINK


def test_describe_skipped_counts_kinds(layout):
    repo, outside, dst = layout
    (repo / "l1").symlink_to(outside / "secret.txt")
    (repo / "l2").symlink_to("../outside/dir")
    if hasattr(os, "mkfifo"):
        os.mkfifo(repo / "pipe")
    note = repo_copy.describe_skipped(repo_copy.copy_repo(repo, dst))
    assert "2 symlinks pointing outside the repo were not copied" in note
    if hasattr(os, "mkfifo"):
        assert "1 special file" in note
    assert repo_copy.describe_skipped([]) == ""


def test_defaults_are_sensible():
    assert repo_copy.MAX_TOTAL_BYTES == 200 * 1024 * 1024
    assert repo_copy.MAX_FILES == 20_000
    assert repo_copy.MAX_FILE_BYTES == 50 * 1024 * 1024


def test_normal_repo_is_unaffected_by_the_caps(layout):
    repo, _, dst = layout
    assert repo_copy.copy_repo(repo, dst, max_total_bytes=4, max_files=3, max_file_bytes=2) == []
    assert (dst / "a.txt").read_text() == "a\n"
    assert (dst / "sub" / "b.txt").read_text() == "b\n"


def test_file_over_the_per_file_limit_is_skipped_and_reported(layout):
    repo, _, dst = layout
    (repo / "sub" / "big.bin").write_bytes(b"x" * 100)
    skipped = repo_copy.copy_repo(repo, dst, max_file_bytes=10)
    assert not os.path.lexists(dst / "sub" / "big.bin")
    assert (dst / "a.txt").read_text() == "a\n"
    assert [(e.path, e.kind) for e in skipped] == [(os.path.join("sub", "big.bin"), repo_copy.LARGE_FILE)]
    assert "1 file over the per-file size limit was not copied" in repo_copy.describe_skipped(skipped)


def test_skipped_large_files_do_not_count_toward_the_total(layout):
    repo, _, dst = layout
    (repo / "big.bin").write_bytes(b"x" * 100)
    skipped = repo_copy.copy_repo(repo, dst, max_total_bytes=10, max_file_bytes=10)
    assert [e.kind for e in skipped] == [repo_copy.LARGE_FILE]


def test_total_bytes_cap_stops_the_copy_and_removes_the_partial_copy(layout):
    repo, _, dst = layout
    (repo / "c.txt").write_bytes(b"c" * 8)
    with pytest.raises(repo_copy.RepoTooLarge) as caught:
        repo_copy.copy_repo(repo, dst, max_total_bytes=9)
    assert "repo too large to copy safely" in str(caught.value)
    assert not os.path.lexists(dst)


def test_file_count_cap_stops_the_copy_and_removes_the_partial_copy(layout):
    repo, _, dst = layout
    for i in range(5):
        (repo / f"f{i}.txt").write_text("")
    with pytest.raises(repo_copy.RepoTooLarge) as caught:
        repo_copy.copy_repo(repo, dst, max_files=4)
    assert "repo too large to copy safely" in str(caught.value)
    assert not os.path.lexists(dst)


def test_caps_are_read_from_module_defaults_at_call_time(layout, monkeypatch):
    repo, _, dst = layout
    monkeypatch.setattr(repo_copy, "MAX_FILES", 1)
    with pytest.raises(repo_copy.RepoTooLarge):
        repo_copy.copy_repo(repo, dst)


def test_sandbox_reports_a_too_large_repo_as_unverified_without_running(layout, monkeypatch):
    from claimcheck import sandbox
    from claimcheck.models import Status
    repo, _, _ = layout
    monkeypatch.setattr(sandbox, "detect_isolation", lambda: ("fake", []))
    monkeypatch.setattr(repo_copy, "MAX_FILES", 1)
    evidence = sandbox.run_repro(repo, "touch ran; exit 1", "")
    assert evidence.status is Status.UNVERIFIED
    assert "repo too large to copy safely" in evidence.detail
    assert not (repo / "ran").exists()
