"""Git data crossing the VM boundary, as bundles.

Everything leaving the VM is untrusted: a bundle is copied out of the outbox first, so
what is checked is what gets fetched. What goes in is prepared in staging, which the VM
can't see, and only then moved into the inbox, which it can read but not write.

A repo's bundle in the inbox starts as all of its base branch. As that moves, the bundle is
replaced by one holding only what the VM lacks, going by the commit the VM says it has. A
lie there can only cost the VM its own updates, as the bundle never holds more than base.
"""
import os
import re

from cave import git, sandbox
from cave.errors import CaveError
from cave.paths import box_path

CHUNK = 1 << 20
COMMIT = r"[0-9a-f]{40}|[0-9a-f]{64}"


def _max_bundle():
    text = os.environ.get("CAVE_MAX_BUNDLE") or str(1 << 30)
    if not text.isdecimal():
        raise CaveError(f"CAVE_MAX_BUNDLE must be a number of bytes, not {text}")
    return int(text)


def ref(sid, key):
    """Where a repo's bundle lands. Nothing outside refs/cave moves until landing."""
    return f"refs/cave/{sid}/{sandbox.report_key(key)}"


def inbox_name(clone):
    return f"{sandbox.report_key(clone)}.bundle"


def deliver(sid, name, staged, update=False):
    """Move a finished file from staging into the inbox, so the VM only sees it whole.

    A new name refuses anything already there, even a dangling symlink. Only update
    replaces a file, as the inbox is the one place the VM can't plant anything.
    """
    target = box_path(sid) / "inbox" / name
    try:
        if update:
            os.replace(staged, target)
        else:
            os.link(staged, target)
    except FileExistsError:
        raise CaveError(f"{target} already exists")
    finally:
        staged.unlink(missing_ok=True)


def send_repo(sid, clone, repo, base, update=False, since=None):
    """Put a bundle of the repo's base branch in the inbox, for the VM to clone or, on update, fetch."""
    name = inbox_name(clone)
    staged = box_path(sid) / "staging" / f"in-{name}"  # git outlives a poller killed at exit, so not fetch's name
    git.create_bundle(repo, staged, base, since)
    deliver(sid, name, staged, update)


def _acked(sid, clone, tip):
    """What an update can build on: the commit the VM reports having, if it's behind tip on this branch."""
    ack = sandbox.read_report(sandbox.repo_report(sid, clone.key, "acked")).strip()
    return ack if re.fullmatch(COMMIT, ack) and ack != tip and git.is_ancestor(clone.repo, ack, tip) else None


def update(sid, clone):
    """Bring a running sandbox's bundle up to base, unless it's there. Call with the repo's lock held."""
    tip = git.strict(clone.repo, "rev-parse", "--verify", f"refs/heads/{clone.base}^{{commit}}")
    need_full = sandbox.repo_report(sid, clone.key, "need-full")
    full = sandbox.has_report(need_full)
    if not full and git.bundle_tip(clone.repo, box_path(sid) / "inbox" / inbox_name(clone.key)) == tip:
        return
    send_repo(sid, clone.key, clone.repo, clone.base, update=True, since=None if full else _acked(sid, clone, tip))
    if full:
        need_full.unlink(missing_ok=True)  # never follows a link


def _copy_out(source, staged):
    """Copy a bundle to host-private staging, stopping at the cap."""
    limit = _max_bundle()
    try:
        with sandbox.open_report(source) as vm_file, open(staged, "wb") as copy:
            copied = 0
            for chunk in iter(lambda: vm_file.read(CHUNK), b""):
                copied += len(chunk)
                if copied > limit:
                    raise CaveError(f"its bundle is over the {limit} byte cap (CAVE_MAX_BUNDLE)")
                copy.write(chunk)
    except OSError as error:
        raise CaveError(f"could not read its bundle: {error.strerror}")


def fetch(clone, sid):
    """Import a repo's bundle into refs/cave. Returns the tip, or None if it reports nothing new.

    Anything else raises: no bundle must never read as no changes.
    """
    bundle = sandbox.repo_report(sid, clone.key, "bundle")
    if not os.path.lexists(bundle):
        if sandbox.has_report(sandbox.repo_report(sid, clone.key, "none")):
            return None
        raise CaveError("the sandbox left no bundle for it. Everything is kept")
    staging = box_path(sid) / "staging"
    staging.mkdir(mode=0o700, exist_ok=True)
    staged = staging / bundle.name
    branch = f"refs/heads/claude/{sid}"
    try:
        _copy_out(bundle, staged)
        heads = git.bundle_heads(clone.repo, staged)
        if not re.fullmatch(rf"({COMMIT}) {re.escape(branch)}", heads):
            raise CaveError(f"its bundle must hold {branch} and nothing else")
        target = ref(sid, clone.key)
        if not git.fetch_bundle(clone.repo, staged, branch, target):
            raise CaveError("git refused its bundle, which may be damaged or hold malformed objects")
        tip = git.query(clone.repo, "rev-parse", "--verify", "-q", f"{target}^{{commit}}")
        if not tip:  # git would count no commits in anything else, which must not read as no changes
            raise CaveError("its bundle holds something other than a commit")
        return tip
    finally:
        staged.unlink(missing_ok=True)
