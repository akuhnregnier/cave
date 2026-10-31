"""Real Claude sessions in real VMs. These use real tokens.

Run them with `make test-integration-container`, which sets CAVE_TEST_SPEND_TOKENS
only after a typed acknowledgement. Without that variable they fail before any
Claude run. They live outside tests/ so that nothing collects them by default.
"""
import pytest

from cave._testing.vm import ask_claude

pytestmark = pytest.mark.claude_api


def test_claude_works_in_the_vm_and_its_changes_merge(claude_vm):
    prompt = "Run `uname -s` with Bash and write its exact output to kernel.txt. Do not commit. Reply with DONE."
    result = ask_claude(claude_vm, prompt)
    assert result.returncode == 0, result.stderr
    sid = claude_vm.boxes()[-1]
    assert "+Linux" in claude_vm.cave("diff", "-p", sid).stdout
    claude_vm.cave("merge", sid)
    assert "Linux" in (claude_vm.repo / "kernel.txt").read_text()
    assert claude_vm.git("log", "-1", "--format=%s", "main") == "WIP"


def test_the_managed_hooks_brief_a_real_claude(claude_vm):
    prompt = "Your session context has a note from cave about origin. Reply with only the word after 'origin is a'."
    result = ask_claude(claude_vm, prompt)
    assert result.returncode == 0, result.stderr
    assert "read-only" in result.stdout


def test_continue_resumes_the_last_session_from_a_new_sandbox(claude_vm):
    first = ask_claude(claude_vm, "Remember the codeword PLUM-7731. Reply with OK.")
    assert first.returncode == 0, first.stderr
    second = ask_claude(claude_vm, "What was the codeword? Reply with only the codeword.", "--continue")
    assert second.returncode == 0, second.stderr
    assert "PLUM-7731" in second.stdout


def test_config_repo_instructions_reach_claude(claude_vm):
    claude_vm.cave("init")
    with (claude_vm.config / "CLAUDE.md").open("a") as instructions:
        instructions.write("\nIf asked for the cave test phrase, reply with exactly ZEBRA-2291.\n")
    claude_vm.git("commit", "-qam", "test instructions", repo=claude_vm.config)
    result = ask_claude(claude_vm, "What is the cave test phrase?")
    assert result.returncode == 0, result.stderr
    assert "ZEBRA-2291" in result.stdout
