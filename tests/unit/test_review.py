import io
import shutil
import sys

import pytest

from cave import collect, git, log, review
from cave._testing.harness import write_exe
from cave._testing.repos import SID, run_git, run_companion
from cave.errors import CaveError
from cave.sandbox import Clone

META = {"name": "cave-p-abcd-x", "root": "/work"}


@pytest.fixture
def clone(repo):
    return Clone("code", repo, "main", "code", None, None)


def commit(repo, message="work"):
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-qm", message)


def report(clone, start="main", tip="HEAD", patch=False):
    return review._report(META, clone, start, tip, patch, "note")


@pytest.fixture
def branch(repo):
    run_git(repo, "checkout", "-q", "-b", "work")
    return repo


def add(repo, path, content="x\n"):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


@pytest.mark.parametrize("path, label", [
    (".gitattributes", "attributes"),
    ("deep/er/.GITATTRIBUTES", "attributes"),
    (".gitmodules", "git"),
    (".githooks/pre-push", "git"),
    (".husky/pre-commit", "git"),
    (".pre-commit-config.yaml", "git"),
    (".claude/settings.json", "agents"),
    ("claude.md", "agents"),
    ("pkg/AGENTS.md", "agents"),
    (".mcp.json", "agents"),
    (".cursorrules", "agents"),
    (".envrc", "environment"),
    (".mise.toml", "environment"),
    (".tool-versions", "environment"),
    (".vscode/tasks.json", "editors"),
    (".idea/x.xml", "editors"),
    (".github/workflows/ci.yml", "ci"),
    (".github/actions/x/action.yml", "ci"),
    (".lfsconfig", "git"),
    (".github/instructions/a.instructions.md", "agents"),
    (".codex/config.toml", "agents"),
    (".agents/skills/x", "agents"),
    (".npmrc", "environment"),
    (".cargo/config.toml", "environment"),
    (".devcontainer/devcontainer.json", "editors"),
    (".zed/settings.json", "editors"),
    (".nvim.lua", "editors"),
    (".exrc", "editors"),
])
def test_files_that_act_implicitly_are_flagged(clone, branch, path, label):
    add(branch, path)
    commit(branch)
    head = report(clone)[0].split("commits, note:")[0]
    assert f"  {label}" in head and path in head


@pytest.mark.parametrize("path", ["Makefile", "src/main.py", ".github/dependabot.yml", "CLAUDE.md.txt"])
def test_files_run_only_on_purpose_are_not(clone, branch, path):
    add(branch, path)
    commit(branch)
    assert "flagged: nothing" in report(clone)[0]


def test_a_decomposed_name_is_still_matched(clone, branch):
    run_git(branch, "config", "cave.flagged", "caf\u00e9/")
    add(branch, "cafe\u0301/x")
    commit(branch)
    assert "flagged in git config:" in report(clone)[0]


def test_host_config_adds_to_the_list_without_replacing_it(clone, branch):
    run_git(branch, "config", "--add", "cave.flagged", "Dockerfile")
    add(branch, "Dockerfile")
    add(branch, ".envrc")
    commit(branch)
    text = report(clone)[0].split("commits, note:")[0]
    assert "flagged in git config:" in text and "environment:" in text


def test_modes_are_called_out(clone, branch):
    (branch / "file.txt").chmod(0o755)
    (branch / "link").symlink_to("/etc/passwd")
    (branch / "blob").write_bytes(b"\0\1\2")
    run_git(branch, "add", "-A")
    run_git(branch, "update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},sub")
    run_git(branch, "commit", "-qm", "modes")
    text = report(clone)[0]
    assert "[executable bit, 100644 to 100755]" in text
    assert "A link  +1 -0  [symlink]" in text
    assert "A blob  +- --  [binary]" in text
    assert "[submodule]" in text


def test_binary_files_are_listed_not_printed(clone, branch):
    (branch / "blob").write_bytes(b"\0SECRET")
    commit(branch)
    text, findings = report(clone, patch=True)
    assert "Binary files" in text and "SECRET" not in text and not findings


def test_a_file_added_then_deleted_still_shows(clone, branch):
    add(branch, ".envrc")
    commit(branch, message="add")
    (branch / ".envrc").unlink()
    commit(branch, message="remove")
    text = report(clone)[0]
    assert text.count(".envrc") == 3  # flagged at the top, and once per commit
    assert "A .envrc" in text and "D .envrc" in text
    assert "environment:" in text


def test_a_failed_query_is_not_an_empty_report(clone):
    with pytest.raises(CaveError):
        report(clone, tip="no-such-ref")


@pytest.mark.parametrize("name, shown, finding", [
    ("a\x1b[2Jb", r"a\x1b[2Jb", "control characters"),
    ("x\ny  M forged.txt", r"x\ny  M forged.txt", "control characters"),
    ("tab\there", r"tab\there", "control characters"),
    ("gpj.\u202eexe", "gpj.\\u202eexe", "bidi or invisible characters"),
])
def test_hostile_names_cannot_forge_or_act(clone, branch, name, shown, finding):
    (branch / name).write_text("x\n")
    commit(branch)
    text, findings = report(clone)
    assert f"    A {shown}  +1 -0\n" in text + "\n" and "\x1b" not in text
    assert finding in findings


def test_invalid_utf8_in_a_name_survives(clone, branch):
    blob = git.strict(branch, "rev-parse", "HEAD:file.txt")
    run_git(branch, "update-index", "--add", "--cacheinfo", f"100644,{blob},bad\udcff")  # APFS would refuse the name
    run_git(branch, "commit", "-qm", "name")
    text, findings = report(clone)
    assert r"bad\xff" in text and "invalid UTF-8" in findings


def test_hostile_subject_and_author_are_escaped(clone, branch):
    add(branch, "new.txt")
    run_git(branch, "add", "-A")
    run_git(branch, "-c", "user.name=a\x1b]0;x\x07b", "commit", "-qm", "sub\x1b[2Jject", "--author", "e\x1b[31mvil <e@x>")
    text, findings = report(clone)
    assert r"e\x1b[31mvil" in text and r"sub\x1b[2Jject" in text
    assert "\x1b" not in text and findings == ["control characters"]


def test_host_diff_drivers_are_off_and_the_host_pager_is_out_of_the_picture(clone, branch, tmp_path):
    marker = tmp_path / "pwned"
    tool = tmp_path / "tool"
    write_exe(tool, f"#!/bin/sh\ntouch {marker}\n")
    for key, value in {"diff.external": tool, "diff.pwn.textconv": tool, "diff.pwn.command": tool, "core.pager": tool,
                       "color.ui": "always"}.items():
        run_git(branch, "config", key, str(value))
    (branch / ".git/info/attributes").write_text("* diff=pwn\n")
    add(branch, "new.txt", "\x1b[31mred\n")
    commit(branch)
    text = report(clone, patch=True)[0]
    assert r"+\x1b[31mred" in text and not marker.exists()


def test_patches_are_cut_at_long_lines_and_a_literal_escape_gets_no_warning(clone, branch):
    add(branch, "new.txt", r"a literal \x1b" + "\n")
    add(branch, "long.txt", "y" * (review.MAX_LINE + 50) + "\n")
    commit(branch)
    text, findings = report(clone, patch=True)
    assert "characters not shown]" in text and not findings


def on_tty(monkeypatch, tty=True):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)


def hostile_show(clone, branch):
    add(branch, "new.txt", "\x1b[31mred\n")
    commit(branch)
    review.show(META, clone, "main", "HEAD", True, "note")


@pytest.mark.parametrize("delta", [True, False])
def test_the_pager_only_ever_gets_escaped_text(clone, branch, tmp_path, monkeypatch, capfd, delta):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen"
    write_exe(bin_dir / "less", f'#!/bin/sh\ncat > "{seen}"\n')
    if delta:
        write_exe(bin_dir / "delta", f'#!/bin/sh\ntee "{seen}.delta"\n')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    on_tty(monkeypatch)
    hostile_show(clone, branch)
    for path in [seen] + ([tmp_path / "seen.delta"] if delta else []):
        data = path.read_bytes()
        assert b"\x1b" not in data and rb"+\x1b[31mred" in data
        assert data.startswith(b"warning: escaped control characters")
    assert "warning: escaped" in capfd.readouterr().err


@pytest.mark.skipif(not shutil.which("delta"), reason="delta is not installed")
def test_real_delta_shows_the_escape_as_text(clone, branch, monkeypatch, capfd):
    on_tty(monkeypatch)
    hostile_show(clone, branch)
    assert r"\x1b[31mred" in log.clean(capfd.readouterr().out)


@pytest.mark.usefixtures("active")
def test_approval_covers_one_result_so_a_moved_main_asks_again(repo, stopped_sandbox, monkeypatch):
    moved = []

    def answer_then_move(question):
        moved.append(1)
        if len(moved) == 1:
            run_git(repo, "commit", "-q", "--allow-empty", "-m", "moved")
        return True

    monkeypatch.setattr(review, "ask", answer_then_move)
    monkeypatch.setattr(review, "on_terminal", lambda: True)
    assert collect.merge(SID)
    assert len(moved) == 2


@pytest.mark.usefixtures("active")
def test_a_file_that_appears_during_the_prompt_is_not_overwritten(repo, stopped_sandbox, monkeypatch):
    new = stopped_sandbox / "new.txt"
    new.write_text("sandbox\n")
    run_git(stopped_sandbox, "add", "-A")
    run_git(stopped_sandbox, "commit", "-qm", "new")
    run_companion(SID, "--final", {"code": stopped_sandbox})

    def answer(question):
        (repo / "new.txt").write_text("mine\n")
        return True

    monkeypatch.setattr(review, "ask", answer)
    monkeypatch.setattr(review, "on_terminal", lambda: True)
    assert not collect.merge(SID)
    assert (repo / "new.txt").read_text() == "mine\n"


def test_redirected_output_cannot_approve(monkeypatch):
    monkeypatch.setattr(sys, "stdin", type("T", (), {"isatty": lambda self: True})())
    monkeypatch.setattr(sys.stderr, "isatty", lambda: True)
    on_tty(monkeypatch, False)
    assert not review.on_terminal()


@pytest.mark.usefixtures("active")
@pytest.mark.parametrize("answer", ["n\n", "\n", "", "yep\n"])
def test_anything_but_yes_rejects(repo, stopped_sandbox, monkeypatch, answer):
    before = git.strict(repo, "rev-parse", "main")
    monkeypatch.setattr(review, "on_terminal", lambda: True)
    monkeypatch.setattr(sys, "stdin", io.StringIO(answer))
    assert not collect.merge(SID)
    assert git.strict(repo, "rev-parse", "main") == before
    assert git.sandbox_refs(repo, SID) == []


def test_the_summary_repeats_the_flags_after_any_amount_of_output(clone, branch):
    for number in range(30):
        add(branch, f"junk/{number}.txt")
    add(branch, ".envrc")
    commit(branch)
    summary = []
    review._report(META, clone, "main", "HEAD", False, "note", summary)
    assert "31 file changes in 1 commits" in summary[0] and "environment: .envrc" in summary[0]


def test_a_patch_over_the_cap_is_cut_in_plain_view_and_the_flags_stay(clone, branch, monkeypatch):
    monkeypatch.setattr(review, "MAX_PATCH", 500)
    add(branch, "big.txt", "y\n" * 2000)
    add(branch, ".envrc")
    commit(branch)
    add(branch, "later.txt")
    commit(branch)
    text = report(clone, patch=True)[0]
    assert text.count("[cave: patch cut here at 0 MiB") == 2
    head = text.split("commits, note:")[0]
    assert "NOT complete" in head and ".envrc" in head and "big.txt" in text and "later.txt" in text


def test_gate_prints_the_summary_right_before_the_prompt(clone, branch, monkeypatch, capsys):
    for number in range(30):
        add(branch, f"junk/{number}.txt")
    add(branch, ".envrc")
    commit(branch)
    events = []
    monkeypatch.setattr(review, "on_terminal", lambda: True)
    monkeypatch.setattr(review, "ask", lambda question: events.append(capsys.readouterr().err) or True)
    review.gate(META, clone, "main", "HEAD", False, False)
    last = events[0].rstrip("\n").split("\n")[-1]
    assert last.startswith("cave: 31 file changes in 1 commits") and "environment: .envrc" in last


def test_gate_prints_the_summary_with_yes_too(clone, branch, capsys):
    add(branch, ".envrc")
    commit(branch)
    review.gate(META, clone, "main", "HEAD", True, False)
    assert capsys.readouterr().err.rstrip("\n").split("\n")[-1].startswith("cave: 1 file changes in 1 commits")


def test_the_cut_patch_is_named_in_the_summary_beside_the_prompt(clone, branch, monkeypatch):
    monkeypatch.setattr(review, "MAX_PATCH", 500)
    add(branch, "big.txt", "y\n" * 2000)
    commit(branch)
    summary = []
    review._report(META, clone, "main", "HEAD", True, "note", summary)
    assert "CUT" in summary[0] and "NOT complete" in summary[0]
    uncut = []
    review._report(META, clone, "main", "HEAD", False, "note", uncut)
    assert "CUT" not in uncut[0]


def test_the_patch_cap_keeps_escaping_fast(clone):
    assert review.MAX_PATCH == 16 << 20


def test_a_file_listing_that_hits_the_cap_is_an_error_not_a_shorter_list(clone, branch, monkeypatch):
    for number in range(30):
        add(branch, f"f{number}")
    commit(branch)
    monkeypatch.setattr(git, "MAX_LISTING", 200)
    with pytest.raises(CaveError, match="too many files to list"):
        report(clone)
