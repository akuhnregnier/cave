from cave import discover
from cave._testing.repos import make_repo, run_git


def test_finds_the_root_and_nested_repos_parents_first(tmp_path, repo):
    make_repo(repo / "libs")
    (repo / "deep/er").mkdir(parents=True)
    make_repo(repo / "deep/er/lib")
    assert discover.find_repos(repo) == [".", "libs", "deep/er/lib"]


def test_skips_dependency_folders(repo):
    (repo / "node_modules").mkdir()
    make_repo(repo / "node_modules/dep")
    assert discover.find_repos(repo) == ["."]


def test_a_git_file_is_not_a_repo(repo):
    (repo / "sub").mkdir()
    (repo / "sub/.git").write_text("gitdir: ../.git/modules/sub\n")
    assert discover.find_repos(repo) == ["."]


def test_does_not_follow_symlinks(tmp_path, repo):
    outside = make_repo(tmp_path / "outside")
    (repo / "link").symlink_to(outside)
    assert discover.find_repos(repo) == ["."]


def test_repos_without_a_base_branch_are_reported(repo):
    (repo / "empty").mkdir()
    run_git(repo / "empty", "init", "-q", "-b", "main")
    usable, skipped = discover.usable_repos(repo)
    assert usable == [(".", "main")]
    assert skipped == [("empty", "no commits on main or master")]


def test_ignored_repos_are_left_out(repo):
    config = make_repo(repo / "config")
    assert discover.usable_repos(repo, ignore=[config])[0] == [(".", "main")]


def test_an_empty_git_directory_is_reported_not_trusted(repo):
    (repo / "tests/fixtures/.git").mkdir(parents=True)
    usable, skipped = discover.usable_repos(repo)
    assert usable == [(".", "main")]
    assert [rel for rel, _ in skipped] == ["tests/fixtures"]
