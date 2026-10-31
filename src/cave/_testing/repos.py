"""Real git repositories and sandboxes to test against."""
import os
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from cave import git, sandbox
from cave._testing.harness import COMPANION
from cave.paths import box_path

SID = "20260101-000000-abcd"


def raises(error):
    """A stand-in for anything, that fails with this error when called."""
    def fail(*args, **kwargs):
        raise error

    return fail


def run_git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.DEVNULL, env=env)


def make_repo(path, env=None):
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main", env=env)
    (path / "file.txt").write_text("one\n")
    run_git(path, "add", "-A", env=env)
    run_git(path, "commit", "-q", "-m", "first", env=env)
    return path


def age_id(days):
    """A sandbox id that is `days` old."""
    return f"{datetime.now() - timedelta(days=days):%Y%m%d-%H%M%S}-abcd"


def repo_entry(clone, repo, name=None):
    return {"name": name or clone, "repo": str(repo), "base": "main", "clone": clone}


def make_box(sid, repo):
    """A sandbox directory with metadata, an empty code clone and an empty outbox."""
    (box_path(sid) / "code/.git").mkdir(parents=True)
    (box_path(sid) / "outbox").mkdir()
    sandbox.write_meta(sid, {"id": sid, "project": "p", "root": str(repo), "repos": [repo_entry("code", repo)]})
    return box_path(sid)


def clone_bundle(repo, path):
    """Clone repo's main to path the way the VM does, by way of a bundle."""
    with tempfile.TemporaryDirectory() as folder:
        bundle = Path(folder) / "repo.bundle"
        git.create_bundle(repo, bundle, "main")
        run_git(path.parent, "clone", "-q", "--single-branch", "--no-tags", "--branch", "main", str(bundle), str(path))
    return path


def make_clone(repo, path, sid):
    """Clone repo to path and commit a change on claude/<id>, as a VM would."""
    clone_bundle(repo, path)
    run_git(path, "update-ref", "refs/cave/delivered", "HEAD")
    run_git(path, "checkout", "-q", "-b", f"claude/{sid}")
    (path / "file.txt").write_text("sandbox\n")
    run_git(path, "commit", "-qam", "work")
    return path


def run_companion(sid, mode, clones, *args, **env):
    """Run the real companion as the VM would, for {meta `clone` key: clone path}, with extra arguments."""
    home = box_path(sid) / "vm-home"
    home.mkdir(exist_ok=True)
    (home / ".cave-branch").write_text(f"claude/{sid}\n")
    (home / ".cave-repos").write_text("".join(f"{sandbox.report_key(key)}\t{path}\n" for key, path in clones.items()))
    env = {**os.environ, "CAVE_VM_HOME": str(home), "CAVE_OUTBOX": str(box_path(sid) / "outbox"),
           "CAVE_WORKSPACE": str(box_path(sid) / "code"), "CAVE_VOLUME": str(box_path(sid) / "code"),
           "CAVE_INBOX": str(box_path(sid) / "inbox"), **env}
    return subprocess.run(["bash", str(COMPANION), mode, *args], env=env, capture_output=True, text=True)
