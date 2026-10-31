"""The base image plus the user's Dockerfile layer."""
import hashlib
import re
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from cave import container
from cave.log import warn
from cave.paths import config_repo

IMAGE_ASSETS = Path(__file__).parent / "assets" / "image"
BASE_TAG = "cave-base:latest"
TAG_RE = re.compile(r"cave:(\d{8})-([0-9a-f]{12})")
MAX_IMAGE_AGE = timedelta(days=7)


def image_hash(user_dockerfile):
    digest = hashlib.sha256()
    for path in sorted(IMAGE_ASSETS.iterdir()):
        digest.update(path.read_bytes())
    digest.update(user_dockerfile.encode())
    return digest.hexdigest()[:12]


def _parse_tag(name):
    """(build date, hash) of a cave image tag, or None for anything else."""
    match = TAG_RE.fullmatch(name)
    try:
        return (datetime.strptime(match.group(1), "%Y%m%d"), match.group(2)) if match else None
    except ValueError:
        return None


def _existing_tag(digest):
    """The newest image built from this exact content, if there is one."""
    parsed = [(name, _parse_tag(name)) for name in container.image_names()]
    return max((name for name, tag in parsed if tag and tag[1] == digest), default=None)


def ensure(force=False, no_cache=False):
    user_file = config_repo() / "Dockerfile"
    user_dockerfile = user_file.read_text() if user_file.is_file() else ""
    digest = image_hash(user_dockerfile)
    existing = _existing_tag(digest)
    if existing and not force:
        return existing
    tag = f"cave:{datetime.now():%Y%m%d}-{digest}"
    flags = ["--no-cache"] if no_cache else []
    # Not the temp dir: `container build` sends an empty context for any path under /private.
    caches = Path.home() / "Library/Caches"
    caches.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=caches, prefix="cave-build-") as context:
        for path in IMAGE_ASSETS.iterdir():
            shutil.copy(path, context)
        container.build(*flags, "-t", BASE_TAG, context)
    if user_dockerfile:
        container.build(*flags, "-t", tag, "-f", str(user_file), str(config_repo()))
    else:
        container.tag(BASE_TAG, tag)
    return tag


def prune(keep):
    """Delete images built over MAX_IMAGE_AGE ago, except those in keep and any a running VM uses."""
    in_use = container.images_in_use()  # raises if it can't tell what's running, so nothing is deleted
    now = datetime.now()
    for name in container.image_names():
        tag = _parse_tag(name)
        if tag and name not in keep and name not in in_use and now - tag[0] > MAX_IMAGE_AGE:
            if not container.delete_image(name):
                warn(f"could not delete old image {name}")
