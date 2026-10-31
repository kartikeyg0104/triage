"""The webhook worker: claim `received` rows atomically, run the static checks, store the result.

Unit tests drive the store and `process_one` in process; the integration tests use a real SQLite
file and real `python -m claimcheck.server.worker` processes racing over the same rows."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import closing
from pathlib import Path

import pytest

from claimcheck import revisions, sandbox
from claimcheck.server import worker
from claimcheck.server.store import Store

REPORT = "Crash in `parse_url()` and RCE in parse_url_v2()"
REPO = "octo/widgets"


@pytest.fixture
def root(tmp_path):
    repo = tmp_path / "repos" / "widgets"
    repo.mkdir(parents=True)
    (repo / "urls.py").write_text("def parse_url(s):\n    return s\n")
    return tmp_path / "repos"


@pytest.fixture
def db(tmp_path):
    return tmp_path / "claimcheck.db"


@pytest.fixture
def store(db):
    return Store(db)


@pytest.fixture
def repos(root):
    return {REPO.casefold(): root / "widgets"}


def receive(store: Store, repo: str = REPO, text: str = REPORT, delivery: str | None = None, number: int = 1):
    request = {"event": "issues", "action": "opened", "repo": repo, "number": number, "text": text, "claims": []}
    return store.create(f"github:issues:{repo}#{number}", "received", request, delivery_id=delivery)[0]


def statuses(db: Path) -> list[tuple]:
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT id, status, worker FROM investigations ORDER BY rowid").fetchall()


# Claiming


def test_claim_takes_the_oldest_received_row_once(store):
    first, second = receive(store, number=1), receive(store, number=2)
    claimed = store.claim("w1", stale_after=60)
    assert claimed.id == first.id and claimed.status == "processing" and claimed.worker == "w1"
    assert store.claim("w2", stale_after=60).id == second.id
    assert store.claim("w3", stale_after=60) is None


def test_claim_ignores_rows_that_are_not_received(store):
    store.create("api", "completed", {"repo": "x", "report": "r"}, {"verdict": "supported"})
    store.create("github:issues:o/r#1", "skipped", {"repo": "o/r"})
    assert store.claim("w1", stale_after=60) is None


def test_fresh_processing_row_is_not_reclaimed_but_a_stale_one_is(store):
    row = receive(store)
    assert store.claim("dead", stale_after=60, now=1000.0).id == row.id
    assert store.claim("w2", stale_after=60, now=1059.0) is None
    reclaimed = store.claim("w2", stale_after=60, now=1061.0)
    assert reclaimed.id == row.id and reclaimed.worker == "w2" and reclaimed.status == "processing"


def test_a_row_that_keeps_killing_its_worker_is_failed_after_max_attempts(store):
    row = receive(store)
    for attempt in range(3):
        assert store.claim(f"w{attempt}", stale_after=10, max_attempts=3, now=1000.0 + attempt * 20).id == row.id
    assert store.claim("w9", stale_after=10, max_attempts=3, now=1100.0) is None
    failed = store.get(row.id)
    assert failed.status == "failed" and "3 attempts" in failed.error


def test_finish_only_applies_for_the_worker_that_holds_the_claim(store):
    row = receive(store)
    store.claim("slow", stale_after=60, now=1000.0)
    store.claim("fast", stale_after=60, now=2000.0)
    assert store.finish(row.id, "slow", "completed", {"verdict": "stale"}) is False
    assert store.finish(row.id, "fast", "completed", {"verdict": "supported"}) is True
    assert store.finish(row.id, "fast", "failed", error="again") is False
    done = store.get(row.id)
    assert (done.status, done.result, done.worker, done.error) == ("completed", {"verdict": "supported"}, "fast", None)


def test_threads_sharing_one_file_never_claim_the_same_row(db):
    ids = {receive(Store(db), number=n).id for n in range(60)}
    claimed: list[str] = []
    lock = threading.Lock()

    def drain(name: str) -> None:
        store = Store(db)
        while (row := store.claim(name, stale_after=60)) is not None:
            with lock:
                claimed.append(row.id)

    threads = [threading.Thread(target=drain, args=(f"t{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(claimed) == sorted(ids)


def test_an_older_database_is_migrated_in_place(db):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE investigations (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL,"
                     " delivery_id TEXT UNIQUE NULL, status TEXT NOT NULL, request TEXT NOT NULL, result TEXT)")
        conn.execute("INSERT INTO investigations VALUES ('old', '2026-01-01T00:00:00+00:00', 'github:issues:o/r#1',"
                     " 'd', 'received', '{\"repo\": \"o/r\", \"text\": \"t\"}', NULL)")
    store = Store(db)
    assert store.get("old").status == "received" and store.get("old").error is None
    assert store.claim("w1", stale_after=60).id == "old"
    Store(db)  # a second open does not try to add the columns again


# Repo map


def test_repo_map_is_loaded_and_resolved_inside_the_root(tmp_path, root):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"Octo/Widgets": "widgets", "octo/other": "widgets/../widgets"}))
    assert worker.load_repo_map(path, root) == {"octo/widgets": (root / "widgets").resolve(),
                                                "octo/other": (root / "widgets").resolve()}


@pytest.mark.parametrize("content", [
    "not json",
    "[]",
    json.dumps({"o/r": 1}),
    json.dumps({"o/r": ".."}),
    json.dumps({"o/r": "/etc"}),
    json.dumps({"o/r": "missing"}),
    json.dumps({"o/r": "widgets/urls.py"}),
    json.dumps({"o/r": "escape"}),
])
def test_repo_map_entries_outside_the_root_or_malformed_are_refused(tmp_path, root, content):
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    path = tmp_path / "repos.json"
    path.write_text(content)
    with pytest.raises(ValueError):
        worker.load_repo_map(path, root)


# Processing


def test_mapped_repo_is_checked_and_completed(store, root, repos):
    row = receive(store)
    done = worker.process_one(store, root, repos, "w1")
    assert done.id == row.id and done.status == "completed" and done.worker == "w1" and done.error is None
    assert done.result["verdict"] == "refuted" and done.result["extraction_model"] is None
    assert [(e["claim"], e["status"]) for e in done.result["evidence"]] == [
        ("parse_url() exists", "confirmed"), ("parse_url_v2() exists", "refuted")]
    assert store.get(row.id) == done
    assert worker.process_one(store, root, repos, "w1") is None


def test_repo_names_match_case_insensitively(store, root, repos):
    receive(store, repo="OCTO/Widgets")
    assert worker.process_one(store, root, repos, "w1").status == "completed"


def test_unmapped_repo_is_skipped_without_running_checks(store, root, repos):
    row = receive(store, repo="someone/else")
    done = worker.process_one(store, root, repos, "w1", check=lambda *a, **k: pytest.fail("checked"))
    assert done.id == row.id and done.status == "skipped" and done.result is None
    assert "someone/else" in done.error


def test_check_errors_mark_the_row_failed_with_the_error(store, root, repos):
    def explode(*args, **kwargs):
        raise OSError("disk on fire")

    row = receive(store)
    done = worker.process_one(store, root, repos, "w1", check=explode)
    assert done.id == row.id and done.status == "failed" and done.result is None
    assert "disk on fire" in done.error


def test_request_without_text_is_failed(store, root, repos):
    store.create("github:issues:o/r#1", "received", {"repo": REPO})
    assert worker.process_one(store, root, repos, "w1").status == "failed"


def test_checkout_that_now_escapes_the_root_is_failed(store, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "moved").symlink_to(outside, target_is_directory=True)
    receive(store)
    done = worker.process_one(store, root, {REPO: root / "moved"}, "w1",
                              check=lambda *a, **k: pytest.fail("checked a repo outside the root"))
    assert done.status == "failed" and "repo root" in done.error


def test_check_is_static_only(store, root, repos):
    seen = {}

    def spy(repo, text, diff, **kwargs):
        seen.update(repo=repo, text=text, diff=diff, **kwargs)
        return worker.run_check(repo, text, diff, **kwargs)

    receive(store)
    worker.process_one(store, root, repos, "w1", check=spy)
    assert seen["repo"] == (root / "widgets").resolve() and seen["text"] == REPORT and seen["diff"] is None
    assert seen["repro"] is None and seen["use_model"] is False
    with pytest.raises(RuntimeError):
        seen["run_repro"]("repo", "cmd", "")


def test_event_text_is_never_executed(store, root, repos, tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"the worker tried to run a repro: {args}")

    monkeypatch.setattr(sandbox, "run_repro", refuse)
    monkeypatch.setattr(revisions, "verify_fix", refuse)
    monkeypatch.chdir(tmp_path)
    receive(store, text="Reproduce: `touch pwned` or $(touch pwned2); touch pwned3 in parse_url()")
    assert worker.process_one(store, root, repos, "w1").status == "completed"
    assert not list(tmp_path.rglob("pwned*"))


def test_run_once_drains_every_received_row(store, root, repos):
    for n in range(5):
        receive(store, number=n)
    receive(store, repo="someone/else", number=99)
    seen = []
    assert worker.run(store, root, repos, "w1", once=True, log=seen.append) == 6
    assert Counter(status for _, status, _ in statuses(store.path)) == {"completed": 5, "skipped": 1}
    assert len(seen) == 6 and all(json.loads(line)["worker"] == "w1" for line in seen)


def test_run_stops_when_asked(store, root, repos):
    stop = threading.Event()
    stop.set()
    receive(store)
    assert worker.run(store, root, repos, "w1", poll=0.01, stop=stop, log=lambda line: None) == 0


# Entry point


def write_map(tmp_path: Path, mapping: dict) -> Path:
    path = tmp_path / "repos.json"
    path.write_text(json.dumps(mapping))
    return path


def test_main_once_processes_the_queue(tmp_path, root, store, capsys):
    row = receive(store)
    args = ["--db", str(store.path), "--repo-root", str(root), "--repos", str(write_map(tmp_path, {REPO: "widgets"})),
            "--once", "--worker-id", "cli"]
    assert worker.main(args) == 0
    assert store.get(row.id).status == "completed"
    assert json.loads(capsys.readouterr().out) == {"id": row.id, "status": "completed", "worker": "cli"}


@pytest.mark.parametrize("mapping, root_name", [({"o/r": ".."}, "repos"), ({REPO: "widgets"}, "missing")])
def test_main_refuses_a_bad_map_or_root(tmp_path, root, db, capsys, mapping, root_name):
    args = ["--db", str(db), "--repo-root", str(tmp_path / root_name), "--repos", str(write_map(tmp_path, mapping)),
            "--once"]
    assert worker.main(args) == 2
    assert "error" in capsys.readouterr().err


# Integration: real processes over a real SQLite file


def start_worker(tmp_path: Path, db: Path, root: Path, name: str, *extra: str) -> subprocess.Popen:
    repos = write_map(tmp_path, {REPO: "widgets"})
    return subprocess.Popen(
        [sys.executable, "-m", "claimcheck.server.worker", "--db", str(db), "--repo-root", str(root),
         "--repos", str(repos), "--worker-id", name, *extra],
        cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def wait_until_no(db: Path, pending: tuple[str, ...], timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(status in pending for _, status, _ in statuses(db)):
            return
        time.sleep(0.1)
    pytest.fail(f"rows still pending: {Counter(s for _, s, _ in statuses(db))}")


def test_two_worker_processes_racing_process_every_row_exactly_once(tmp_path, root, db):
    store = Store(db)
    workers = [start_worker(tmp_path, db, root, name, "--poll", "0.02") for name in ("alpha", "beta")]
    try:
        for process in workers:  # both are polling before the rows arrive, so they race for every one
            assert "repo(s) mapped" in process.stderr.readline()
        ids = {receive(store, number=n, delivery=f"d-{n}").id for n in range(300)}
        skipped = {receive(store, repo="someone/else", number=n).id for n in range(300, 320)}
        wait_until_no(db, ("received", "processing"))
    finally:
        for process in workers:
            process.send_signal(signal.SIGTERM)
        outputs = [process.communicate(timeout=30) for process in workers]
    assert [process.returncode for process in workers] == [0, 0], outputs

    logged = [json.loads(line) for out, _ in outputs for line in out.splitlines()]
    per_id = Counter(entry["id"] for entry in logged)
    assert set(per_id) == ids | skipped and set(per_id.values()) == {1}, "a row was processed twice or not at all"
    by_worker = Counter(entry["worker"] for entry in logged)
    assert by_worker["alpha"] > 0 and by_worker["beta"] > 0, f"no real race happened: {by_worker}"

    final = {row_id: (status, name) for row_id, status, name in statuses(db)}
    assert all(final[i][0] == "completed" for i in ids) and all(final[i][0] == "skipped" for i in skipped)
    assert all(final[entry["id"]][1] == entry["worker"] for entry in logged)
    assert all(store.get(i).result["verdict"] == "refuted" for i in list(ids)[:10])


def test_a_crashed_workers_stale_row_is_reclaimed_by_another_process(tmp_path, root, db):
    store = Store(db)
    fresh = receive(store, number=2)
    assert store.claim("busy", stale_after=60).id == fresh.id
    orphan = receive(store)
    assert store.claim("crashed", stale_after=60, now=time.time() - 120).id == orphan.id
    process = start_worker(tmp_path, db, root, "rescuer", "--once", "--stale-after", "60")
    out, err = process.communicate(timeout=60)
    assert process.returncode == 0, err
    assert (store.get(orphan.id).status, store.get(orphan.id).worker) == ("completed", "rescuer")
    assert (store.get(fresh.id).status, store.get(fresh.id).worker) == ("processing", "busy")
    assert [json.loads(line)["id"] for line in out.splitlines()] == [orphan.id]


def test_sigterm_lets_the_worker_exit_cleanly(tmp_path, root, db):
    Store(db)
    process = start_worker(tmp_path, db, root, "idle", "--poll", "0.05")
    assert "worker idle:" in process.stderr.readline()  # signal handlers are installed by now
    os.kill(process.pid, signal.SIGTERM)
    assert process.wait(timeout=10) == 0
