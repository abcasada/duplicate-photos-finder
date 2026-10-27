"""Filename normalisation for mode 1: names that differ only cosmetically share a key.

`match_key` strips the suffixes and prefixes that copying adds ("DSC01234 (1)",
"IMG_0001 - Copy", "Copy of X", "PXL_…~2", "DSC00001-2", "IMG_0001 2") and, when a
renamed file still carries a camera's running file number, drops *neutral* text around
that number. The extension is always kept, so DSC01234.ARW never matches DSC01234.JPG.

Neutral text around a camera number is a date/time ("2023-08-26_DSC01234",
"20230826 DSC01234", "2023_08_26_17_16_18_DSC01234": only digits and separators, with
at least four digits) or bare separators ("_DSC1234"). Any other text is kept verbatim
in the key, so "BodyA_DSC01234" ≠ "BodyB_DSC01234" ≠ "DSC01234" and "DSC01234-Edit" ≠
"DSC01234-Edit-2", while two copies carrying the same text still match.

A key only proposes candidates. Camera counters repeat (DSC wraps every 10,000 shots and
two bodies share one numbering), so mode 1 still requires equal size and mtime, and
match_name applies two more rules within a key, based on the numbers in the copy marks
(copy_numbers):
- Different copy numbers never match: "X (1)" matches "X", "X - Copy" and "X (1)", never
  "X (2)". A Windows bulk rename of a burst gives "Vacation (1)", "Vacation (2)", … of
  equal size and time; only a name without a number can be the original of both.
- A folder holding two names of one key with different copy numbers ("X-1" and "X-2")
  is a renamed series, not copies, so that key falls back to exact names.
Unnumbered marks (" - Copy", "Copy of ") only come from copying, so "X - Copy" and
"X - Copy - Copy" beside "X" stay copies.

Deliberately left alone:
- A number after a space ("Party 2", "Beach 3"): people number distinct photos this
  way. It is only a copy mark right after a camera number (macOS "Keep Both":
  "IMG_0001 2").
- `-N`/`_N` unless it follows a camera file number ("DSC00001-2" is a copy, "Pic-2"
  is another picture), and never with three or more digits ("IMG_1234", "GX010039").
- Short or generic counters (Sony video "C0001") are only recognised as the whole
  name, never inside a longer one, so "C0017913-4C7D-…" stays as it is.
"""

from __future__ import annotations

import os
import re

_COPY_PREFIX = re.compile(r"^copy(?:\s*\(\d{1,3}\))?\s+of\s+")
_COPY_SUFFIX = re.compile(
    r"(?:\s*\(\d{1,3}\)"  # " (1)", "(2)"
    r"|\s+-\s+copy(?:\s*\(\d{1,3}\))?"  # " - Copy", " - Copy (2)"
    r"|\s+copy(?:\s+\d{1,3}|\s*\(\d{1,3}\))?"  # " copy", " copy 2"
    r"|~\d{1,3}"  # "~2", "~01"
    r")$"
)
_NUMBER_SUFFIX = re.compile(r"(?:[-_]| +)\d{1,2}$")
_SPACE = re.compile(r"\s+")
_DIGIT_RUN = re.compile(r"\d+")

# Text around a camera number that may be dropped: separators, or a date/time.
_NEUTRAL = re.compile(r"[\s._-]*|[\d\s._-]*\d[\d\s._-]*")
_NEUTRAL_MIN_DIGITS = 4

# Camera file numbers. A token must not touch another letter or digit ("_" and "-"
# separate) and must not be followed by more numbers ("IMG_2019-04-19-…" is a date).
# The second group needs a leading "_" (Adobe RGB variants), which the key drops.
_TOKEN_BODY = (
    r"(?<![^\W_])(?:"
    r"dsc\d{5}"  # Sony
    r"|dsc_\d{4}|dscn\d{4}"  # Nikon
    r"|dscf\d{4}"  # Fujifilm
    r"|img[pq]\d{4}"  # Pentax
    r"|img_\d{4}|mvi_\d{4}"  # Canon, phones
    r"|g[hxop]\d{6}|gopr\d{4}"  # GoPro
    r"|dji_\d{4}"  # DJI
    r"|p\d{7}|sam_\d{4}|sany\d{4}|pict\d{4}|hpim\d{4}"  # Panasonic, Samsung, Sanyo, …
    r")"
    r"|(?<=_)(?:dsc\d{4}|mg_\d{4}|ig[pq]\d{4})"  # _DSC1234, _MG_1234, _IGP1234
)
_TOKEN = re.compile(rf"(?:{_TOKEN_BODY})(?![^\W_])(?![-_]\d)")
_TOKEN_AT_END = re.compile(rf"(?:{_TOKEN_BODY})$")


def split_copy_marks(stem: str) -> tuple[str, tuple[str, ...]]:
    """(`stem` without copy marks, the marks removed), for a casefolded stem.

    Marks are removed repeatedly, outermost first, and returned with whitespace dropped
    ("(1)", "-copy(2)", "copyof", "~2", "-2"). A 1–2 digit "-N"/"_N"/" N" is only a copy
    mark right after a camera file number ("DSC00001-2"); after other text it usually
    numbers distinct pictures ("Pic-1", "Pic-2", "Party 2"), which can share size and
    mtime when the format is uncompressed (BMP). A stem that would vanish is kept.
    """
    marks: list[str] = []
    while True:
        before, kept = stem, len(marks)
        m = _COPY_PREFIX.search(stem)
        if m:
            marks.append(m.group())
            stem = stem[m.end():]
        m = _COPY_SUFFIX.search(stem)
        if m:
            marks.append(m.group())
            stem = stem[: m.start()]
        m = _NUMBER_SUFFIX.search(stem)
        if m and _TOKEN_AT_END.search(stem, 0, m.start()):
            marks.append(m.group())
            stem = stem[: m.start()]
        if not stem:
            stem, marks = before, marks[:kept]
        if stem == before:
            return stem, tuple(_SPACE.sub("", x) for x in marks)


def strip_copy_marks(stem: str) -> str:
    """`stem` (casefolded) without copy prefixes/suffixes; see split_copy_marks."""
    return split_copy_marks(stem)[0]


def copy_marks(name: str) -> tuple[str, ...]:
    """The copy marks of `name`, e.g. ("(1)",) for "X (1).jpg"; () for an unmarked name."""
    return split_copy_marks(os.path.splitext(name.casefold())[0])[1]


def copy_numbers(name: str) -> tuple[int, ...]:
    """The numbers in the copy marks of `name`, outermost mark first.

    (1,) for "X (1).jpg" and "X - Copy (1).jpg", (2,) for "DSC00001-2.jpg", and () for
    "X.jpg" or "X - Copy.jpg".
    """
    return tuple(int(n) for mark in copy_marks(name) for n in _DIGIT_RUN.findall(mark))


def camera_token(stem: str) -> str | None:
    """The one camera file number in `stem` (casefolded), or None if none or several."""
    m = _token_match(stem)
    return m.group() if m else None


def _token_match(stem: str) -> re.Match[str] | None:
    found = list(_TOKEN.finditer(stem))
    return found[0] if len(found) == 1 else None


def is_neutral(text: str) -> bool:
    """True for text that may surround a camera number: separators or a date/time."""
    if not _NEUTRAL.fullmatch(text):
        return False
    digits = sum(c.isdigit() for c in text)
    return digits == 0 or digits >= _NEUTRAL_MIN_DIGITS


def match_stem(name: str) -> str:
    """match_key without the extension: shared by RAW+JPG siblings and their copies."""
    stem = strip_copy_marks(os.path.splitext(name.casefold())[0])
    m = _token_match(stem)
    if m is None:
        return stem
    before, after = stem[: m.start()], stem[m.end():]
    return (
        ("" if is_neutral(before) else before) + m.group() + ("" if is_neutral(after) else after)
    )


def match_key(name: str) -> str:
    """Case-insensitive key shared by names that differ only by copy marks or renaming.

    >>> match_key("2023-08-26_DSC01234 (1).ARW")
    'dsc01234.arw'
    >>> match_key("BodyA_DSC01234.ARW")
    'bodya_dsc01234.arw'
    """
    return match_stem(name) + os.path.splitext(name.casefold())[1]
