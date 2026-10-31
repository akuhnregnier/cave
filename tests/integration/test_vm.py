"""Real VMs, with the real image and entrypoint. Slow; skip with `-m "not vm"`."""
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from cave._testing.harness import write_exe
from cave import sandbox
from cave.paths import network_name, state_key, volume_name
from cave._testing.vm import (
    PRUNABLE_IMAGE,
    exit_interactive_session,
    in_vm,
    interactive_screen,
    LIVE,
    session,
    stop_timed,
    wait_until,
)

pytestmark = pytest.mark.vm


def test_full_cycle(stubbed_vm):
    run = stubbed_vm.cave("run", "--", "-p", "hello", ok=False)
    assert run.returncode == 7, run.stderr
    out = run.stdout
    assert "USER: claude PWD=/workspace/project" in out
    assert "PLUGINS: /workspace/.cave-config/plugins/cave-config" in out
    assert "loaded" in out.split("PLUGINLIST:")[1].splitlines()[0]
    assert ('SETTINGS: {"enableAllProjectMcpServers":true,"model":"sonnet",'
            '"sandbox":{"enabled":false,"x":1},"skipDangerousModePermissionPrompt":true}') in out
    assert "CLAUDEMD: # test instructions" in out
    assert "REMOTES: [origin] AUTHOR: t" in out

    sid = stubbed_vm.boxes()[-1]
    name = json.loads((stubbed_vm.box(sid) / "meta.json").read_text())["name"]
    assert f"ARGS: --dangerously-skip-permissions --name {name} -p hello" in out
    assert f"BRANCH: claude/{sid}" in out
    assert " stopped " in stubbed_vm.cave("ls").stdout

    diff = stubbed_vm.cave("diff", "-p", sid).stdout
    assert "+committed" in diff and "+leftover" in diff and "+edit" in diff

    sessions = stubbed_vm.root / "home/Library/Application Support/cave/sessions"
    assert list(sessions.glob("project-*/-workspace-project/2f75b9f7-cef5-417b-8933-cc966ac1cc27.jsonl"))

    stubbed_vm.cave("merge", sid)
    assert stubbed_vm.git("log", "--format=%s", "main").split("\n") == ["WIP", "committed by claude", "first"]
    assert stubbed_vm.git("log", "-1", "--format=%s", "main", repo=stubbed_vm.config) == "WIP"
    assert stubbed_vm.boxes() == []


def test_the_clones_outlive_the_container(stubbed_vm):
    assert stubbed_vm.cave("run", "--", "-p", "hello", ok=False).returncode == 7
    sid = stubbed_vm.boxes()[-1]
    assert sid in subprocess.run(["container", "ls", "--all", "--quiet"], capture_output=True, text=True).stdout.split()
    subprocess.run(["container", "delete", sid], capture_output=True, check=True)
    tag = json.loads((stubbed_vm.box(sid) / "meta.json").read_text())["image"]
    read = subprocess.run(["container", "run", "--rm", "-v", f"cave-{sid}:/workspace", "--entrypoint", "git", tag,
                           "-C", "/workspace/project", "-c", "safe.directory=*", "log", "--format=%s"],
                          capture_output=True, text=True)
    assert "committed by claude" in read.stdout, read.stderr
    assert network_name(sid) in network_names()  # and it was the sandbox's own, which the next test relies on
    stubbed_vm.cave("discard", sid)
    assert f"cave-{sid}" not in subprocess.run(["container", "volume", "ls", "--quiet"], capture_output=True, text=True).stdout
    assert network_name(sid) not in network_names()


def network_names():
    listing = subprocess.run(["container", "network", "ls", "--format", "json"], capture_output=True, text=True).stdout
    return [entry["id"] for entry in json.loads(listing)]


def vm_ip(sid):
    """(address, network) of a running VM, from `container`."""
    found = json.loads(subprocess.run(["container", "inspect", sid], capture_output=True, text=True).stdout)
    attached = found[0]["status"]["networks"][0]
    return attached["ipv4Address"].split("/")[0], attached["network"]


PROBE = ("import socket, sys; s = socket.socket(); s.settimeout(4)\n"
         "try: s.connect((sys.argv[1], 8080)); print('open')\n"
         "except OSError as e: print('closed', e)")


def reaches(sid, address):
    """Can this VM open a TCP connection to address, port 8080?"""
    said = subprocess.run(["container", "exec", sid, "python3", "-c", PROBE, address], capture_output=True, text=True)
    assert said.stdout.startswith(("open", "closed")), said.stderr
    reaches.last = said.stdout.strip()  # what the last probe said, for a failure to show
    return said.stdout.startswith("open")


def listen(sid):
    subprocess.run(["container", "exec", "-d", sid, "python3", "-m", "http.server", "8080", "-d", "/etc"], check=True,
                   capture_output=True)
    wait_until(lambda: reaches(sid, "127.0.0.1"), timeout=20)


def test_sandboxes_each_get_a_network_so_their_vms_cannot_reach_each_other(stubbed_vm):
    small = {"CAVE_CPUS": "1", "CAVE_MEMORY": "1G"}  # three VMs share the one slot this test holds
    first = stubbed_vm.start("run", "--", "hang", **small)
    second = control = None
    try:
        wait_until(lambda: stubbed_vm.boxes() and in_vm(stubbed_vm.boxes()[0], LIVE))
        one = stubbed_vm.boxes()[0]
        second = stubbed_vm.start("run", "--", "hang", **small)
        wait_until(lambda: len(stubbed_vm.boxes()) == 2 and in_vm(stubbed_vm.boxes()[1], LIVE))
        two = stubbed_vm.boxes()[1]
        (address_one, net_one), (address_two, net_two) = vm_ip(one), vm_ip(two)
        assert (net_one, net_two) == (network_name(one), network_name(two))
        image = json.loads((stubbed_vm.box(one) / "meta.json").read_text())["image"]
        # The control shares the first sandbox's network, so a closed port could only be the network's doing.
        subprocess.run(["container", "run", "-d", "--name", f"{one}-control", "--network", net_one, "--cpus", "1",
                        "--memory", "512M", "--entrypoint", "sleep", image, "300"], check=True, capture_output=True)
        control = f"{one}-control"
        listen(one)
        listen(two)
        try:
            wait_until(lambda: reaches(control, address_one), timeout=30)  # the control may still be coming up
        except AssertionError:
            raise AssertionError(f"{control} never reached {address_one}: {reaches.last}")
        assert not reaches(control, address_two)
        assert not reaches(one, address_two) and not reaches(two, address_one)
    finally:
        # VMs first: killing cave leaves its `container run` holding the pipes until its VM goes (300 s of sleep).
        if control:
            subprocess.run(["container", "delete", "--force", control], capture_output=True, timeout=60)
        for sid in stubbed_vm.boxes():
            subprocess.run(["container", "delete", "--force", sid], capture_output=True, timeout=60)
        for process in (first, second):
            if process:
                process.kill()
                process.communicate(timeout=60)
    for sid in stubbed_vm.boxes():
        stubbed_vm.cave("discard", sid)
        assert network_name(sid) not in network_names()


def test_the_token_reaches_claude_and_nothing_stores_it(stubbed_vm):
    token = "fake-token-3d1f09"
    write_exe(Path(stubbed_vm.env["PATH"].split(":")[0]) / "security", f"#!/bin/bash\necho {token}\n")
    with session(stubbed_vm, "hang") as (process, sid):
        as_claude = ["container", "exec", "--user", "claude", sid]
        # The stub's own sleep, by its command line: the newest `sleep` of any kind may be the watcher's, which
        # has the entrypoint's environment and so no token.
        found = subprocess.run([*as_claude, "sh", "-c", "pid=$(pgrep -nxf 'sleep 300') && cat /proc/$pid/environ"],
                               capture_output=True, text=True)
        assert f"CLAUDE_CODE_OAUTH_TOKEN={token}" in found.stdout, (
            f"status {found.returncode}, stderr {found.stderr!r}, processes: "
            + subprocess.run([*as_claude, "ps", "-eo", "pid,ppid,lstart,args"], capture_output=True, text=True).stdout)
        init_env = subprocess.run([*as_claude, "cat", "/proc/1/environ"], capture_output=True, text=True)
        assert init_env.returncode == 0 and token not in init_env.stdout
        assert not in_vm(sid, "/run/cave/token")
        for user in ("root", "claude"):  # the mount is read-only for root too
            write = ["container", "exec", "--user", user, sid, "sh", "-c", "echo x > /cave/inbox/planted"]
            assert subprocess.run(write, capture_output=True).returncode != 0
        stubbed_vm.cave("stop", sid)
        process.wait(timeout=30)
        stored = Path.home() / "Library/Application Support/com.apple.container/containers" / sid
        assert (stored / "config.json").is_file()
        inspected = subprocess.run(["container", "inspect", sid], capture_output=True, text=True)
        assert sid in inspected.stdout and token not in inspected.stdout
        config = [path for path in stored.rglob("*") if path.is_file() and path.stat().st_size < 1 << 20]  # no disks
        assert stored / "config.json" in config and not [path for path in config if token.encode() in path.read_bytes()]


def test_nested_repo_stays_separate_from_its_parent(stubbed_vm):
    nested = stubbed_vm.add_repo(stubbed_vm.repo / "nested")
    run = stubbed_vm.cave("run", "--", "-p", "hello", ok=False)
    assert run.returncode == 7, run.stderr
    sid = stubbed_vm.boxes()[-1]
    stubbed_vm.cave("merge", sid)
    assert (nested / "new.txt").read_text() == "nested\n"
    assert stubbed_vm.git("log", "-1", "--format=%s", repo=nested) == "WIP"
    assert "nested" not in stubbed_vm.git("ls-tree", "-r", "--name-only", "main")


def test_launch_deletes_an_old_image_for_real_but_never_the_users(real_vm):
    users = f"cave:20200102-{os.urandom(6).hex()}"  # as a stopped sandbox out of this test's sight might need
    for old in (PRUNABLE_IMAGE, users):
        subprocess.run(["container", "image", "tag", "cave-base:latest", old], check=True)
    try:
        real_vm.cave("run", "--", "--version")
        images = subprocess.run(["container", "image", "ls", "--quiet"], capture_output=True, text=True).stdout.split()
        assert PRUNABLE_IMAGE not in images and users in images
    finally:
        for old in (PRUNABLE_IMAGE, users):
            subprocess.run(["container", "image", "rm", old], capture_output=True)


def test_the_copy_out_recipe_copies_a_hostile_volume_without_running_or_following_it(real_vm):
    sid = f"20200101-000000-{os.urandom(2).hex()}"
    volume, image = volume_name(sid), "cave-base:latest"
    run = lambda *args: subprocess.run(["container", *args], check=False, capture_output=True, text=True)
    fill = ("mkdir -p /workspace/repo/.git/hooks /workspace/stray && echo kept > /workspace/stray/f.txt "
            "&& ln -s /etc/passwd /workspace/stray/link && ln -s ../../secret /workspace/repo/rel "
            "&& printf '#!/bin/sh\\ntouch /workspace/EXECUTED\\n' > /workspace/repo/.git/hooks/post-checkout "
            "&& chmod +x /workspace/repo/.git/hooks/post-checkout && mkfifo /workspace/stray/fifo "
            "&& cp /bin/sh /workspace/suid && chmod 6755 /workspace/suid && mkdir /workspace/stray/empty "
            "&& { mknod /workspace/stray/dev c 1 3 || true; }")
    try:
        assert run("volume", "create", volume).returncode == 0
        assert run("run", "--rm", "--user", "root", "--entrypoint", "sh", "-v", f"{volume}:/workspace",
                   image, "-c", fill).returncode == 0
        command = sandbox.copy_out_command(sid, {"image": image})
        home = real_vm.root / "h,ome"  # a comma would split a --mount
        home.mkdir()
        copied = subprocess.run(["sh", "-c", command], capture_output=True, text=True,
                                env={**os.environ, "HOME": str(home)})
        assert copied.returncode == 0, copied.stderr
        out = home / "cave-rescue" / sid
        assert (out / "stray/f.txt").read_text() == "kept\n"
        assert os.readlink(out / "stray/link") == "/etc/passwd"  # copied as a link, not followed
        assert os.readlink(out / "repo/rel") == "../../secret"
        assert (out / "repo/.git/hooks/post-checkout").is_file()
        assert (out / "stray/empty").is_dir()
        kinds = {path: path.lstat().st_mode for path in out.rglob("*")}
        assert all(stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode) for mode in kinds.values()), kinds
        assert not (out / "stray/fifo").exists() and not (out / "stray/dev").exists()
        assert (out / "suid").is_file() and not kinds[out / "suid"] & (stat.S_ISUID | stat.S_ISGID)
        mounts = run("run", "--rm", "--entrypoint", "grep", "-v", f"{volume}:/workspace:ro", image, "/workspace", "/proc/mounts")
        assert " ro," in mounts.stdout, mounts.stdout  # the syntax the recipe uses really is read-only
        after = run("run", "--rm", "--user", "root", "--entrypoint", "test", "-v", f"{volume}:/workspace",
                    image, "-e", "/workspace/EXECUTED")
        assert after.returncode != 0  # nothing from the volume ran
    finally:
        run("volume", "delete", volume)


def test_prune_deletes_an_orphan_for_real_and_only_its_own(real_vm, monkeypatch):
    sid, other = (f"20200101-000000-{os.urandom(2).hex()}" for _ in "ab")
    monkeypatch.setenv("HOME", real_vm.env["HOME"])
    run = lambda *args: subprocess.run(["container", *args], check=False, capture_output=True, text=True)
    try:
        assert run("volume", "create", "--label", f"cave={state_key()}", f"cave-{sid}").returncode == 0
        assert run("volume", "create", "--label", "cave=1", f"cave-{other}").returncode == 0
        assert run("create", "--name", sid, "-v", f"cave-{sid}:/x", "cave-base:latest").returncode == 0
        real_vm.cave_dir.mkdir(parents=True)
        real_vm.cave("prune")
        assert sid not in run("ls", "--all", "--quiet").stdout.split()
        volumes = run("volume", "ls", "--quiet").stdout.split()
        assert f"cave-{sid}" not in volumes and f"cave-{other}" in volumes
    finally:
        run("delete", "--force", sid)
        run("volume", "delete", f"cave-{sid}")
        run("volume", "delete", f"cave-{other}")  # alone: a missing name stops the rest of a list


def test_killed_vm_recovers_and_lands(stubbed_vm):
    with session(stubbed_vm, "locked") as (process, sid):
        subprocess.run(["container", "kill", sid], capture_output=True, check=True)
        process.wait(timeout=30)
    listing = stubbed_vm.cave("ls").stdout
    assert " crashed " in listing, listing
    merge = stubbed_vm.cave("merge", sid)  # past a stale index.lock
    assert "recovery VM" in merge.stdout and "reported a failure" not in merge.stderr, merge.stderr
    assert (stubbed_vm.repo / "live.txt").read_text() == "live\n", merge.stdout + merge.stderr
    assert stubbed_vm.boxes() == [], merge.stdout + merge.stderr


def test_a_conflict_is_resolved_in_a_new_vm_on_the_same_volume(stubbed_vm):
    stubbed_vm.cave("run", ok=False)
    sid = stubbed_vm.boxes()[-1]
    stubbed_vm.host_commit_conflicting_with_sandbox()
    assert "rebase conflict" in stubbed_vm.cave("merge", sid, ok=False).stderr
    stubbed_vm.cave("resolve", sid, ok=False)
    stubbed_vm.cave("merge", sid)
    assert (stubbed_vm.repo / "file.txt").read_text() == "resolved\n"
    assert (stubbed_vm.repo / "new.txt").read_text() == "leftover\n"
    assert stubbed_vm.boxes() == []


def as_claude(sid, command, stdin=None):
    return subprocess.run(["container", "exec", "-i", "--user", "claude", "--env", "HOME=/home/claude", sid,
                           "bash", "-c", command], capture_output=True, text=True, input=stdin)


def origin_main(sid):
    return as_claude(sid, "git -C /workspace/project rev-parse refs/remotes/origin/main").stdout.strip()


def test_a_conflict_is_resolved_live_with_the_watcher_and_hooks_doing_their_part(stubbed_vm):
    with session(stubbed_vm, "hang") as (process, sid):
        commit = "cd /workspace/project && echo theirs > file.txt && git commit -qam sandbox"
        assert as_claude(sid, commit).returncode == 0
        stubbed_vm.host_commit_conflicting_with_sandbox()
        assert "rebase conflict" in stubbed_vm.cave("merge", sid, ok=False).stderr
        assert "delivered the current main" in stubbed_vm.cave("resolve", sid).stdout
        tip = stubbed_vm.git("rev-parse", "main")
        wait_until(lambda: origin_main(sid) == tip)
        hook = "cave-hook UserPromptSubmit"
        assert "could not land" in as_claude(sid, hook, "{}").stdout
        assert as_claude(sid, hook, "{}").stdout == ""
        rebase = ("cd /workspace/project && git rebase origin/main > /dev/null 2>&1; echo resolved > file.txt; "
                  "git add -A && GIT_EDITOR=true git rebase --continue > /dev/null")
        assert as_claude(sid, rebase).returncode == 0
        stubbed_vm.git("commit", "-q", "--allow-empty", "-m", "main moved after the resolve")
        stubbed_vm.cave("merge", sid)
        assert stubbed_vm.git("log", "--format=%s", "main").split("\n") == [
            "sandbox", "main moved after the resolve", "host", "first"]
        assert (stubbed_vm.repo / "file.txt").read_text() == "resolved\n"
        stubbed_vm.cave("discard", sid)
        process.wait(timeout=30)


def test_the_watcher_takes_in_an_update_and_goes_when_the_session_does(stubbed_vm):
    with session(stubbed_vm, "hang") as (process, sid):
        stubbed_vm.git("commit", "-q", "--allow-empty", "-m", "by hand")
        stubbed_vm.cave("ls")
        tip = stubbed_vm.git("rev-parse", "main")
        wait_until(lambda: origin_main(sid) == tip)
        assert "main moved" in as_claude(sid, "cave-hook PostToolUse", "{}").stdout
        assert as_claude(sid, "git -C /workspace/project rev-parse --abbrev-ref HEAD").stdout.strip() == f"claude/{sid}"
        assert as_claude(sid, "pgrep -f 'cave-companion watch'").stdout.strip()
        assert stop_timed(stubbed_vm, sid) < 18  # well under the 20s that `container stop` allows
        process.wait(timeout=30)
    assert " stopped " in stubbed_vm.cave("ls").stdout


def test_a_repo_joins_a_running_vm_and_lands(stubbed_vm):
    with session(stubbed_vm, "hang") as (process, sid):
        added = stubbed_vm.add_repo(stubbed_vm.root / "elsewhere")
        stubbed_vm.add_repo(stubbed_vm.repo / "sub")
        assert as_claude(sid, "cd /workspace/project && cave-companion request add-repo sub").returncode == 0
        output = stubbed_vm.on_terminal("requests", sid, answer="y\n")
        assert "added sub" in output, output
        commit = "cd {} && git branch --show-current && echo new > new.txt && git add -A && git commit -qm work"
        assert sid in as_claude(sid, commit.format("/workspace/project/sub")).stdout
        placed = stubbed_vm.cave("add", str(added), sid).stdout.split(" at ")[-1].strip()
        assert sid in as_claude(sid, commit.format(placed)).stdout
        assert as_claude(sid, "cave-hook UserPromptSubmit", "{}").stdout.count("repo was added") == 1
        wait_until(lambda: stubbed_vm.cave("ls").stdout.splitlines()[1].split()[2:4] == ["working", "1"])  # live.txt
        stubbed_vm.cave("merge", sid)
        assert stubbed_vm.git("show", "main:new.txt", repo=added) == "new"
        assert stubbed_vm.git("show", "main:new.txt", repo=stubbed_vm.repo / "sub") == "new"
        assert "sub" not in as_claude(sid, "git -C /workspace/project status --porcelain").stdout
        assert not (stubbed_vm.repo / "live.txt").exists()  # a running Claude's uncommitted work isn't taken
        assert " running " in stubbed_vm.cave("ls").stdout
        assert "nothing new" in stubbed_vm.cave("merge", sid).stdout
        stubbed_vm.cave("discard", sid)
        process.wait(timeout=30)
    assert sid not in subprocess.run(["container", "ls", "--quiet"], capture_output=True, text=True).stdout


def stopped_and_committed(harness, sid):
    """After a stop: the sandbox is stopped, and the final commit has what Claude left."""
    assert " stopped " in harness.cave("ls").stdout
    harness.cave("merge", sid)
    assert (harness.repo / "live.txt").read_text() == "live\n"


def plain_stop_timed(harness, sid):
    started = time.time()
    subprocess.run(["container", "stop", sid], capture_output=True, check=True)
    return time.time() - started


def run_until_stopped(harness, mode, stop=stop_timed):
    """Start a stub that won't exit on SIGTERM, stop it, and return the seconds taken."""
    with session(harness, mode) as (process, sid):
        elapsed = stop(harness, sid)
        process.wait(timeout=30)
    return sid, elapsed


def test_stop_kills_a_stuck_claude_after_5s_and_commits(stubbed_vm):
    sid, elapsed = run_until_stopped(stubbed_vm, "stubborn")
    assert 5 <= elapsed < 18  # killed at 5s, not left for the 20s limit
    stopped_and_committed(stubbed_vm, sid)  # the snapshot behind this would fail on a stale index.lock
    assert (stubbed_vm.repo / "background.txt").exists()


def test_plain_stop_is_quick_and_still_commits(stubbed_vm):
    sid, elapsed = run_until_stopped(stubbed_vm, "stubborn", plain_stop_timed)
    assert elapsed < 8  # the commit below is what proves it fit inside the 5s that `container stop` gives
    stopped_and_committed(stubbed_vm, sid)


def test_cave_stop_works_after_the_terminal_closed(stubbed_vm):
    with session(stubbed_vm, "hang") as (process, sid):
        process.kill()
        process.wait(timeout=30)
        assert " running " in stubbed_vm.cave("ls").stdout
        stubbed_vm.cave("stop", sid)
    stopped_and_committed(stubbed_vm, sid)


def test_stop_allows_time_for_slow_git(stubbed_vm):
    sid, elapsed = run_until_stopped(stubbed_vm, "slow")
    assert elapsed > 10  # past `container stop`'s default of 5s, so 20s is really in force
    assert elapsed < 25
    stopped_and_committed(stubbed_vm, sid)


def test_piped_stdin_reaches_claude(stubbed_vm):
    run = subprocess.run([sys.executable, "-m", "cave", "run", "--", "stdin"], cwd=str(stubbed_vm.repo),
                         env={**os.environ, **stubbed_vm.env}, input="from the pipe\n",
                         capture_output=True, text=True)
    assert "from the pipe" in run.stdout, run.stderr


def test_real_claude_takes_keyboard_input_and_exits_cleanly(real_vm):
    assert exit_interactive_session(real_vm)
    assert " stopped " in real_vm.cave("ls").stdout


def test_real_claude_starts_interactively_without_prompts(real_vm):
    text = interactive_screen(real_vm, until="bypass permissions")
    assert "no token stored" in text
    assert "bypass permissions on" in text
    assert "trust" not in text.lower()
