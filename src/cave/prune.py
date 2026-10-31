"""Delete sandboxes older than MAX_AGE, what they leave behind, and old sessions.

This is the one place a sandbox goes with its work unlanded, so every command says so
beforehand, from WARN_AHEAD before that, and the deletion itself says what went.

What's left of a sandbox that has gone from disk (a stopped container, its volume, its
refs) is an orphan. Only a box known to be gone, and an id over MAX_AGE, can make one: a
failed listing leaves everything, and the volume may be the last copy of its work.
"""
import errno
import math
import os
import re
import stat
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

from cave import container, discover, git, sandbox
from cave.errors import CaveError
from cave import log
from cave.log import escape, warn
from cave.paths import MAX_AGE, box_path, config_repo, expiry, is_id, is_old, sandboxes_dir, sessions_dir, state_dir

SESSION_RE = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}(\.jsonl)?")
STAGING_AGE = timedelta(days=1)  # nothing takes that long, so a file this old lost its process
WARN_AHEAD = timedelta(days=7)  # from day 23 of the 30
GRACE = timedelta(days=1)  # from a sandbox's first warning to its deletion, whatever its age
MAX_DEPTH = 64  # of folders in a session, as each level of a walk holds a descriptor
MAX_NAMED = 5  # of the files outside any repo, as those can run to thousands
FAILED = (OSError, CaveError, subprocess.SubprocessError)


def _open_dir(name, parent=None):
    """A directory as a file descriptor, never through a symlink: the VM writes to this tree."""
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _subdirs(parent):
    """Names of the real directories in an open directory."""
    return [name for name in os.listdir(parent)
            if stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode)]


def _newest_mtime(name, parent, depth=0):
    """The newest mtime of an entry and anything under it, going by descriptors so no link is followed.

    Anything it can't see to the end counts as brand new, so the session stays: a tree deeper than
    MAX_DEPTH (each level holds a descriptor), or a folder that won't open. Only a vanished entry is skipped.
    """
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    newest = info.st_mtime
    if stat.S_ISDIR(info.st_mode):
        if depth >= MAX_DEPTH:
            return math.inf
        try:
            fd = _open_dir(name, parent)
        except FileNotFoundError:
            return newest
        except OSError:
            return math.inf
        try:
            for child in os.listdir(fd):
                try:
                    newest = max(newest, _newest_mtime(child, fd, depth + 1))
                except FileNotFoundError:
                    continue
                except OSError:
                    return math.inf
        finally:
            os.close(fd)
    return newest


def _remove(name, parent, depth=0):
    """rm -rf by descriptors: a name swapped for a link is unlinked, never entered. The VM can leave
    read-only directories. Deeper than MAX_DEPTH raises, leaving the rest."""
    if stat.S_ISDIR(os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode):
        if depth >= MAX_DEPTH:
            raise OSError(errno.ENAMETOOLONG, "too deep to remove")
        os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
        fd = _open_dir(name, parent)
        try:
            for child in os.listdir(fd):
                _remove(child, fd, depth + 1)
        finally:
            os.close(fd)
        os.rmdir(name, dir_fd=parent)
    else:
        os.unlink(name, dir_fd=parent)


def _prune_sessions(now):
    # UUIDs hold no date, so this goes by mtime. Anything else in there (auto
    # memory) is not a session. Every step is relative to an open directory, so a
    # component swapped for a symlink mid-way fails the open instead of leading outside.
    cutoff = (now - MAX_AGE).timestamp()
    try:
        root = _open_dir(sessions_dir().resolve())
    except OSError:
        return
    try:
        for project in _subdirs(root):
            _prune_project(project, root, cutoff)
    except OSError as error:
        warn(f"could not prune sessions: {error}")
    finally:
        os.close(root)


def _prune_project(name, root, cutoff):
    try:
        project = _open_dir(name, root)
    except OSError:
        return
    try:
        for key in _subdirs(project):
            try:
                directory = _open_dir(key, project)
            except OSError:
                continue
            try:
                for entry in os.listdir(directory):
                    try:
                        if SESSION_RE.fullmatch(entry) and _newest_mtime(entry, directory) < cutoff:
                            _remove(entry, directory)
                    except (OSError, RecursionError) as error:  # a folder too deep to walk by descriptors stays
                        warn(f"could not prune {name}/{key}/{entry}: {error}")
            finally:
                os.close(directory)
    except OSError:
        pass
    finally:
        os.close(project)


def _ref_repos():
    # Refs can outlive their sandbox dir, for example if the state dir was cleared.
    root = git.toplevel() or str(Path.cwd())
    repos = [str(Path(root) / rel) for rel in discover.find_repos(root)]
    if (config_repo() / ".git").exists():
        repos.append(str(config_repo()))
    return repos


def _gone(sid):
    """Is this sandbox known to be off disk? With no state dir at all, nothing is known."""
    if not sandboxes_dir().is_dir():
        return False
    try:
        os.lstat(box_path(sid))
    except FileNotFoundError:
        return True
    return False  # and anything but "no such file" raises, so says nothing


def _orphan(sid, now, active):
    return is_id(sid) and is_old(sid, now) and sid not in active and _gone(sid)


def _unlanded_work(sid, meta):
    """What going would cost this sandbox, by name. Every repo is named, and a VM's text can't forge a line."""
    repos = [clone.name for clone in sandbox.clones(meta) if sandbox.holds_work(sid, clone.key)]
    others = sandbox.unlanded(sid, meta)
    named = repos + others[:MAX_NAMED] + ([f"{len(others) - MAX_NAMED} more"] if len(others) > MAX_NAMED else [])
    return ", ".join(escape(name, one_line=True)[0] for name in named)


def _first_warned(sid, meta, now):
    """When this sandbox was first warned of (now, if it never was), kept in its meta."""
    if not meta.get("warned"):  # cleared (None) when the sandbox starts again
        sandbox.update_meta(sid, warned=now.timestamp())
    return datetime.fromtimestamp(meta.get("warned") or now.timestamp())


def _note_late(message):
    """A prune thread past its time is muted, so its word on a deletion waits for the next command."""
    if log.muted():
        try:
            with open(state_dir() / "pruned", "a") as handle:
                handle.write(message.replace("\n", " ") + "\n")
        except OSError:
            pass


def _report_late():
    path = state_dir() / "pruned"
    try:
        lines = path.read_text(errors="replace").splitlines()
        path.unlink()
    except OSError:
        return
    for line in lines:
        warn(line)


def warn_expiring():
    """Say which stopped sandboxes prune will soon delete along with their unlanded work. Never raises."""
    _report_late()
    try:
        now = datetime.now()
        expiring = [sid for sid in sandbox.all_ids() if is_old(sid, now + WARN_AHEAD)]
        if not expiring:
            return
        try:
            active = container.running(container.CLEANUP_SECONDS)
        except FAILED:
            active = set()  # better to warn of one that turns out to be running
        for sid in expiring:
            meta = sandbox.load_meta(sid)
            work = _unlanded_work(sid, meta)
            if work and sid not in active:
                when = max(expiry(sid), _first_warned(sid, meta, now) + GRACE)
                warn(f"WARNING: sandbox {meta.get('name', sid)} ({sid}) is deleted WITH ITS UNLANDED WORK by the first "
                     f"launch or cave prune after {when:%Y-%m-%d %H:%M}: {work}. "
                     f"Run cave merge {sid} to land it, or cave discard {sid} if you don't want it"
                     + (f". merge can't land files outside a repo, but this copies the volume out (treat it as untrusted): "
                        f"{sandbox.copy_out_command(sid, meta)}" if sandbox.unlanded(sid, meta) else ""))
    except Exception as error:
        warn(f"could not check for sandboxes about to expire: {error}")


def _prune_boxes(now, active):
    for sid in sandbox.all_ids():
        if is_old(sid, now) and sid not in active:
            try:
                meta = sandbox.load_meta(sid)
                work = _unlanded_work(sid, meta)
                if work and meta and now < _first_warned(sid, meta, now) + GRACE:
                    continue  # the warning is what lets it be landed first
                message = f"pruning {sid}, over {MAX_AGE.days} days old" + (f", and its unlanded work with it: {work}" if work else "")
                warn(message)
                sandbox.delete(sid)
                _note_late(message)
            except FAILED as error:
                warn(f"could not prune {sid}: {error}")


def _tidy_boxes(now, active):
    """In the sandboxes that stay: what a killed transfer left, and bundles already landed."""
    cutoff = (now - STAGING_AGE).timestamp()
    for sid in sandbox.all_ids():
        try:
            with os.scandir(box_path(sid) / "staging") as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
            if sid not in active:  # no bundle is read again once its repo is merged
                for entry in sandbox.load_meta(sid).get("repos", []):
                    if entry.get("merged"):
                        sandbox.repo_report(sid, entry["clone"], "bundle").unlink(missing_ok=True)  # never follows a link
        except FAILED as error:
            warn(f"could not tidy {sid}: {error}")


def _prune_orphans(now, active):
    """A stopped container holds its volume, so it goes first. Only a container with a volume can be left."""
    for name in container.own_volumes():
        sid = name.removeprefix("cave-")
        if sid != name and _orphan(sid, now, active):
            warn(f"pruning volume {name}")
            container.force_delete(sid)
            if not container.delete_volume(name):
                warn(f"could not delete volume {name}")


def sweep_networks(active, deadline=None):
    """Delete the networks of sandboxes that are off disk, which a failed delete or a crash leaves behind.

    Only a network with this state dir's label and a sandbox's name qualifies. One that a VM still uses
    can't be deleted by `container`, and one whose box exists is in use or about to be. Past the
    deadline (a time.time() value) it stops, and the next sweep takes what remains.
    """
    for name in container.own_networks():
        if deadline is not None and time.time() > deadline:
            return
        sid = name.removeprefix("cave-")
        if sid != name and is_id(sid) and sid not in active and _gone(sid):
            if not container.delete_network(name):
                warn(f"could not delete network {name}")


def _prune_networks(now, active):
    sweep_networks(active)


def _prune_refs(now, active):
    for repo in _ref_repos():
        for sid in git.sandbox_ref_ids(repo):
            if _orphan(sid, now, active):
                git.delete_sandbox_refs(repo, sid)


def prune():
    now = datetime.now()
    try:
        active = container.running(container.CLEANUP_SECONDS, strict=True)
    except FAILED as error:
        warn(f"pruning only sessions, as what is running is unknown: {error}")
    else:
        for step in (_prune_boxes, _tidy_boxes, _prune_orphans, _prune_networks, _prune_refs):
            try:
                step(now, active)
            except FAILED as error:
                warn(f"prune skipped a step: {error}")
    _prune_sessions(now)
