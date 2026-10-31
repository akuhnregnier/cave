"""Nothing a VM asks for happens without the user's yes, and what it's asked is what happens."""
import pytest

from cave import launch, requests, review, sandbox
from cave._testing.repos import SID, make_repo, run_git
from cave.errors import CaveError
from cave.paths import box_path

pytestmark = pytest.mark.usefixtures("active")
NAME = "request-0123456789abcdef"


@pytest.fixture
def meta(active, box, tmp_path, repo):
    """A running sandbox rooted at a folder that holds the repo."""
    active.add(SID)
    (box / "inbox").mkdir()
    (box / "staging").mkdir()
    meta = dict(sandbox.load_meta(SID), name="box", root=str(tmp_path), workspace="/workspace/w")
    meta["repos"][0]["vm"] = "/workspace/w/project"
    return meta


@pytest.fixture
def asked(meta, monkeypatch):
    """What the user was asked, each answered with terminal.answer."""
    terminal = type("Terminal", (), {"answer": True, "questions": []})
    monkeypatch.setattr(review, "on_terminal", lambda: True)
    monkeypatch.setattr(review, "ask", lambda question: terminal.questions.append(question) or terminal.answer)
    sandbox.write_meta(SID, meta)
    return terminal


def request(text):
    (box_path(SID) / "outbox" / NAME).write_text(text)


def in_sandbox():
    return [entry["clone"] for entry in sandbox.load_meta(SID)["repos"]]


def test_no_means_no_and_the_request_is_gone(asked, tmp_path):
    make_repo(tmp_path / "other")
    asked.answer = False
    request("add-repo other\n")
    requests.walk(SID)
    assert len(asked.questions) == 1 and str(tmp_path / "other") in asked.questions[0]
    assert in_sandbox() == ["code"] and sandbox.requests(SID) == []


def test_yes_adds_the_repo_it_showed(asked, tmp_path, container_calls):
    make_repo(tmp_path / "other")
    request("add-repo ./x/../other\n")
    requests.walk(SID)
    assert str(tmp_path / "other") in asked.questions[0] and "x/.." not in asked.questions[0]
    assert in_sandbox() == ["code", "code/other"]
    assert container_calls.log[-1][:-1] == ("exec", "--user", "claude", SID, "cave-companion", "add")


@pytest.mark.parametrize("path", ["../{repo}", "{absolute}", "/etc", "link", "/"])
def test_a_path_outside_the_root_is_never_put_to_the_user(asked, tmp_path, path, capsys):
    outside = make_repo(tmp_path.parent / f"{tmp_path.name}-outside")
    (tmp_path / "link").symlink_to(tmp_path.parent)
    request(f"add-repo {path.format(repo=outside.name, absolute=outside)}\n")
    requests.walk(SID)
    assert asked.questions == [] and in_sandbox() == ["code"]
    assert "outside the sandbox's root" in capsys.readouterr().err


def test_control_text_the_format_lets_through_never_reaches_the_prompt_raw(asked, tmp_path, capfd):
    make_repo(tmp_path / "a\u202eb\u009b2J")
    request("add-repo a\u202eb\u009b2J\n")
    requests.walk(SID)
    assert asked.questions and "\u202e" not in asked.questions[0] and "\u009b" not in asked.questions[0]
    assert r"a\u202eb\x9b2J" in asked.questions[0]
    captured = capfd.readouterr()
    assert "\u202e" not in captured.err + captured.out


def test_a_folder_that_is_no_repo_is_refused_before_any_question(asked, tmp_path, capsys):
    (tmp_path / "plain").mkdir()
    request("add-repo plain\n")
    requests.walk(SID)
    assert asked.questions == [] and "not the top of a git repo" in capsys.readouterr().err


def test_without_a_terminal_requests_are_listed_and_kept(meta, monkeypatch, capsys):
    sandbox.write_meta(SID, meta)
    monkeypatch.setattr(launch, "add_repo", lambda *args: pytest.fail("acted without an answer"))
    request("add-repo ‮evil\n")
    with pytest.raises(CaveError, match="takes a terminal"):
        requests.walk(SID)
    assert "\\u202eevil" in capsys.readouterr().out
    assert len(sandbox.requests(SID)) == 1


def test_a_repo_a_vm_would_be_handed_twice_is_refused(meta, repo):
    with pytest.raises(CaveError, match="already in"):
        launch.added_entry(meta, str(repo))


def test_a_repo_where_its_parent_tracks_files_is_refused(meta, repo):
    run_git(repo, "checkout", "-q", "-b", "side")  # base stays as it is, with file.txt
    (repo / "file.txt").unlink()
    run_git(repo, "commit", "-qam", "gone")
    make_repo(repo / "file.txt")
    with pytest.raises(CaveError, match="tracks files"):
        launch.added_entry(meta, str(repo / "file.txt"))


def test_repos_in_the_way_are_refused(meta, repo, tmp_path):
    run_git(tmp_path, "init", "-q", "-b", "main")
    run_git(tmp_path, "commit", "-q", "--allow-empty", "-m", "first")
    with pytest.raises(CaveError, match="in the way"):
        launch.added_entry(meta, str(tmp_path))


def test_a_repo_outside_the_root_gets_its_own_place(meta, tmp_path):
    other = make_repo(tmp_path.parent / f"{tmp_path.name}-other")
    entry = launch.added_entry(meta, str(other))
    assert entry["clone"].startswith("added/") and entry["vm"].startswith("/workspace/.cave-added/")
