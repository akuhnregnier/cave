import pytest


@pytest.fixture(autouse=True)
def _isolate(isolated_env, container_calls):
    """Every unit test gets its own HOME, TMPDIR and git identity, and no real `container`."""
