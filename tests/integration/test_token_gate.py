"""Nothing here spends tokens: each test proves that real Claude tests can't start."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cave._testing.vm import SPEND_TOKENS_VAR, ask_claude

ROOT = Path(__file__).resolve().parents[2]


def env_without_approval():
    return {name: value for name, value in os.environ.items() if name != SPEND_TOKENS_VAR}


def run_pytest(*args):
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-n0", "-p", "no:cacheprovider", *args], cwd=str(ROOT),
                          env=env_without_approval(), capture_output=True, text=True)


def test_claude_tests_fail_without_the_variable():
    result = run_pytest("claude_api_tests")
    assert result.returncode == 1
    assert f"{SPEND_TOKENS_VAR}=1 is required" in result.stdout
    assert "passed" not in result.stdout
    assert "skipped" not in result.stdout


@pytest.mark.parametrize("value", [None, "0"])
def test_ask_claude_refuses_unset_or_any_other_value_before_touching_the_harness(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(SPEND_TOKENS_VAR, raising=False)
    else:
        monkeypatch.setenv(SPEND_TOKENS_VAR, value)
    with pytest.raises(pytest.fail.Exception, match=SPEND_TOKENS_VAR):
        ask_claude(None, "never sent")


def test_default_collection_excludes_claude_tests():
    result = run_pytest("--collect-only")
    assert result.returncode == 0
    assert "claude_api_tests" not in result.stdout
    assert "test_claude.py" not in result.stdout


def test_make_runs_nothing_unless_acknowledged():
    for answer in ("no\n", "y\n", "YES\n", ""):
        result = subprocess.run(["make", "test-integration-container"], cwd=str(ROOT), input=answer, text=True,
                                env=env_without_approval(), capture_output=True)
        assert result.returncode != 0
        assert "Nothing was run" in result.stdout
        assert SPEND_TOKENS_VAR not in result.stdout
