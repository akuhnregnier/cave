"""Bring running sandboxes up to date with the host's base branches.

Main can move at any time, with or without cave, and only something running can notice:
each cave command sweeps as it ends, and a foreground session on a timer. It only ever
moves files into inboxes. It's a convenience, so it never waits for a merge, which will
sweep when it ends, and all that can go wrong with it is that an update comes late. It
also says when a sandbox has requests waiting, but only `cave requests` can act on them.
"""
import shutil
import threading
from contextlib import contextmanager

from cave import container, git, sandbox, transport
from cave.log import warn

POLL_SECONDS = 30


def _update(sid, clone):
    with git.lock(clone.repo, wait=False) as held:
        if held:
            transport.update(sid, clone)


def sweep(report=warn):
    """Update every running sandbox's repos, and mention its requests. Never raises."""
    if shutil.which("container") is None:
        return  # nothing can be running, and only some commands need it
    try:
        running = container.running(container.CLEANUP_SECONDS)
        live = [sid for sid in sandbox.all_ids() if sid in running]
        targets = [(sid, clone) for sid in live for clone in sandbox.clones(sandbox.load_meta(sid))]
    except Exception as error:
        report(f"could not look for sandboxes to update: {error}")
        return
    for sid, clone in targets:
        try:
            _update(sid, clone)
        except Exception as error:
            report(f"could not update {clone.name} in sandbox {sid}: {error}")
    for sid in live:
        if sandbox.requests(sid):
            report(f"sandbox {sid} has requests waiting: cave requests {sid}")


@contextmanager
def polling():
    """Sweep in the background. Silent, as Claude has the terminal, and a daemon, so it can't delay exit."""
    stop = threading.Event()

    def poll():
        while not stop.wait(POLL_SECONDS):
            sweep(report=lambda message: None)

    threading.Thread(target=poll, daemon=True).start()
    try:
        yield
    finally:
        stop.set()
