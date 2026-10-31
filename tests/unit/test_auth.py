import pytest

from cave import auth
from cave.errors import CaveError


def test_read_token(security):
    assert auth.read_token() == "tok"
    assert security.calls[0].args[:3] == ["security", "find-generic-password", "-s"]
    assert "cave-claude-token" in security.calls[0].args


def test_no_stored_token_is_empty(security):
    security.fake.returncode = 44
    assert auth.read_token() == ""


def test_store_token_keeps_it_out_of_argv(security):
    auth.store_token("sk-ant-oat01-abc_DEF-1.2")
    assert security.calls[0].args == ["security", "-i"]
    assert "sk-ant-oat01-abc_DEF-1.2" in security.calls[0].input
    assert " -U " in security.calls[0].input


@pytest.mark.parametrize("bad", ["", 'a"; delete-keychain', "a b", "a\nb"])
def test_store_token_rejects_anything_that_could_break_the_command(security, bad):
    with pytest.raises(CaveError):
        auth.store_token(bad)
    assert security.calls == []
