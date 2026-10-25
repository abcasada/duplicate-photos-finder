"""Filename normalisation for mode 1: names that differ only cosmetically share a key.

`match_key` strips the suffixes and prefixes that copying adds ("DSC01234 (1)",
"IMG_0001 - Copy", "Copy of X", "PXL_…~2", "DSC00001-2") and, when a renamed file
still carries a camera's running file number ("2023-08-26_DSC01234",
"Lincoln Air Show DSC04205"), reduces the name to that number. The extension is always
kept, so DSC01234.ARW never matches DSC01234.JPG.

A key only proposes candidates. Camera counters repeat (DSC wraps every 10,000 shots and
two bodies share one numbering), so mode 1 still requires equal size and mtime.

Deliberately left alone:
- A number after a space ("Party 2", "Beach 3"): people number distinct photos this
  way. macOS "Keep Both" copies ("IMG_0001 2") still match through the camera number.
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
_NUMBER_SUFFIX = re.compile(r"[-_]\d{1,2}$")

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


def strip_copy_marks(stem: str) -> str:
    """`stem` (casefolded) without copy prefixes/suffixes, removed repeatedly.

    A 1–2 digit "-N"/"_N" is only a copy mark right after a camera file number
    ("DSC00001-2"); after other text it usually numbers distinct pictures ("Pic-1",
    "Pic-2"), which can share size and mtime when the format is uncompressed (BMP).
    """
    while True:
        before = stem
        stem = _COPY_PREFIX.sub("", stem)
        stem = _COPY_SUFFIX.sub("", stem)
        m = _NUMBER_SUFFIX.search(stem)
        if m and _TOKEN_AT_END.search(stem, 0, m.start()):
            stem = stem[: m.start()]
        if stem == before or not stem:
            return stem or before


def camera_token(stem: str) -> str | None:
    """The one camera file number in `stem` (casefolded), or None if none or several."""
    found = {m.group() for m in _TOKEN.finditer(stem)}
    return found.pop() if len(found) == 1 else None


def match_stem(name: str) -> str:
    """match_key without the extension: shared by RAW+JPG siblings and their copies."""
    stem = strip_copy_marks(os.path.splitext(name.casefold())[0])
    return camera_token(stem) or stem


def match_key(name: str) -> str:
    """Case-insensitive key shared by names that differ only by copy marks or renaming.

    >>> match_key("2023-08-26_DSC01234 (1).ARW")
    'dsc01234.arw'
    """
    return match_stem(name) + os.path.splitext(name.casefold())[1]
