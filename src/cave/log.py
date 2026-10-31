"""Output. Text can come from a VM, so nothing cave prints reaches the terminal as it came.

The one exception is a running session: container run hands the terminal to the VM, so its output is raw."""
import re
import sys
import threading
import unicodedata

SUSPECT = re.compile(r"[^\t\n\x20-\x7e]")  # only these can need escaping, so plain text stays fast
SUSPECT_IN_A_LINE = re.compile(r"[^\x20-\x7e]")
INVISIBLE = "bidi or invisible characters"
FINDINGS = {"Cc": "control characters", "Cf": INVISIBLE, "Cs": "invalid UTF-8"}
# Blank or line-breaking, whatever their category says. Variation selectors stay: emoji need them.
ALSO_INVISIBLE = frozenset("\u034f\u115f\u1160\u3164\uffa0\u2028\u2029")
_muted = set()


def _visible(char, found):
    finding = INVISIBLE if char in ALSO_INVISIBLE else FINDINGS.get(unicodedata.category(char))
    if finding is None:
        return char
    found.add(finding)
    if "\udc80" <= char <= "\udcff":  # surrogateescape's stand-in for an undecodable byte
        return f"\\x{ord(char) - 0xdc00:02x}"
    return char.encode("unicode_escape").decode()


def _shorten(line, limit):
    hidden = len(line) - limit
    return f"{line[:limit]} [cave: {hidden} more characters not shown]" if hidden > 0 else line


def escape(data, max_line=None, one_line=False):
    """Render whatever could act on a terminal or mislead a reader as visible text.

    Takes str or bytes, keeps tabs and newlines, and returns (text, findings). Lines are
    cut after escaping, so what lies past the cut still counts towards the findings.
    one_line is for a field such as a filename, where a tab or newline could forge a line.
    """
    text = data.decode("utf-8", "surrogateescape") if isinstance(data, bytes) else data
    found = set()
    text = (SUSPECT_IN_A_LINE if one_line else SUSPECT).sub(lambda match: _visible(match.group(), found), text)
    if max_line is not None:
        text = "\n".join(_shorten(line, max_line) for line in text.split("\n"))
    return text, sorted(found)


def warning_line(findings):
    """The line to show beside escaped text, or '' if escape() found nothing."""
    return f"warning: escaped {', '.join(findings)} (shown as \\x.. or \\u..)" if findings else ""


def clean(text):
    return escape(text)[0]


def mute(thread):
    """Nothing more from this thread reaches the terminal, for one that outlived its turn there."""
    _muted.add(thread)


def muted():
    return threading.current_thread() in _muted


def say(message):
    if threading.current_thread() not in _muted:
        print(clean(f"cave: {message}"), flush=True)


def warn(message):
    if threading.current_thread() not in _muted:
        print(clean(f"cave: {message}"), file=sys.stderr, flush=True)
