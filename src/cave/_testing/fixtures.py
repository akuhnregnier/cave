"""pytest fixtures, loaded with `-p cave._testing.fixtures`."""
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from cave import auth, container, launch
from cave._testing.harness import COMPANION, FAKE_CONTAINER, NO_TOKEN, Harness, base_env, write_exe
from cave._testing.repos import SID, make_box, make_clone, make_repo, run_companion
from cave.paths import network_name, volume_name
from cave._testing.vm import (
    build_once,
    image_guard,
    install_stub_claude,
    max_workers,
    require_token_approval,
    vm_available,
    vm_slot,
)


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    return max_workers()


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    for name in ("home", "tmpdir"):
        (tmp_path / name).mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmpdir"))
    monkeypatch.setenv("CAVE_CONFIG", str(tmp_path / "config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@example.com")


@pytest.fixture
def repo(isolated_env, tmp_path):
    return make_repo(tmp_path / "project")


@pytest.fixture
def box(repo):
    return make_box(SID, repo)


@pytest.fixture
def stopped_sandbox(box, repo):
    """A cleanly stopped sandbox with one commit on claude/<id>. Returns its clone."""
    shutil.rmtree(box / "code")
    path = make_clone(repo, box / "code", SID)
    run_companion(SID, "--final", {"code": path})
    return path


@pytest.fixture
def active(monkeypatch):
    """The set of running sandbox ids, with no real `container` involved."""
    ids = set()
    monkeypatch.setattr(container, "running", lambda *args, **kwargs: ids)
    monkeypatch.setattr(container, "force_delete", lambda name: ids.discard(name))
    return ids


@pytest.fixture
def container_calls(monkeypatch):
    log = []

    def fake(*args, **kwargs):
        log.append(args)
        return SimpleNamespace(returncode=fake.returncode, stdout=fake.stdout, stderr=fake.stderr)

    fake.returncode, fake.stdout, fake.stderr = 0, "", ""
    monkeypatch.setattr(container, "container", fake)
    return SimpleNamespace(log=log, fake=fake)


@pytest.fixture
def image_builds(monkeypatch):
    log = []

    def build(*args):
        context = Path(args[-1])
        log.append({"args": args, "files": sorted(p.name for p in context.iterdir())})

    monkeypatch.setattr(container, "build", build)
    monkeypatch.setattr(container, "image_names", lambda: [])
    monkeypatch.setattr(container, "tag", lambda source, target: log.append({"tag": (source, target)}))
    return log


@pytest.fixture
def security(monkeypatch):
    calls = []

    def fake(args, **kwargs):
        calls.append(SimpleNamespace(args=args, input=kwargs.get("input")))
        return SimpleNamespace(returncode=fake.returncode, stdout=fake.stdout)

    fake.returncode, fake.stdout = 0, "tok\n"
    monkeypatch.setattr(subprocess, "run", fake)
    return SimpleNamespace(calls=calls, fake=fake)


@pytest.fixture
def launched(monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "run", lambda args, label=None: calls.append((args, label)) or 7)
    return calls


@pytest.fixture
def fake_vm(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    write_exe(bin_dir / "container", FAKE_CONTAINER.read_text())
    write_exe(bin_dir / "security", NO_TOKEN)
    env = base_env(tmp_path, bin_dir)
    env.update(FAKE_LOG=str(tmp_path / "container.log"), FAKE_RUNNING=str(tmp_path / "running"), FAKE_IMAGES=str(tmp_path / "images"),
               CAVE_VM_HOME=str(tmp_path / "vm-home"), CAVE_COMPANION=str(COMPANION))
    harness = Harness(tmp_path, env)
    harness.log = tmp_path / "container.log"
    harness.running = tmp_path / "running"
    return harness


@pytest.fixture
def real_vm():
    if not vm_available():
        pytest.skip("Apple container is not running")
    with vm_slot():
        # Under the real home, not pytest's tmp_path: `container build` sends an
        # empty context for any path under /private.
        root = Path(tempfile.mkdtemp(prefix="cave-test-", dir=Path.home() / "Library/Caches"))
        bin_dir = root / "bin"
        bin_dir.mkdir()
        write_exe(bin_dir / "security", NO_TOKEN)  # never use a real stored token
        write_exe(bin_dir / "container", image_guard())
        env = base_env(root, bin_dir)
        env.update(CAVE_CPUS="2", CAVE_MEMORY="2G")
        harness = Harness(root, env)
        build_once(harness, "")
        yield harness
        for sid in harness.boxes():  # stopped containers and their volumes stay until deleted
            container.force_delete(sid)
            container.delete_volume(volume_name(sid))
            # Not container.delete_network, which goes by this worker's label and the sandbox has the test's.
            subprocess.run(["container", "network", "delete", network_name(sid)], capture_output=True, timeout=30)
        subprocess.run(["chmod", "-R", "u+rwx", str(root)], check=False)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def claude_token():
    """The real token, read before any fixture replaces HOME. Fails without approval."""
    require_token_approval()
    token = auth.read_token()
    if not token:
        pytest.fail("no token stored; run `cave token`")
    return token


@pytest.fixture
def claude_vm(claude_token, real_vm):
    """A real VM that runs Claude logged in. The token reaches it the way a stored one would."""
    bin_dir = Path(real_vm.env["PATH"].split(":")[0])
    write_exe(bin_dir / "security", '#!/bin/bash\necho "$CAVE_TEST_TOKEN"\n')
    real_vm.env["CAVE_TEST_TOKEN"] = claude_token
    return real_vm


@pytest.fixture
def stubbed_vm(real_vm):
    install_stub_claude(real_vm)  # its image stays for other workers, until a launch outside tests prunes it
    return real_vm
