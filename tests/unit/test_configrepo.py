import json
from pathlib import Path

import pytest

from cave import configrepo, git, review
from cave._testing.repos import clone_bundle, run_git
from cave.errors import CaveError
from cave.paths import config_repo
from cave.sandbox import Clone


def test_init_creates_a_committed_marketplace():
    configrepo.init()
    repo = config_repo()
    marketplace = json.loads((repo / ".claude-plugin/marketplace.json").read_text())
    source = marketplace["plugins"][0]["source"]
    assert (repo / source / ".claude-plugin/plugin.json").is_file()
    assert (repo / source / ".mcp.json").is_file()
    assert git.query(repo, "status", "--porcelain") == ""
    assert git.base_branch(repo) == "main"
    assert (repo / "Dockerfile").read_text().startswith("FROM cave-base:latest\n")
    assert (repo / "host-settings.json").read_text() == "{}\n"
    assert (repo / "cave-settings.json").is_file() and not (repo / "settings.json").exists()
    assert {"cave-settings.json", "host-settings.json"} <= set(git.config_values(repo, "cave.flagged"))


def test_the_entrypoint_reads_cave_settings_not_the_host_ones():
    entrypoint = (Path(configrepo.__file__).parent / "assets" / "image" / "entrypoint.sh").read_text()
    assert '"$config/cave-settings.json"' in entrypoint
    assert '"$config/settings.json"' not in entrypoint and "host-settings" not in entrypoint


def test_init_flags_what_acts_on_the_host_and_no_clone_inherits_that(tmp_path):
    configrepo.init()
    repo = config_repo()
    run_git(repo, "checkout", "-q", "-b", "work")
    for path in ("cave-settings.json", "host-settings.json", "Dockerfile", ".claude-plugin/marketplace.json", "plugins/cave-config/skills/new/SKILL.md", "notes.txt"):
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text("changed\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", "work")
    clone = Clone("config", repo, "main", "(config repo)", None, None)
    head = review._report({}, clone, "main", "HEAD", False, "note")[0].split("commits, note:")[0]
    assert "cave-settings.json" in head and "host-settings.json" in head and "Dockerfile" in head and "marketplace.json" in head and "plugins/cave-config/skills/new/SKILL.md" in head
    assert "notes.txt" not in head
    assert git.config_values(clone_bundle(repo, tmp_path / "clone"), "cave.flagged") == []


def test_init_never_overwrites():
    config_repo().mkdir()
    with pytest.raises(CaveError, match="already exists"):
        configrepo.init()
