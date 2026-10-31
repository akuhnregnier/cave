"""Letting more into a running sandbox: a repo, or a review the VM asked for.

A request is a file a VM wrote, so it can say anything and do nothing. Only a typed yes
here acts on one, and a repo it names must lie under the sandbox's root. What the user
typed themselves, with `cave add`, can come from anywhere.
"""
import os
import subprocess

from cave import collect, container, launch, review, sandbox
from cave.errors import CaveError
from cave.log import escape, say, warn


def _running(sid):
    meta = sandbox.require_meta(sid)
    if sid not in container.running():
        raise CaveError(f"sandbox {sid} is not running")
    return meta


def _shown(text):
    return escape(text, one_line=True)[0]


def _add(meta, entry):
    launch.add_repo(meta, entry)
    say(f"added {_shown(entry['name'])} to {meta['id']}, at {_shown(entry['vm'])}")


def add(sid, path):
    """Typing the path is the approval."""
    meta = _running(sid)
    _add(meta, launch.added_entry(meta, os.path.realpath(path)))


def _requested(meta, path):
    """The entry for a repo a VM named, which might be any path at all."""
    root = os.path.realpath(meta["root"])
    repo = os.path.realpath(os.path.join(root, path))
    if repo != root and not repo.startswith(root + os.sep):
        raise CaveError(f"{_shown(repo)} is outside the sandbox's root, so only cave add, typed by you, can add it")
    return launch.added_entry(meta, repo)


def _answer(sid, request):
    meta = sandbox.require_meta(sid)  # as it stands now: the last answer may have changed it
    name = meta["name"]
    if request.repo is None:
        if review.ask(f"sandbox {name} asks for a review. Run cave merge, which shows each repo before landing it? "
                      f"(For line-by-line, answer no and run cave merge -p {sid})"):
            collect.merge(sid)
        return
    entry = _requested(meta, request.repo)
    if review.ask(f"sandbox {name} asks for {_shown(entry['repo'])}. Give its VM a copy of {entry['base']}?"):
        _add(meta, entry)
    else:
        say("not added")


def walk(sid):
    """Put each request to the user. Each is settled by its one answer, so it's then gone."""
    _running(sid)
    waiting = sandbox.requests(sid)
    if not waiting:
        say("no requests")
    elif not review.on_terminal():
        for request in waiting:
            say(f"{sid} asks for {_shown(request.repo) if request.repo else 'a review'}")
        raise CaveError("answering takes a terminal, so nothing was done")
    for request in waiting:
        try:
            _answer(sid, request)
        except (CaveError, OSError, subprocess.CalledProcessError) as error:
            warn(f"request refused: {error}")
        finally:
            request.file.unlink(missing_ok=True)  # never follows a link
