"""Work out which sandbox a command means.

An argument is an id or a name, or a unique start of either. Without one, the
sandboxes that hold the current repo match or, in a plain folder, those rooted here or
in a parent. A sandbox below the current folder is never picked.
"""
import os
import sys
from pathlib import Path

from cave import git, sandbox
from cave.errors import CaveError
from cave.log import warn
from cave.paths import is_id


def _real(path):
    return Path(os.path.realpath(path))


def _name(sid, meta):
    return meta.get("name", sid)


def _listing(sids, boxes):
    lines = []
    for number, sid in enumerate(sids, 1):
        meta = boxes[sid]
        repos = ", ".join(entry["name"] for entry in meta.get("repos", []))
        lines.append(f"  {number}) {_name(sid, meta)}  {meta.get('root', '?')}\n    repos: {repos}")
    return "\n".join(lines)


def _named(arg, boxes):
    if is_id(arg):  # as given, since discard can clear what a deleted sandbox left behind
        return arg
    hits = [sid for sid, meta in boxes.items() if meta.get("name") == arg]  # a name may start another
    if not hits and arg:
        hits = [sid for sid, meta in boxes.items() if sid.startswith(arg) or meta.get("name", "").startswith(arg)]
    if not hits:
        raise CaveError(f"not a sandbox id or name: {arg}")
    if len(hits) > 1:
        raise CaveError(f"{arg} matches several sandboxes:\n{_listing(hits, boxes)}")
    return hits[0]


def _covering(boxes, cwd, repo):
    """The ids that cover this place, nearest first."""
    def covers(meta):
        if repo:
            return any(_real(entry["repo"]) == repo for entry in meta["repos"])
        return _real(meta["root"]) == cwd or _real(meta["root"]) in cwd.parents

    hits = [sid for sid, meta in boxes.items() if meta and covers(meta)]
    return sorted(hits, key=lambda sid: -len(_real(boxes[sid]["root"]).parts))


def _pick(sids, boxes):
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise CaveError(f"several sandboxes match here, so name one:\n{_listing(sids, boxes)}")
    warn(f"several sandboxes match here, nearest first:\n{_listing(sids, boxes)}")
    print("which one? ", end="", file=sys.stderr, flush=True)
    choice = sys.stdin.readline().strip()
    if not (choice.isdecimal() and 1 <= int(choice) <= len(sids)):
        raise CaveError("no sandbox chosen")
    return sids[int(choice) - 1]


def resolve(arg=None, always_show=False):
    """The sandbox id meant by arg or by the current folder. Prints it unless that's obvious."""
    boxes = {sid: sandbox.load_meta(sid) for sid in reversed(sandbox.all_ids())}  # newest first
    if arg is not None:
        sid, show = _named(arg, boxes), False
    else:
        cwd = _real(os.getcwd())
        repo = git.toplevel()
        sids = _covering(boxes, cwd, repo and _real(repo))
        if not sids:
            raise CaveError(f"no sandbox covers {cwd}. Name one, or see cave ls")
        sid = sids[0] if len(sids) == 1 else _pick(sids, boxes)
        show = _real(boxes[sid]["root"]) != cwd
    if boxes.get(sid) and (show or always_show):
        warn(f"using sandbox {_name(sid, boxes[sid])}, root {boxes[sid].get('root', '?')}")
    return sid
