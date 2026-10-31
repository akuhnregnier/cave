"""The long-lived Claude token in cave's own Keychain item.

Never the host's `Claude Code-credentials` item: refresh tokens rotate, so a
sandbox refresh would invalidate the host login and other sandboxes.
"""
import getpass
import re
import subprocess

from cave.errors import CaveError

KEYCHAIN_SERVICE = "cave-claude-token"


def read_token():
    result = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def store_token(token):
    if not re.fullmatch(r"[\w.\-]+", token):
        raise CaveError("that doesn't look like a token")
    # Goes through stdin so that the token never appears in a process's argv.
    subprocess.run(
        ["security", "-i"], check=True, text=True, stdout=subprocess.DEVNULL,
        input=f'add-generic-password -U -a cave -s {KEYCHAIN_SERVICE} -w "{token}"\n',
    )


def setup():
    subprocess.run(["claude", "setup-token"], check=True)
    store_token(getpass.getpass("Paste the token: ").strip())
    print(f"cave: token stored in Keychain item {KEYCHAIN_SERVICE}")
