import unicodedata

import pytest

from cave import log

CONTROL, INVISIBLE, INVALID = "control characters", "bidi or invisible characters", "invalid UTF-8"


@pytest.mark.parametrize("data, text, found", [
    ("a\x1b]0;PWNED\x07b", r"a\x1b]0;PWNED\x07b", CONTROL),
    ("a\r\n", r"a\r" + "\n", CONTROL),
    ("\x00\x7f", r"\x00\x7f", CONTROL),
    ("\x9b2J", r"\x9b2J", CONTROL),
    (b"\xc2\x9b2J", r"\x9b2J", CONTROL),  # C1 in its 2-byte form
    (b"\xc0\x9b2J", r"\xc0\x9b2J", INVALID),  # overlong ESC
    (b"\xff", r"\xff", INVALID),
    (b"euro \xe2\x82", r"euro \xe2\x82", INVALID),  # split multi-byte sequence
    ("\ud800", r"\ud800", INVALID),
    ("\u202e", r"\u202e", INVISIBLE),  # bidi override
    ("\u2066\u2069", r"\u2066\u2069", INVISIBLE),  # bidi isolate
    ("\u200f", r"\u200f", INVISIBLE),  # RLM
    ("a\u200b\u200c\u200d\u2060\ufeffb", r"a\u200b\u200c\u200d\u2060\ufeffb", INVISIBLE),
    ("\U000e0041", r"\U000e0041", INVISIBLE),  # tag character
    ("a\u034fb", r"a\u034fb", INVISIBLE),  # combining grapheme joiner
    ("\u115f\u1160\u3164\uffa0", r"\u115f\u1160\u3164\uffa0", INVISIBLE),  # Hangul fillers
    ("a\u2028b\u2029c", r"a\u2028b\u2029c", INVISIBLE),  # line and paragraph separators
])
def test_escape_makes_each_class_visible(data, text, found):
    assert log.escape(data) == (text, [found])


@pytest.mark.parametrize("data", ["plain\ttab\n", "é 日本語 🙂 e\u0301", "é".encode(), r"a literal \x1b",
                                  "\u2764\ufe0f \u82a6\U000e0100"])
def test_harmless_text_passes_through(data):
    text = data.decode() if isinstance(data, bytes) else data
    assert log.escape(data) == (text, [])


def test_nothing_unsafe_survives_in_any_code_point_or_byte():
    for data in ("".join(map(chr, range(0x110000))), bytes(range(256))):
        text, found = log.escape(data)
        assert len(found) >= 2
        assert not {c for c in text if c not in "\t\n" and unicodedata.category(c) in log.FINDINGS}
        assert not log.ALSO_INVISIBLE.intersection(text)


def test_every_finding_is_reported_once_and_makes_a_warning():
    findings = log.escape(b"\x1b\xff\xe2\x80\xae\x1b")[1]
    assert findings == sorted([CONTROL, INVISIBLE, INVALID])
    assert all(name in log.warning_line(findings) for name in findings)
    assert log.warning_line([]) == ""


def test_long_lines_are_cut_with_a_note_and_findings_survive_the_cut():
    text, found = log.escape("abcdef\x1b\nok\n", max_line=3)
    assert text == "abc [cave: 7 more characters not shown]\nok\n"
    assert found == [CONTROL]


def test_one_line_also_escapes_what_could_forge_a_line():
    assert log.escape("a\nb\tc", one_line=True) == (r"a\nb\tc", [CONTROL])


def test_output_helpers_escape(capfd):
    assert log.clean("x\x1b[2Jy") == r"x\x1b[2Jy"
    log.say("x\x07y")
    log.warn("x\x1b[2Jy")
    out, err = capfd.readouterr()
    assert (out, err) == ("cave: x\\x07y\n", "cave: x\\x1b[2Jy\n")
