from datetime import datetime, timedelta

import pytest

from cave import container, image, launch, sandbox
from cave._testing.repos import SID
from cave.errors import CaveError


DIGEST = image.image_hash("")


def stored_images(monkeypatch, *names):
    images = list(names)
    deleted = []
    monkeypatch.setattr(container, "image_names", lambda: images)
    monkeypatch.setattr(container, "images_in_use", lambda: set())
    monkeypatch.setattr(container, "delete_image", lambda name: deleted.append(name) or True)
    return deleted


def days_ago(days):
    return f"{datetime.now() - timedelta(days=days):%Y%m%d}"


def test_hash_follows_the_user_dockerfile():
    assert image.image_hash("") != image.image_hash("FROM cave-base:latest\n")
    assert image.image_hash("x") == image.image_hash("x")


def test_new_tag_has_todays_date_and_the_hash(image_builds):
    assert image.ensure() == f"cave:{datetime.now():%Y%m%d}-{DIGEST}"


def test_an_image_with_the_same_hash_is_reused_whatever_its_date(image_builds, monkeypatch):
    old = f"cave:20200101-{DIGEST}"
    stored_images(monkeypatch, old, "cave:20200101-000000000000")
    assert image.ensure() == old
    assert image_builds == []


def test_the_newest_of_several_matching_images_wins(image_builds, monkeypatch):
    stored_images(monkeypatch, f"cave:20200101-{DIGEST}", f"cave:20210101-{DIGEST}")
    assert image.ensure() == f"cave:20210101-{DIGEST}"


def test_force_rebuilds_an_existing_image(image_builds, monkeypatch):
    stored_images(monkeypatch, f"cave:20200101-{DIGEST}")
    image.ensure(force=True, no_cache=True)
    assert image_builds[0]["args"][0] == "--no-cache"


OLD, RECENT = f"cave:{days_ago(8)}-{DIGEST}", f"cave:{days_ago(6)}-{DIGEST}"
NOT_OURS = ["cave-base:latest", "cave:7fd8616d1d88", "alpine:3.20", f"cave:99999999-{DIGEST}", "claude-sandbox:latest"]


@pytest.mark.parametrize("names, keep, in_use, deleted", [
    ([OLD, RECENT], {"other"}, set(), [OLD]),  # only those over seven days old
    (NOT_OURS, {"other"}, set(), []),  # anything that is not a dated cave image stays
    ([OLD], {OLD}, set(), []),  # the image this launch uses
    ([OLD], {"other"}, {OLD}, []),  # the images running VMs use
])
def test_prune_deletes_only_old_images_that_nothing_needs(monkeypatch, names, keep, in_use, deleted):
    gone = stored_images(monkeypatch, *names)
    monkeypatch.setattr(container, "images_in_use", lambda: in_use)
    image.prune(keep=keep)
    assert gone == deleted


def test_launch_keeps_the_image_each_sandbox_started_from(box, monkeypatch):
    sandbox.write_meta(SID, dict(sandbox.load_meta(SID), image="cave:20200101-aaaaaaaaaaaa"))
    kept = []
    monkeypatch.setattr(image, "prune", kept.append)
    launch._prune_images("cave:20260101-bbbbbbbbbbbb")
    assert kept == [{"cave:20200101-aaaaaaaaaaaa", "cave:20260101-bbbbbbbbbbbb"}]


def test_a_sandbox_whose_meta_cannot_be_read_stops_image_prune(box, monkeypatch):
    (box / "meta.json").write_text("{")
    kept = []
    monkeypatch.setattr(image, "prune", kept.append)
    launch._prune_images("cave:20260101-bbbbbbbbbbbb")
    assert kept == []


def test_prune_deletes_nothing_if_running_vms_are_unknown(monkeypatch):
    deleted = stored_images(monkeypatch, f"cave:{days_ago(30)}-{DIGEST}")

    def unknown():
        raise CaveError("could not read the container listing")

    monkeypatch.setattr(container, "images_in_use", unknown)
    with pytest.raises(CaveError):
        image.prune(keep={"other"})
    assert deleted == []


def test_prune_reports_an_image_it_could_not_delete(monkeypatch, capsys):
    stored_images(monkeypatch, f"cave:{days_ago(30)}-{DIGEST}")
    monkeypatch.setattr(container, "delete_image", lambda name: False)
    image.prune(keep={"other"})
    assert "could not delete old image" in capsys.readouterr().err


def test_base_context_holds_the_assets_outside_the_temp_dir(image_builds, tmp_path):
    image.ensure()
    assert image_builds[0]["files"] == ["Dockerfile", "cave-companion", "cave-hook", "cave-init", "entrypoint.sh", "managed-settings.json"]
    assert str(image_builds[0]["args"][-1]).startswith(str(tmp_path / "home/Library/Caches"))


def test_without_a_user_layer_the_base_is_tagged(image_builds):
    tag = image.ensure()
    assert image_builds[-1] == {"tag": (image.BASE_TAG, tag)}


def test_user_layer_builds_from_the_config_repo(image_builds, tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "Dockerfile").write_text("FROM cave-base:latest\n")
    tag = image.ensure()
    assert image_builds[-1]["args"] == ("-t", tag, "-f", str(config / "Dockerfile"), str(config))
