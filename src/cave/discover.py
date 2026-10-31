"""Find the git repos a sandbox should hold."""
import os
from pathlib import Path

from cave import git
from cave.errors import CaveError

# Dependency and cache folders: large, and any repos in them aren't the user's work.
SKIP_DIRS = {".git", ".cache", ".tox", ".venv", "__pycache__", "node_modules", "venv"}


def find_repos(root):
    """Relative paths of repos in and under root, parents first.

    Only a .git directory counts. A .git file is a submodule or a worktree, which
    belongs to its parent repo.
    """
    found = []
    for path, dirs, _ in os.walk(root):
        if os.path.isdir(os.path.join(path, ".git")):
            found.append(os.path.relpath(path, root))
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
    return sorted(found, key=lambda rel: (0 if rel == "." else rel.count(os.sep) + 1, rel))


def is_repo_root(repo):
    """False for a .git directory that git itself doesn't accept as this repo's."""
    git_dir = git.query(repo, "rev-parse", "--absolute-git-dir")
    return bool(git_dir) and os.path.realpath(git_dir) == os.path.realpath(repo / ".git")


def usable_repos(root, ignore=()):
    """Returns ([(relative path, base branch)], [(relative path, why it was skipped)])."""
    usable, skipped = [], []
    ignored = {Path(path).resolve() for path in ignore}
    for rel in find_repos(root):
        repo = Path(root) / rel
        if repo.resolve() in ignored:
            continue
        if not is_repo_root(repo):
            skipped.append((rel, "its .git directory isn't a valid repo"))
            continue
        try:
            usable.append((rel, git.base_branch(repo)))
        except CaveError:
            skipped.append((rel, "no commits on main or master"))
    return usable, skipped
