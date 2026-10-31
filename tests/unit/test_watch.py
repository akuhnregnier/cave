"""Sweeps must never cost anything but a late update."""
import threading
import time

import pytest

from cave import container, git, launch, sandbox, transport, watch
from cave._testing.repos import SID, raises, repo_entry


@pytest.fixture
def updates(active, box, monkeypatch):
    active.add(SID)
    seen = []
    monkeypatch.setattr(transport, "update", lambda sid, clone: seen.append((sid, clone.key)))
    return seen


def test_a_sweep_never_queues_behind_a_merge(updates, repo):
    with git.lock(repo):
        watch.sweep()
    assert updates == []
    watch.sweep()
    assert updates == [(SID, "code")]


def test_one_bad_repo_is_reported_and_the_rest_still_update(updates, repo, capsys):
    meta = sandbox.load_meta(SID)
    meta["repos"].insert(0, repo_entry("gone", "/nowhere", "gone"))
    sandbox.write_meta(SID, meta)
    watch.sweep()
    assert updates == [(SID, "code")]
    assert "could not update gone" in capsys.readouterr().err


def test_a_sweep_only_mentions_requests(updates, box, capsys, monkeypatch):
    (box / "outbox/request-0123456789abcdef").write_text("add-repo secret\n")
    monkeypatch.setattr(launch, "add_repo", lambda *args: pytest.fail("acted on a request"))
    watch.sweep()
    err = capsys.readouterr().err
    assert f"cave requests {SID}" in err and "secret" not in err
    assert (box / "outbox/request-0123456789abcdef").exists()


def test_a_sweep_survives_container_failing(monkeypatch, capsys):
    monkeypatch.setattr(container, "running", raises(OSError("no container")))
    watch.sweep()
    assert "no container" in capsys.readouterr().err


def test_polling_survives_a_failure_and_is_silent(monkeypatch, capsys):
    swept = threading.Event()
    calls = []

    def fail(timeout=None):
        calls.append(1)
        if len(calls) > 1:
            swept.set()
        raise OSError("no container")

    monkeypatch.setattr(watch, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(container, "running", fail)
    with watch.polling():
        assert swept.wait(5)
    assert capsys.readouterr().err == ""


def test_a_hung_sweep_cannot_delay_the_end_of_a_session(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    def hang(report):
        entered.set()
        release.wait(30)

    monkeypatch.setattr(watch, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(watch, "sweep", lambda report: hang(report))
    try:
        with watch.polling():
            assert entered.wait(5)
            ending = time.monotonic()
        assert time.monotonic() - ending < 5  # well short of the sweep's 30
        assert all(thread.daemon for thread in threading.enumerate() if thread is not threading.main_thread())
    finally:
        release.set()
