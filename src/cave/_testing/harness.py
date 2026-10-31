"""Runs the real CLI in a subprocess, in an isolated environment."""
import hashlib
import os
import pty
import stat
import subprocess
import sys
from pathlib import Path

FAKE_CONTAINER = Path(__file__).parent / "fake_container.sh"
COMPANION = Path(__file__).parent.parent / "assets/image/cave-companion"
NO_TOKEN = "#!/bin/bash\nexit 44\n"


def write_exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def base_env(root, bin_dir):
    home = root / "home"
    home.mkdir()
    (root / "tmpdir").mkdir()
    (home / ".gitconfig").write_text("[user]\n\tname = t\n\temail = t@example.com\n")
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "HOME": str(home),
        "TMPDIR": str(root / "tmpdir"),
        "CAVE_CONFIG": str(root / "config"),
        "GIT_CONFIG_GLOBAL": str(home / ".gitconfig"),
    }


def values_after(args, flag):
    return [args[i + 1] for i, arg in enumerate(args) if arg == flag]


class Harness:
    def __init__(self, root, env):
        self.root = root
        self.env = env
        self.config = Path(env["CAVE_CONFIG"])
        self.cave_dir = Path(env["HOME"]) / "Library/Application Support/cave/sandboxes"
        self.repo = self.add_repo(root / "project")

    def add_repo(self, path):
        from cave._testing.repos import make_repo  # repos imports this module

        return make_repo(path, {**os.environ, **self.env})

    def git(self, *args, repo=None):
        return subprocess.run(["git", "-C", str(repo or self.repo), *args], check=True, text=True,
                              env={**os.environ, **self.env}, stdout=subprocess.PIPE).stdout.strip()

    def start(self, *args, cwd=None, **env):
        return subprocess.Popen([sys.executable, "-m", "cave", *args], cwd=str(cwd or self.repo),
                                env={**os.environ, **self.env, **env}, text=True, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def cave(self, *args, ok=True, cwd=None, approve=True, **env):
        """Run cave. A merge passes the gate with --yes unless a test is about the gate."""
        if approve and args[:1] == ("merge",):
            args += ("--yes",)
        process = self.start(*args, cwd=cwd, **env)
        stdout, stderr = process.communicate()
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        if ok:
            assert result.returncode == 0, stderr
        return result

    def on_terminal(self, *args, answer):
        """Run cave on a pty and type answer. Returns everything it printed."""
        master, slave = pty.openpty()
        process = subprocess.Popen([sys.executable, "-m", "cave", *args], cwd=str(self.repo),
                                   env={**os.environ, **self.env}, stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        os.write(master, answer.encode())
        output = b""
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:  # the pty's way of saying the process has gone
                break
            if not chunk:
                break
            output += chunk
        os.close(master)
        process.wait()
        return output.decode()

    def orphan_volume(self, sid, label=None):
        """Fake VM only: a volume of this state dir's, as though its sandbox had left no box."""
        key = hashlib.sha256(str(self.cave_dir.parent).encode()).hexdigest()[:12]
        subprocess.run(["container", "volume", "create", "--label", f"cave={label or key}", f"cave-{sid}"], check=True,
                       env={**os.environ, **self.env})
        self.cave_dir.mkdir(parents=True, exist_ok=True)
        return self.root / "volumes" / f"cave-{sid}"

    def boxes(self):
        return sorted(p.name for p in self.cave_dir.iterdir()) if self.cave_dir.exists() else []

    def box(self, sid):
        return self.cave_dir / sid

    def launch(self, *args, **env):
        self.cave("run", *args, ok=False, **env)
        return self.boxes()[-1]

    def box_running(self, sid):
        """Fake VM only: report the sandbox as running."""
        self.running.write_text(sid + "\n")

    def box_stopped(self, sid):
        """Fake VM only: stop the sandbox, which takes its final snapshot."""
        subprocess.run(["container", "stop", sid], check=True, env={**os.environ, **self.env})
        self.running.write_text("")

    def refs(self, repo=None):
        return self.git("for-each-ref", "--format=%(refname:short)", "refs/heads/", "refs/cave/", repo=repo).split()

    def held(self, sid, repo=None):
        """Is a bundle from this sandbox held in the cave namespace?"""
        return any(ref.startswith(f"cave/{sid}/") for ref in self.refs(repo))

    def host_commit_conflicting_with_sandbox(self):
        (self.repo / "file.txt").write_text("ours\n")
        self.git("commit", "-qam", "host")
