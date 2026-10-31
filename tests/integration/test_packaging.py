import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_wheel_ships_every_asset_and_the_entry_point(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(ROOT)], check=True, capture_output=True)
    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        entry_points = next(n for n in names if n.endswith("entry_points.txt"))
        assert "cave = cave.cli:main" in archive.read(entry_points).decode()
    assets = ROOT / "src/cave/assets"
    expected = {str(p.relative_to(ROOT / "src")) for p in assets.rglob("*") if p.is_file()}
    assert expected <= names


def test_help_works_with_an_old_git(tmp_path):
    fake = tmp_path / "git"
    fake.write_text("#!/bin/bash\necho git version 2.0.0\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    shown = subprocess.run([sys.executable, "-m", "cave", "--help"], env=env, capture_output=True, text=True)
    assert shown.returncode == 0 and "cave ls" in shown.stdout
    refused = subprocess.run([sys.executable, "-m", "cave", "ls"], env=env, capture_output=True, text=True)
    assert refused.returncode == 1
    assert "2.50.1 or newer" in refused.stderr
