"""Scaffold the config repo, a local plugin marketplace."""
import shutil
from pathlib import Path

from cave.errors import CaveError
from cave.git import git
from cave.paths import config_repo

TEMPLATE = Path(__file__).parent / "assets" / "config"
FLAGGED = ("settings.json", "plugins/", "Dockerfile", ".claude-plugin/")  # these reach the host and every later VM


def init():
    repo = config_repo()
    if repo.exists():
        raise CaveError(f"{repo} already exists")
    repo.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE, repo)
    git(repo, "init", "-q", "-b", "main")
    for path in FLAGGED:
        git(repo, "config", "--add", "cave.flagged", path)  # in .git/config, so a clone neither gets nor sets it
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "Initial cave config")
    print(f"cave: created {repo}\nUse it on the host too: claude plugin marketplace add {repo}")
