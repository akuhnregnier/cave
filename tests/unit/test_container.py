import json
import subprocess
from types import SimpleNamespace

import pytest

from cave import container
from cave._testing.repos import raises
from cave.errors import CaveError


def test_running_parses_quiet_listing(container_calls):
    container_calls.fake.stdout = "a\nb\n"
    assert container.running() == {"a", "b"}


@pytest.mark.parametrize("system_up", [True, False])
def test_a_failed_listing_is_never_an_empty_one(monkeypatch, system_up):
    def fake(*args, **kwargs):
        return SimpleNamespace(returncode=int(args[0] == "ls" or not system_up), stdout="")

    monkeypatch.setattr(container, "container", fake)
    with pytest.raises(CaveError):
        container.running(strict=True)
    if system_up:
        with pytest.raises(CaveError):
            container.running()
    else:
        assert container.running() == set()  # a system that's down runs nothing


def test_the_volume_listing_fails_loudly_and_has_a_time_limit(container_calls, monkeypatch):
    container_calls.fake.returncode = 1
    with pytest.raises(CaveError):
        container.own_volumes()

    def hang(*args, timeout, **kwargs):
        raise subprocess.TimeoutExpired("container", timeout)

    monkeypatch.setattr(container, "container", hang)
    with pytest.raises(CaveError, match="did not answer within"):
        container.own_volumes()


def test_every_question_to_a_hung_service_times_out_with_a_clear_message(monkeypatch):
    seen = []

    def hang(*args, timeout, **kwargs):
        seen.append(timeout)
        raise subprocess.TimeoutExpired("container", timeout)

    monkeypatch.setattr(container, "container", hang)
    monkeypatch.setattr(container.shutil, "which", lambda name: "/bin/container")
    for call in (lambda: container.stop("x"), container.image_names,
                 container.images_in_use, container.ensure_system, lambda: container.running(strict=True)):
        with pytest.raises(CaveError, match="did not answer within"):
            call()
    assert all(timeout is not None for timeout in seen) and seen[0] <= container.STOP_SECONDS + 10


def test_a_volume_is_ours_only_with_this_state_dirs_label(container_calls):
    container.create_volume("ours")
    label = container_calls.log[-1][3].split("=")[1]
    container_calls.fake.stdout = json.dumps([
        {"configuration": {"name": "ours", "labels": {"cave": label}}},
        {"configuration": {"name": "no label", "labels": {}}},
        {"configuration": {"name": "another HOME", "labels": {"cave": "other"}}},
    ])
    assert container.own_volumes() == ["ours"]


@pytest.mark.parametrize("stdout", ["", "not json", "{}x", "[{}]", '[{"configuration": {"name": "x"}}]', "[1]"])
def test_a_volume_listing_that_makes_no_sense_is_unknown(container_calls, stdout):
    container_calls.fake.stdout = stdout
    with pytest.raises(CaveError):
        container.own_volumes()


def test_a_missing_volume_counts_as_deleted(container_calls):
    container_calls.fake.returncode = 1
    assert not container.delete_volume("v")  # the listing failed too, so it's unknown
    container_calls.fake.returncode, container_calls.fake.stdout = 0, "other\n"
    assert container.delete_volume("v")
    container_calls.fake.stdout = "v\n"
    assert not container.delete_volume("v")


def test_cleanup_that_hangs_gives_up(monkeypatch):
    monkeypatch.setattr(container, "container", raises(subprocess.TimeoutExpired("container", 1)))
    assert "still running" in container.force_delete("x")
    assert not container.delete_volume("v")


def test_force_delete_says_why_unless_there_was_nothing_to_delete(container_calls):
    assert container.force_delete("x") == ""
    container_calls.fake.returncode, container_calls.fake.stderr = 1, 'notFound: "container with ID x not found"'
    assert container.force_delete("x") == ""
    container_calls.fake.stderr = "volume in use\n"
    assert container.force_delete("x") == "volume in use"


def test_run_to_end_returns_the_end_of_what_a_failed_vm_said(monkeypatch):
    script = "python3 -c 'print(\"x\" * 100000 + \"the end\")' >&2; exit 3"
    monkeypatch.setattr(container, "container", lambda *args, **kwargs: subprocess.run(args, **kwargs))
    said = container.run_to_end("sh", "-c", script)
    assert said.endswith("the end") and len(said) == container.MAX_ERROR - 1
    assert container.run_to_end("sh", "-c", "exit 3") == "container run exited with status 3"
    assert container.run_to_end("true") == ""


def test_the_token_goes_over_stdin_and_retries_until_the_vm_is_up(monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0 if len(calls) == 2 else 1)

    monkeypatch.setattr(container, "container", fake)
    monkeypatch.setattr(container.time, "sleep", lambda seconds: None)
    assert container.send_token("sid", "s3cret", SimpleNamespace(poll=lambda: None))
    assert len(calls) == 2
    assert calls[1][1]["timeout"] < calls[0][1]["timeout"] <= container.TOKEN_SECONDS  # shares one deadline
    assert not any("s3cret" in arg for arg in calls[0][0])
    assert calls[0][1]["input"] == "s3cret\n"


def test_a_vm_that_exits_gets_no_token(container_calls):
    container_calls.fake.returncode = 1
    assert not container.send_token("sid", "s3cret", SimpleNamespace(poll=lambda: 1))
    assert len(container_calls.log) == 1


def test_build_never_uses_quiet_mode(container_calls):
    container.build("-t", "x", "ctx")
    assert container_calls.log == [("build", "--progress", "plain", "-t", "x", "ctx")]


def test_build_failure_is_a_cave_error(container_calls):
    container_calls.fake.returncode = 1
    with pytest.raises(CaveError, match="image build failed"):
        container.build("ctx")


def test_images_in_use_are_short_names(container_calls):
    container_calls.fake.stdout = json.dumps([
        {"configuration": {"image": {"reference": "docker.io/library/cave:20260101-abc"}}},
        {"configuration": {"image": {"reference": "ghcr.io/apple/builder:0.12.0"}}},
    ])
    assert container.images_in_use() == {"cave:20260101-abc", "builder:0.12.0"}


@pytest.mark.parametrize("stdout,returncode", [("", 0), ("not json", 0), ('[{"unexpected": 1}]', 0), ("null", 0), ("[]", 1)])
def test_images_in_use_raises_if_the_listing_is_unreadable_or_fails(container_calls, stdout, returncode):
    container_calls.fake.stdout = stdout
    container_calls.fake.returncode = returncode
    with pytest.raises(CaveError):
        container.images_in_use()


def test_ensure_system_explains_a_missing_install(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(CaveError, match="brew install container"):
        container.ensure_system()


def test_the_companion_runs_as_claude_and_its_output_is_not_read(container_calls):
    assert container.run_companion("box", "bundle")
    assert container_calls.log == [("exec", "--user", "claude", "box", "cave-companion", "bundle")]
    container_calls.fake.returncode = 1
    assert not container.run_companion("box", "bundle")


def test_a_hung_exec_counts_as_no_answer(monkeypatch):
    seen = {}

    def hang(*args, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired("container", 1)

    monkeypatch.setattr(container, "container", hang)
    assert not container.run_companion("box", "bundle")
    assert seen["timeout"] == container.EXEC_SECONDS and seen["stdout"] == subprocess.DEVNULL


def test_a_network_is_ours_only_with_this_state_dirs_label(container_calls):
    container.create_network("ours")
    assert container_calls.log[-1][:3] == ("network", "create", "--label")
    label = container_calls.log[-1][3].split("=")[1]
    container_calls.fake.stdout = json.dumps([
        {"configuration": {"name": "ours", "labels": {"cave": label}}},
        {"configuration": {"name": "default", "labels": {"com.apple.container.resource.role": "builtin"}}},
        {"configuration": {"name": "another HOME", "labels": {"cave": "other"}}},
    ])
    assert container.own_networks() == ["ours"]


def test_ensure_network_creates_only_a_missing_one_and_otherwise_raises(container_calls, monkeypatch):
    container_calls.fake.stdout = "[]"
    container.ensure_network("n")
    assert container_calls.log[-1][:2] == ("network", "create")
    label = container_calls.log[-1][3].split("=")[1]
    container_calls.fake.stdout = json.dumps([{"configuration": {"name": "n", "labels": {"cave": label}}}])
    del container_calls.log[:]
    container.ensure_network("n")
    assert [call[:2] for call in container_calls.log] == [("network", "ls")]
    container_calls.fake.stdout, container_calls.fake.returncode = "[]", 1
    with pytest.raises(CaveError):  # the listing fails
        container.ensure_network("n")
    container_calls.fake.returncode = 0
    original = container_calls.fake

    def create_fails(*args, **kwargs):
        result = original(*args, **kwargs)
        result.returncode = int(args[1] == "create")
        return result

    monkeypatch.setattr(container, "container", create_fails)
    with pytest.raises(CaveError, match="could not create network n"):
        container.ensure_network("n")


def test_only_a_network_with_our_label_is_ever_deleted(container_calls):
    label = None
    container.create_network("x")
    label = container_calls.log[-1][3].split("=")[1]
    del container_calls.log[:]
    container_calls.fake.stdout = json.dumps([{"configuration": {"name": "theirs", "labels": {"cave": "other"}}}])
    assert container.delete_network("theirs") and container.delete_network("missing")
    assert [call[:2] for call in container_calls.log] == [("network", "ls")] * 2  # asked, and left alone
    container_calls.fake.stdout = json.dumps([{"configuration": {"name": "mine", "labels": {"cave": label}}}])
    assert not container.delete_network("mine")  # still listed after the delete: container refused
    assert ("network", "delete", "mine") in container_calls.log


def test_network_cleanup_that_hangs_or_cannot_list_counts_as_not_deleted(monkeypatch, container_calls):
    container_calls.fake.returncode = 1
    assert not container.delete_network("n")
    monkeypatch.setattr(container, "container", raises(subprocess.TimeoutExpired("container", 1)))
    assert not container.delete_network("n")
    with pytest.raises(CaveError, match="did not answer within"):
        container.ensure_network("n")
