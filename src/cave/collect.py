"""Bring a sandbox's work home: diff, merge, resolve, discard.

Landing a repo runs under a per-repo lock:
  fetch -> work out what's new -> check it's safe -> rebase in a scratch clone -> review -> move the base branch

A repo that can't land is held back with the reason, and everything is kept. A
failed git query never counts as "no changes".
"""
from pathlib import Path

from cave import container, git, launch, review, sandbox, transport
from cave.errors import CaveError
from cave.log import say, warn
from cave.paths import box_path, volume_name


LAND_ATTEMPTS = 3
BRIEFING = ("cave resolve: these repos could not land on the host: {repos}. In each one, finish or abort any merge or "
            "rebase under way, get back on {branch} with all your work on it, run git fetch origin, then rebase it onto "
            "that main, resolving any conflicts, and leave the result committed, with no merge commits. Then exit: the "
            "user reviews it with cave merge.")


def _base_ref(clone):
    return f"refs/heads/{clone.base}"


def _request_bundles(sid):
    if not container.run_companion(sid, "bundle"):
        raise CaveError(f"sandbox {sid} did not answer, so nothing was fetched. Everything is kept")


def _recover(sid, meta):
    """Have a VM take the final snapshot of any repo in a stopped sandbox that hasn't one."""
    if all(sandbox.settled(sid, clone.key) for clone in sandbox.clones(meta)):
        return
    say(f"sandbox {sid} stopped before its final snapshot, so a recovery VM is taking it")
    failure = launch.recover(meta)
    if failure:
        warn(f"the recovery VM reported a failure, so repos it could not snapshot are held back: {failure}")


def _needs_vm(clone, sid, problem):
    """A repo only the VM can fix, flagged so that cave resolve can reach it even in a stopped sandbox."""
    sandbox.update_repo(sid, clone.key, conflict=True)
    return CaveError(f"{problem}. Everything is kept: cave resolve {sid} has Claude fix it, or cave discard {sid} drops it")


def _fetch(clone, sid, running):
    """Import the repo's bundle into refs/cave. Returns the tip, or None if the VM reports nothing new."""
    if not git.query(clone.repo, "rev-parse", "--verify", "-q", f"{_base_ref(clone)}^{{commit}}"):
        raise CaveError(f"base branch {clone.base} no longer exists in {clone.repo}")
    if sandbox.mid_operation(sid, clone.key):
        raise CaveError("it was skipped because a merge or rebase is under way in the VM, or it was left off its "
                        f"branch, so what is current is unclear. Everything is kept: cave resolve {sid} finishes it")
    if not (running or sandbox.finished(sid, clone.key)):
        raise _needs_vm(clone, sid, "no final snapshot of this repo could be taken, so it may be broken")
    return transport.fetch(clone, sid)


def _current(clone, sid):
    """Its entry as it stands now the lock is held: a merge we waited for may have changed it."""
    for entry in sandbox.clones(sandbox.load_meta(sid)):
        if entry.key == clone.key:
            return entry
    raise CaveError("another cave command has dealt with it")


def _start(clone, sid, tip):
    """The commit after which the bundle has commits the host doesn't.

    After a merge, the host holds rebased copies of what it took, so only later commits
    count. A bundle that contains base was rebased onto it, so it stands alone. One with
    those copies was rebased onto a main that has them, which base may have moved past
    since, so it has what lies beyond the two's merge base.
    """
    if not clone.landed_tip or (tip and git.is_ancestor(clone.repo, _base_ref(clone), tip)):
        return _base_ref(clone)
    if tip and git.is_ancestor(clone.repo, clone.landed, tip):
        return git.strict(clone.repo, "merge-base", _base_ref(clone), tip)
    if tip and git.is_ancestor(clone.repo, clone.landed_tip, tip):
        return clone.landed_tip
    raise _needs_vm(clone, sid, f"claude/{sid} was rewritten after the last merge: it no longer contains "
                                f"{clone.landed_tip[:12]}, which landed as {(clone.landed or '')[:12]}. Nothing was "
                                f"merged. It needs a rebase onto its origin's {clone.base}, which has the landed copy")


def _pending(clone, sid, start):
    return int(git.strict(clone.repo, "rev-list", "--count", f"{start}..{transport.ref(sid, clone.key)}"))


def _changed_paths(clone, sid, start):
    output = git.strict(clone.repo, "log", "--format=", "--name-only", "--no-renames", "-z",
                        f"{start}..{transport.ref(sid, clone.key)}")
    return {name.strip("\n") for name in output.split("\0") if name.strip("\n")}


def _parents(path):
    parts = path.split("/")
    return ["/".join(parts[:end]) for end in range(1, len(parts))]


def _in_the_way(changed, untracked):
    """The changed paths that need an untracked or ignored host file gone: the same path,
    a file where it wants a folder, or a folder of them where it wants a file."""
    paths = {path.casefold(): path for path in changed}
    folders = {parent: path for folded, path in paths.items() for parent in _parents(folded)}
    hits = set()
    for name in (path.rstrip("/").casefold() for path in untracked):  # a nested repo is listed as `dir/`
        hits.update(found[key] for found, key in [(paths, name), (folders, name), *((paths, up) for up in _parents(name))]
                    if key in found)
    return sorted(hits)


def _check_landable(clone, sid, start):
    worktrees = {clone.repo, Path(git.worktree_of(clone.repo, clone.base) or clone.repo)}
    changed = _changed_paths(clone, sid, start)
    for path in worktrees:
        if git.in_progress(path):
            raise CaveError(f"{path} has a rebase, merge or cherry-pick in progress")
        if git.strict(path, "status", "--porcelain", "--untracked-files=no"):
            raise CaveError(f"{path} has uncommitted changes")
        # Fast-forwarding checks the commits out, which would replace these without a word.
        hits = _in_the_way(changed, git.strict(path, "ls-files", "-o", "-z").split("\0"))
        if hits:
            raise CaveError(f"it would overwrite untracked or ignored files in {path}: {', '.join(hits[:5])}")
    if git.strict(clone.repo, "rev-list", "--merges", "--count", f"{start}..{transport.ref(sid, clone.key)}") != "0":
        raise _needs_vm(clone, sid, "its history has a merge commit, which rebasing would drop")


def _fast_forward(clone, onto, rebased):
    """Move base to the rebased commits. False if it has moved since the rebase."""
    if git.strict(clone.repo, "rev-parse", _base_ref(clone)) != onto:
        return False
    worktree = git.worktree_of(clone.repo, clone.base)
    if not worktree:
        return git.compare_and_swap(clone.repo, _base_ref(clone), rebased, onto)
    git.strict(worktree, "merge", "-q", "--ff-only", rebased)  # refuses by itself if base moves from here on
    return True


def _check_size(clone, sid, start, tip):
    """Refuse commits too big to list or rebase: reading them would cost more than the host has."""
    written = git.written_bytes(clone.repo, start, tip)
    if written > git.MAX_WRITTEN:
        raise _needs_vm(clone, sid, f"rebasing it would write {written >> 20} MiB, over the {git.MAX_WRITTEN >> 20} MiB limit")


def _land(clone, sid, start, tip, yes, patch):
    """Rebase the new commits onto base, review the result, then move base. Returns the new base.

    Approval covers exactly that result, so if base moves first it goes through the gate again.
    """
    dest = f"{transport.ref(sid, clone.key)}-rebased"
    for _ in range(LAND_ATTEMPTS):
        _check_landable(clone, sid, start)
        _check_size(clone, sid, start, tip)
        onto, rebased = git.rebase_in_scratch(clone.repo, clone.base, start, tip, dest)
        clashes = git.fold_collisions(clone.repo, onto, rebased)
        if clashes:
            raise _needs_vm(clone, sid, f"paths differing only by case or Unicode form would overwrite each other "
                                        f"on the host's filesystem: {', '.join(clashes[:6])}")
        review.gate(sandbox.load_meta(sid), clone, onto, rebased, yes, patch)
        _check_landable(clone, sid, start)  # the prompt can take a while
        if _fast_forward(clone, onto, rebased):
            return rebased
    raise CaveError(f"{clone.base} kept moving during the rebase. Everything is kept")


def _land_repo(clone, sid, running, yes=False, patch=False):
    """Land what's new in one repo. True if anything landed."""
    with git.lock(clone.repo):
        clone = _current(clone, sid)
        tip = _fetch(clone, sid, running)
        start = _start(clone, sid, tip)
        if not tip or not _pending(clone, sid, start):
            sandbox.update_repo(sid, clone.key, conflict=False)
            return False
        try:
            landed = _land(clone, sid, start, tip, yes, patch)
        except review.Rejected:
            git.delete_sandbox_refs(clone.repo, sid)
            raise
        sandbox.update_repo(sid, clone.key, landed_tip=tip, landed=landed, conflict=False)
        return True


def _gone_check(sid):
    if not sandbox.load_meta(sid):  # a merge that won the race landed all and deleted it
        raise CaveError(f"sandbox {sid} is gone: another cave command landed it and deleted it") from None


def _land_all(sid, targets, running, yes, patch):
    """Land every repo independently. Returns (the repos that are done, how many landed)."""
    done, landed, conflicts = [], 0, []
    for clone in targets:
        try:
            if _land_repo(clone, sid, running, yes, patch):
                landed += 1
                say(f"merged {clone.name} into {clone.base} in {clone.repo}")
            done.append(clone)
        except git.RebaseFailed as failure:
            conflicts.append((clone, failure.output))
            try:
                sandbox.update_repo(sid, clone.key, conflict=True)
            except (CaveError, OSError, KeyError, StopIteration):  # no meta to update: the sandbox is gone
                _gone_check(sid)
                raise
        except (CaveError, OSError) as error:
            _gone_check(sid)
            warn(f"held back {clone.name}: {error}")
    if conflicts:
        lines = [f"rebase conflict in {len(conflicts)} {'repo' if len(conflicts) == 1 else 'repos'}, left as they "
                 f"were. The commits are kept. Run cave resolve {sid}, then cave merge again:"]
        for clone, output in conflicts:
            lines.append(f"  {clone.name} ({clone.repo})")
            lines += [f"    {line}" for line in output.splitlines() if "CONFLICT" in line]
        warn("\n".join(lines))
    return done, landed


def _refresh(sid, meta):
    """Bring the outbox up to date. A running sandbox gives up committed work, a stopped one is recovered."""
    running = sid in container.running()
    if running:
        _request_bundles(sid)
    else:
        _recover(sid, meta)
    return running


def diff(sid, patch=False):
    """Show what each repo would land. False if any couldn't be shown."""
    meta = sandbox.require_meta(sid)
    targets = sandbox.clones(meta)
    running = _refresh(sid, meta)
    if running:
        warn("sandbox is still running; uncommitted work is not shown")
    shown = True
    for clone in targets:
        try:
            with git.lock(clone.repo):
                clone = _current(clone, sid)
                tip = _fetch(clone, sid, running)
                start = _start(clone, sid, tip)
                if tip and _pending(clone, sid, start):
                    _check_size(clone, sid, start, tip)
                    review.show(meta, clone, start, tip, patch,
                                f"as committed in the sandbox: cave merge rebases them onto {clone.base} first")
        except CaveError as error:
            warn(f"{clone.name}: {error}")
            shown = False
    return shown


def merge(sid, force=False, yes=False, patch=False):
    """Land each repo independently, then delete the sandbox if it has stopped.

    A running sandbox keeps running and gives up committed work only, since snapshotting
    a live Claude would race it. force deletes a stopped sandbox despite work no repo can
    carry, once that's named. It skips nothing, and a repo held back still keeps the
    sandbox. Returns False if any repo was held back, or if a stopped sandbox had to be kept.
    """
    meta = sandbox.require_meta(sid)
    running = _refresh(sid, meta)
    targets = sandbox.clones(meta)
    done, landed = _land_all(sid, targets, running, yes, patch)
    for clone in done:
        git.delete_sandbox_refs(clone.repo, sid)
        if not running:
            sandbox.update_repo(sid, clone.key, merged=True)
    if len(done) != len(targets):
        if force:
            warn(f"sandbox {sid} is kept: --force doesn't drop a repo held back. cave discard {sid} does")
        return False
    if running:
        if not landed:
            say("nothing new to merge")
        say(f"sandbox {sid} is still running, so only committed work counts and it stays up")
        return True
    unlanded = sandbox.unlanded(sid, sandbox.load_meta(sid))
    if unlanded:
        warn(f"cave can't land these from sandbox {sid}: {', '.join(unlanded)}\n"
             f"They are repos created in the VM, or files outside any repo, and they stay on volume {volume_name(sid)}.\n"
             f"To copy the volume out first (treat what comes out as untrusted), run:\n  {sandbox.copy_out_command(sid, meta)}")
        if not force:
            warn(f"sandbox {sid} is kept. Use cave merge --force {sid} to delete it anyway.")
            return False
        warn(f"deleting sandbox {sid} anyway, and those with it")
    sandbox.delete(sid)
    return True


def resolve(sid):
    """Give the repos that can't land the current main, and Claude the job of rebasing onto it.

    A running sandbox just gets a note. A stopped one gets a new VM on its own volume, which
    still has the conflicting commits, and any merge or rebase it stopped in. That VM has every
    repo, so even one already merged can change, and goes through cave merge again. What comes
    back is an ordinary change set: cave merge reviews it in full against main.
    """
    meta = sandbox.require_meta(sid)
    running = _refresh(sid, meta)
    targets = [clone for clone in sandbox.clones(meta) if clone.conflict or sandbox.mid_operation(sid, clone.key)]
    if not targets:
        raise CaveError(f"sandbox {sid} has no conflict, paused merge or other repo held back for Claude to fix. "
                        "cave merge says what is held back and why")
    for clone in targets:
        with git.lock(clone.repo):
            transport.send_repo(sid, clone.key, clone.repo, clone.base, update=True)
    if not running:
        for entry in meta["repos"]:
            sandbox.update_repo(sid, entry["clone"], merged=False)
        vm_paths = {entry["clone"]: entry["vm"] for entry in meta["repos"]}
        repos = ", ".join(f"{vm_paths[clone.key]} (onto {clone.base})" for clone in targets)
        return launch.resume(meta, [BRIEFING.format(repos=repos, branch=f"claude/{sid}")])
    if not container.run_companion(sid, "note"):
        warn(f"sandbox {sid} did not take the note")
    say(f"delivered the current main to {sid}. Claude is told at its next prompt or tool call. Then run cave merge {sid}")
    return 0


def stop(sid):
    sandbox.require_meta(sid)
    if sid not in container.running():
        raise CaveError(f"sandbox {sid} is not running")
    if not container.stop(sid):
        raise CaveError(f"could not stop sandbox {sid}")
    say(f"stopped {sid}. Next: cave diff {sid}, cave merge {sid} or cave discard {sid}")


def discard(sid):
    repo = git.toplevel()
    has_ref = repo and git.sandbox_refs(repo, sid)
    if not box_path(sid).exists() and not has_ref:
        raise CaveError(f"no such sandbox: {sid}")
    sandbox.delete(sid, [repo] if repo else [])
    say(f"discarded {sid}")
