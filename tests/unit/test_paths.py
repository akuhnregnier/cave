from datetime import datetime, timedelta

import pytest

from cave import paths
from cave.errors import CaveError


def test_new_id_is_a_valid_id():
    assert paths.is_id(paths.new_id())


def test_new_ids_differ_within_a_second():
    assert len({paths.new_id() for _ in range(20)}) > 1


@pytest.mark.parametrize("bad", ["..", "../victim", "20260101-000000-abcd/../..", "20260101-000000-ABCD", ""])
def test_box_path_rejects_anything_but_an_id(bad):
    with pytest.raises(CaveError, match="not a sandbox id"):
        paths.box_path(bad)


def test_box_path_is_in_the_state_dir_not_a_purgeable_one(tmp_path):
    box = paths.box_path("20260101-000000-abcd")
    assert box == tmp_path / "home/Library/Application Support/cave/sandboxes/20260101-000000-abcd"


@pytest.mark.parametrize("days,old", [(29, False), (31, True)])
def test_is_old(days, old):
    now = datetime(2026, 6, 1, 12)
    sid = f"{now - timedelta(days=days):%Y%m%d-%H%M%S}-abcd"
    assert paths.is_old(sid, now) is old


def test_config_repo_defaults_under_home(monkeypatch, tmp_path):
    monkeypatch.delenv("CAVE_CONFIG")
    assert paths.config_repo() == tmp_path / "home/.config/cave/config"
