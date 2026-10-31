"""Create a sandbox and run Claude in its VM."""
import hashlib
import json
import os
import posixpath
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from cave import auth, container, discover, git, image, sandbox, transport, watch
from cave.errors import CaveError
from cave.log import clean, mute, warn
from cave.paths import box_path, config_repo, network_name, new_id, sessions_dir, volume_name
from cave.prune import FAILED, prune, sweep_networks

VM_INBOX = "/cave/inbox"
VM_OUTBOX = "/cave/outbox"
VM_SECRETS = "/run/cave"
VM_VOLUME = "/workspace"
VM_CONFIG = f"{VM_VOLUME}/.cave-config"
VM_ADDED = f"{VM_VOLUME}/.cave-added"  # repos from outside the root
VM_PROJECTS = "/home/claude/.claude/projects"
CONFIG_NAME = "(config repo)"  # brackets, so it can't match a discovered repo's path
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}")
HOUSEKEEPING_SECONDS = 30
SWEEP_SECONDS = 15  # in all, for the leaked networks in the way of a launch


def _project(root):
    return re.sub(r"[^\w.-]", "_", Path(root).name)


def _root_hash(root, length):
    return hashlib.sha256(root.encode()).hexdigest()[:length]


def _project_key(root):
    return f"cave-{_project(root)}-{_root_hash(root, 4)}"


def _check_label(root, label):
    """Refuse a bad --name before any of the slow work."""
    if not LABEL_RE.fullmatch(label):
        raise CaveError(f"bad --name {label}: use up to 40 letters, digits, dots, dashes or underscores")
    name = f"{_project_key(root)}-{label}"
    if any(sandbox.load_meta(sid).get("name") == name for sid in sandbox.all_ids()):
        raise CaveError(f"a sandbox called {name} already exists")


def _repo_entry(name, repo, base, clone, vm):
    return {"name": name, "repo": repo, "base": base, "clone": clone, "vm": vm, "origin": git.public_origin(repo)}


def _workspace_entry(rel, repo, base, workspace):
    """A repo at the same relative path in the box's code folder and in the VM's workspace."""
    return _repo_entry(rel, repo, base, posixpath.normpath(f"code/{rel}"), posixpath.normpath(f"{workspace}/{rel}"))


def _volumes(mounts):
    return [arg for source, target in mounts.items() for arg in ("-v", f"{source}:{target}")]


def _plan(root, label=None):
    """Work out what goes in a sandbox. Returns (meta, skipped)."""
    project = _project(root)
    workspace = f"{VM_VOLUME}/{project}"
    config = config_repo()
    has_config = (config / ".git").exists()
    usable, skipped = discover.usable_repos(root, ignore=[config] if has_config else [])
    if not usable:
        if (Path(root) / ".git").is_file():
            raise CaveError(f"{root} is a linked worktree or submodule; run cave from the main clone")
        raise CaveError(f"no git repos with commits on main or master in {root}")
    entries = []
    for rel, base in usable:
        repo = str(Path(root) / rel) if rel != "." else root
        entries.append(_workspace_entry(rel, repo, base, workspace))
    if has_config:
        entries.append({"name": CONFIG_NAME, "repo": str(config), "base": git.base_branch(config),
                        "clone": "config", "vm": VM_CONFIG, "origin": ""})
    else:
        warn(f"no config repo at {config}; run `cave init` to create one")
    sid = new_id()
    key = _project_key(root)
    return {"id": sid, "key": key, "name": f"{key}-{label or sid[-4:]}", "project": project, "root": root,
            "workspace": workspace, "repos": entries}, skipped


def _tracked_by_parent(entry, parents):
    """Does a repo going in the sandbox have files where this one would be cloned?"""
    return any(entry["vm"].startswith(parent["vm"] + "/")
               and git.tracks(parent["repo"], parent["base"], entry["vm"][len(parent["vm"]) + 1:])
               for parent in parents)


def _create_sandbox(meta, skipped):
    """Bundle every repo into the inbox. One that can't be bundled moves from meta to skipped."""
    sid = meta["id"]
    box = box_path(sid)
    box.mkdir(parents=True, mode=0o700)
    for name in ("inbox", "outbox", "staging"):
        (box / name).mkdir(mode=0o700)
    sandbox.write_meta(sid, meta)
    bundled = []
    for entry in meta["repos"]:
        try:
            if _tracked_by_parent(entry, bundled):
                skipped.append((entry["name"], "its parent repo tracks files at this path"))
                continue
            transport.send_repo(sid, entry["clone"], entry["repo"], entry["base"])
            bundled.append(entry)
        except (OSError, CaveError, subprocess.CalledProcessError):
            skipped.append((entry["name"], "it could not be bundled"))
    if not any(entry["name"] != CONFIG_NAME for entry in bundled):
        sandbox.rmtree(box)
        raise CaveError(f"the repos in {meta['root']} could not be bundled")
    meta["repos"] = bundled
    sandbox.write_meta(sid, meta)


def _nested(entry, meta):
    """Paths of the repos inside this one, relative to it."""
    prefix = entry["vm"] + "/"
    return [other["vm"][len(prefix):] for other in meta["repos"] if other["vm"].startswith(prefix)]


def _has_uncommitted(entry, meta):
    excludes = [f":(top,exclude,literal){rel}" for rel in _nested(entry, meta)]
    return bool(git.query(entry["repo"], "status", "--porcelain", "--untracked-files=all", "--", ".", *excludes))


def _not_included(entry, meta):
    return (["uncommitted changes"] if _has_uncommitted(entry, meta) else []) + git.left_out(entry["repo"], entry["base"])


def _announce(meta, skipped):
    repos = meta["repos"]
    warn(f"sandbox {meta['name']} has {len(repos)} {'repo' if len(repos) == 1 else 'repos'}:")
    width = max(len(entry["name"]) for entry in repos)
    for entry in repos:
        note = "".join(f" ({what} not included)" for what in _not_included(entry, meta))
        print(clean(f"  {entry['name']:<{width}}  {entry['base']}  {entry['repo']}{note}"), file=sys.stderr)
    for rel, reason in skipped:
        print(clean(f"  skipped {rel}: {reason}"), file=sys.stderr)
    sys.stderr.flush()


def _warn_if_nested(meta):
    """Nesting is allowed, but a parent sandbox still holding these repos is worth knowing about."""
    repos = {entry["repo"] for entry in meta["repos"] if entry["name"] != CONFIG_NAME}
    parents = Path(meta["root"]).parents
    for sid in sandbox.all_ids():
        other = sandbox.load_meta(sid)
        shared = repos & {entry["repo"] for entry in other.get("repos", []) if not entry.get("merged")}
        if shared and Path(other["root"]) in parents:
            warn(f"sandbox {other.get('name', sid)} at {other['root']} already covers {', '.join(sorted(shared))}")


def _escape_pattern(path):
    """A literal path as a gitignore pattern."""
    return re.sub(r"([\\*?\[\]])", r"\\\1", path)


def _session_key(meta):
    """Per root, so that two projects with the same name can't see each other's sessions."""
    return f"{meta['project']}-{_root_hash(meta['root'], 8)}"


def _vm_repo(entry, meta):
    """What the VM needs to know about a repo."""
    return {"path": entry["vm"], "key": sandbox.report_key(entry["clone"]), "origin": entry["origin"],
            "bundle": f"{VM_INBOX}/{transport.inbox_name(entry['clone'])}", "base": entry["base"],
            "exclude": [f"/{_escape_pattern(rel)}/" for rel in _nested(entry, meta)]}


def _vm_args(meta, resume):
    """What any VM of this sandbox gets, whatever it's there to do."""
    args = ["--cpus", os.environ.get("CAVE_CPUS", "4"), "--memory", os.environ.get("CAVE_MEMORY", "4G")]
    variables = {
        "CAVE_ID": meta["id"], "CAVE_NAME": meta["name"], "CAVE_WORKSPACE": meta["workspace"],
        "CAVE_REPOS": json.dumps([_vm_repo(entry, meta) for entry in meta["repos"]]),
        "CAVE_GIT_NAME": git.query(meta["root"], "config", "user.name"),
        "CAVE_GIT_EMAIL": git.query(meta["root"], "config", "user.email"),
    }
    if meta.get("network"):  # one made by an older cave has none, so it stays on the default
        args += ["--network", network_name(meta["id"])]
    if resume:
        variables["CAVE_RESUME"] = "1"
    for key, value in variables.items():
        args += ["-e", f"{key}={value}"]
    return args


def _run_args(meta, has_token, resume=False):
    sid = meta["id"]
    sessions = sessions_dir() / _session_key(meta)
    sessions.mkdir(parents=True, exist_ok=True)
    box = box_path(sid)
    # No --rm: the container outlives its session, so nothing secret may be in its config.
    # Root only for the image's shim, as a user's Dockerfile layer may set any USER.
    args = ["run", "--name", sid, "-i", "--user", "root", "--tmpfs", VM_SECRETS, *_vm_args(meta, resume)]
    if sys.stdin.isatty() and sys.stdout.isatty():
        args.append("-t")
    for name in ("TERM", "COLORTERM"):
        if name in os.environ:
            args += ["-e", name]
    if has_token:
        args += ["-e", "CAVE_TOKEN_EXPECTED=1"]  # the token itself comes over stdin: see run()
    args += _volumes({volume_name(sid): VM_VOLUME, box / "outbox": VM_OUTBOX, sessions: VM_PROJECTS})
    return args + ["--mount", f"type=bind,source={box / 'inbox'},target={VM_INBOX},readonly"]


def _ensure_network(meta):
    """A VM of an old sandbox must not fall back to the shared network if its own has gone: make it again."""
    if meta.get("network"):  # only a flag: the name always comes from the id
        container.ensure_network(network_name(meta["id"]))


def _try_create_network(name):
    """Did the network get made? A create that hangs counts as one that failed."""
    try:
        return container.create_network(name)
    except CaveError:
        return False


def _create_network(sid):
    """Make the sandbox's network. A leak or two may be in the way, so sweep, retry once, then refuse."""
    name = network_name(sid)
    if _try_create_network(name):
        return
    try:
        _bounded_sweep()
    except FAILED as error:
        warn(f"could not sweep leaked networks: {error}")
    if not _try_create_network(name):
        raise CaveError(f"could not create network {name}. Each sandbox gets its own so that sandboxes can't reach "
                        "each other, and cave won't use the shared one instead")


def _bounded_sweep():
    """Sweep for at most SWEEP_SECONDS: what remains is left for the next sweep. Never raises past FAILED."""
    deadline = time.time() + SWEEP_SECONDS
    active = container.running(strict=True)
    worker = threading.Thread(target=lambda: _quietly(lambda: sweep_networks(active, deadline)), daemon=True)
    worker.start()
    worker.join(SWEEP_SECONDS)


def _quietly(work):
    try:
        work()
    except Exception as error:
        warn(f"could not sweep leaked networks: {error}")


def _wait_until_gone(sid, timeout=15):
    """`container run` returns while its VM is still being torn down and listed as running."""
    deadline = time.time() + timeout
    try:
        while sid in container.running() and time.time() < deadline:
            time.sleep(0.1)
    except CaveError:
        pass  # this was only ever a courtesy to the next command


def _housekeep(what, work):
    """Housekeeping must never stop a launch, so no failure counts, and it gets only so long.

    Past that it carries on behind the session, or dies with cave: all it does can be retried.
    It stays silent then, as the terminal is about to be Claude's.
    """
    def guarded():
        try:
            work()
        except Exception as error:
            warn(f"{what} failed: {error}")

    worker = threading.Thread(target=guarded, daemon=True)
    worker.start()
    worker.join(HOUSEKEEPING_SECONDS)
    mute(worker)


def _prune_images(tag):
    """Keep this launch's image, and the one each sandbox started from, which recovery needs."""
    metas = [sandbox.load_meta(sid) for sid in sandbox.all_ids()]
    if all(metas):  # one that can't be read might name any of them
        image.prune({tag, *(meta.get("image") for meta in metas)})


def _session(meta, tag, claude_args, resume=False):
    """Run Claude in a VM until it stops. Nothing keeps the token, so every session is sent it afresh."""
    sid, name = meta["id"], meta["name"]
    sandbox.update_meta(sid, warned=None)  # new work is coming, so an old warning no longer covers it
    token = auth.read_token()
    vm = container.boot(*_run_args(meta, bool(token), resume), tag, *claude_args)
    if token and not container.send_token(sid, token, vm):
        warn("the VM did not take its token, so Claude starts logged out")
    with watch.polling():
        status = vm.wait()
    _wait_until_gone(sid)
    warn(f"sandbox {name} stopped. Next: cave diff {name}, cave merge {name} or cave discard {name}")
    return status


def _take_over(meta):
    """Clear the way for a new VM on this volume, as a stopped container still holds it.

    Returns the image the sandbox started from, or the current one if that has gone.
    """
    container.ensure_system()
    tag = meta.get("image")
    if tag not in container.image_names():
        warn(f"the sandbox's image {tag} has gone, so this VM uses the current one")
        tag = image.ensure()
    failure = container.force_delete(meta["id"])
    if failure:
        warn(f"could not clear the sandbox's old container: {failure}")
    return tag


def recover(meta):
    """Have a short-lived VM take the snapshot a dead one didn't. Returns what went wrong, or ''.

    Its volume holds whatever the sandbox left, so nothing here runs as root, and it gets no
    Claude, token, sessions or inbox. It answers with files in the outbox, as the session would.
    """
    sid = meta["id"]
    try:
        _ensure_network(meta)  # not the shared network: the volume is the VM's to write, and others are listening
    except CaveError as error:
        return str(error)
    tag = _take_over(meta)
    args = ["run", "--name", sid, "--user", "claude", "--entrypoint", "cave-companion", *_vm_args(meta, True),
            *_volumes({volume_name(sid): VM_VOLUME, box_path(sid) / "outbox": VM_OUTBOX})]
    try:
        return container.run_to_end(*args, tag, "recover")
    finally:
        container.force_delete(sid)


def added_entry(meta, repo):
    """The entry for the repo at this real path joining a sandbox. Raises if it can't."""
    if not discover.is_repo_root(Path(repo)):
        raise CaveError(f"{repo} is not the top of a git repo")
    if any(os.path.realpath(entry["repo"]) == repo for entry in meta["repos"]):
        raise CaveError(f"{repo} is already in this sandbox")
    base, rel = git.base_branch(repo), os.path.relpath(repo, os.path.realpath(meta["root"]))
    if rel.split(os.sep)[0] == os.pardir:
        tail = f"{_project(repo)}-{_root_hash(repo, 4)}"
        entry = _repo_entry(Path(repo).name, repo, base, f"added/{tail}", f"{VM_ADDED}/{tail}")
    else:
        entry = _workspace_entry(rel, repo, base, meta["workspace"])
    if _tracked_by_parent(entry, meta["repos"]):
        raise CaveError(f"a repo in this sandbox tracks files at {repo}")
    if _nested(entry, meta):
        raise CaveError(f"repos in this sandbox are in the way of {repo}")
    return entry


def add_repo(meta, entry):
    """Bundle a repo into a running VM's inbox and have it cloned. It takes no restart or mount.

    Meta comes last, so it never lists a repo the VM says it didn't get. A VM that lies about
    that gets the repo held back, as a clone with no bundle always is.
    """
    sid = meta["id"]
    transport.send_repo(sid, entry["clone"], entry["repo"], entry["base"])
    if not container.run_companion(sid, "add", json.dumps(_vm_repo(entry, meta))):
        (box_path(sid) / "inbox" / transport.inbox_name(entry["clone"])).unlink(missing_ok=True)
        raise CaveError(f"sandbox {sid} could not clone {entry['repo']}, so it wasn't added")
    sandbox.add_repo(sid, entry)
    for what in _not_included(entry, meta):
        warn(f"{what} not included")


def resume(meta, claude_args):
    """Run Claude again on a stopped sandbox's volume, in a VM of its own."""
    _ensure_network(meta)
    return _session(meta, _take_over(meta), claude_args, resume=True)


def run(claude_args, label=None):
    root = git.toplevel() or str(Path.cwd())
    if label is not None:
        _check_label(root, label)
    container.ensure_system()
    _housekeep("prune", lambda: prune())
    tag = image.ensure()
    _housekeep("image prune", lambda: _prune_images(tag))
    meta, skipped = _plan(root, label)
    meta["image"] = tag
    meta["network"] = network_name(meta["id"])
    _create_sandbox(meta, skipped)
    _announce(meta, skipped)
    _warn_if_nested(meta)
    sid = meta["id"]
    if not container.create_volume(volume_name(sid)):
        sandbox.delete(sid)
        raise CaveError(f"could not create volume {volume_name(sid)}")
    try:
        _create_network(sid)
    except CaveError:
        sandbox.delete(sid)
        raise
    return _session(meta, tag, claude_args)
