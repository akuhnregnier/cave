"""Where cave keeps things, and the sandbox id format."""
import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from cave.errors import CaveError

MAX_AGE = timedelta(days=30)
ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{4}")


def state_dir():
    return Path.home() / "Library/Application Support/cave"


def state_key():
    """Labels what's this state dir's in `container`, which every HOME shares, tests' included."""
    return hashlib.sha256(str(state_dir()).encode()).hexdigest()[:12]


def sandboxes_dir():
    return state_dir() / "sandboxes"


def sessions_dir():
    return state_dir() / "sessions"


def config_repo():
    return Path(os.environ.get("CAVE_CONFIG") or Path.home() / ".config/cave/config")


def new_id():
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def is_id(text):
    return ID_RE.fullmatch(text) is not None


def expiry(sid):
    """When a sandbox's MAX_AGE is up, or None if its id is no real date, so not one of ours."""
    try:
        return datetime.strptime(sid[:15], "%Y%m%d-%H%M%S") + MAX_AGE
    except ValueError:
        return None


def is_old(sid, now):
    expires = expiry(sid)
    return expires is not None and now > expires


def box_path(sid):
    if not is_id(sid):  # sid becomes a path that gets deleted
        raise CaveError(f"not a sandbox id: {sid}")
    return sandboxes_dir() / sid


def volume_name(sid):
    """The named volume that holds a sandbox's clones."""
    return f"cave-{box_path(sid).name}"


def network_name(sid):
    """The network of one sandbox, so that its VM can't reach another's."""
    return f"cave-{box_path(sid).name}"
