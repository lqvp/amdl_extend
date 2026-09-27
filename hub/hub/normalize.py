"""Filename normalization for dedup matching (spec §7.3, §7.5).

The filesystem is the only source of truth (§7.1): a download is skipped when the track's
*rendered filename* matches a file that already exists. `normalize()` is the single
transform applied to both sides of that comparison, which is why it has to be identical on
both sides and why it stays a pure function -- no config, no environment, no filesystem.
"""

from __future__ import annotations

import re
import unicodedata

# Measured on /run/media/m/1A5E05A75E057D2F/Music (§7.2.1), plus the siblings the Apple
# Music catalog can deliver. Deliberately excludes ".jpg" (a cover-art-only directory must
# not count as an album, §7.3 step 1b), ".lrc", and ".part" -- the library has 160 of the
# latter from interrupted downloads, and counting them as existing tracks would wrongly
# skip a re-request (Review Focus #5).
#
# Stored all-lowercase, so a bare `ext in AUDIO_EXTS` is a case-sensitive test and is
# False for ".M4A". Test membership with `is_audio_file()` rather than reaching for the
# set directly. Every extension in the real library is lowercase, so this is latent, not
# live -- but Task 3's scan walks untrusted names and should not inherit the trap.
#
# The cases that decide membership, all pinned in test_audio_exts_membership_table:
#
#   "Song.m4a"                     True   known extension
#   "Song.M4A"                     True   matched case-insensitively
#   "album/01. Track.flac"          True   a path to an audio file
#   "01. A／B.m4a"                  True   "/" is not a separator, even after NFKC
#   "01. Artist - Title"           False  no extension; the dot is part of the name
#   "1. Title"                     False  same
#   ".m4a"                         False  a leading dot is not an extension
#   ".hidden"                      False
#   "cover.jpg" / "dl.part"        False  known non-audio extensions
#   "no-extension"                 False
AUDIO_EXTS: frozenset[str] = frozenset(
    {
        ".m4a",
        ".mp4",
        ".m4b",
        ".flac",
        ".aac",
        ".ec3",
        ".ac3",
        ".wav",
        ".ogg",
        ".opus",
    }
)

# Top-level directories that group by codec rather than by artist, so the grandparent
# check in §8 step 1 must see past them. Stored casefolded because that check compares
# them against `normalize()`d path components.
FORMAT_BUCKETS: frozenset[str] = frozenset({"alac", "atmos"})

# `songNameFormat` defaults to `{disk}-{tracknum:02d} {title}` (2 numeric groups) and
# `playlistSongNameFormat` to `{playlistSongIndex:02d}. {artist} - {title}` (1), so 2 is
# enough for the real library and a third group is left alone: "1-01-02 T" -> "02 T".
#
# The separator must NOT be a greedy `[\s._-]+` run. NFKC folds "…" (U+2026) to "...", so
# a run swallows the title's own leading dots: the real file "13. …to mo da ti _.m4a"
# keyed to "to mo da ti _" and "04. ...And Then" collided with "And Then". One separator
# character with optional padding around it cannot eat two adjacent dots.
# `\d{1,3}` deliberately does not match a 4-digit year, so "1979 - Song" keeps its number
# and matches the rendered "01-1979 - Song" rather than collapsing onto an unrelated
# "Song". See §7.5 step 4.
_TRACK_PREFIX_RE = re.compile(r"^(?:\d{1,3}(?:\s*[.\-_]\s*|\s+)){1,2}")
_WHITESPACE_RE = re.compile(r"\s+")


def _audio_base(filename: str) -> str | None:
    """`filename` without its audio extension, or None when it has no audio extension.

    The single source of truth for "does this name end in a known audio extension", so
    `is_audio_file` and `stem_of` cannot drift apart.

    `pathlib` must not be used here. NFKC folds the fullwidth solidus "／" (U+FF0F) to
    "/", and `PurePath` then reads that as a path separator and keeps only the last
    component, collapsing "01. A／B.m4a" to "b" -- which collides with "01. B.m4a" and
    "01. C／B.m4a". "／" is ordinary Japanese typography for a two-part title, so treating
    "/" as structural manufactures a false skip. `rpartition` looks only at the final dot
    and leaves every other character alone.

    A leading dot is not an extension: ".m4a" has no base, so it is returned as-is rather
    than reduced to the empty string.
    """
    base, dot, ext = filename.rpartition(".")
    # `rpartition` yields the extension without its dot, while `AUDIO_EXTS` stores the
    # dot, so the comparison puts it back.
    if not dot or not base or f".{ext}".casefold() not in AUDIO_EXTS:
        return None
    return base


def is_audio_file(name: str) -> bool:
    """Whether `name` is a file this library stores audio in, judged by its extension.

    Every extension in `AUDIO_EXTS` is lowercase, so the comparison casefolds and answers
    True for "Song.M4A". Use this rather than `ext in AUDIO_EXTS`; see the note on the
    constant.

    Takes a *filename*, not a bare extension, and that is deliberate rather than a quirk:
    `is_audio_file(".m4a")` is False, because a leading dot marks a hidden file rather
    than an extension. Callers walking a directory have filenames, so the ambiguous input
    is answered conservatively -- and `stem_of(".m4a")` agrees, returning ".m4a" unchanged.
    """
    return _audio_base(name) is not None


def stem_of(filename: str) -> str:
    """Drop the extension, but only when it is one this library actually stores audio in.

    "1-01 Caribbean Blue.m4a" -> "1-01 Caribbean Blue", "Song. Pt. 2.m4a" -> "Song. Pt. 2".

    The membership test is the whole point. A "strip everything after the last dot"
    implementation cannot be used: it treats the final ".<anything>" as an extension, so
    `playlistSongNameFormat`'s default "{playlistSongIndex:02d}. {artist} - {title}"
    renders "01. Artist - Title", which collapses to "01". A library downloaded from
    playlists would then key every track to a bare index, and two tracks sharing an index
    inside one album scope would falsely match -- exactly what the album scoping in §7.3
    exists to prevent.

    Anything that is not a known audio extension is part of the name and is returned
    unchanged, so "01. Artist - Title", "1. Title" and "no-extension" all pass through.
    "/" is likewise never structural: "AC/DC - Back in Black" keeps every character.
    """
    base = _audio_base(filename)
    return filename if base is None else base


def normalize(name: str, *, strip_track_prefix: bool = True) -> str:
    """Return the comparison key for a track filename or an album directory name.

    Order is load-bearing (§7.5): **all string folding first, then structural removal.**
    Stripping first misses structure written in fullwidth -- "Song．Ｍ４Ａ" would keep
    ".m4a" inside the key, and the fullwidth track number in "０１．Artist - Title" would
    never be recognised. So: NFKC, `casefold()`, extension strip, track-prefix strip,
    whitespace squeeze, unusable-title check.

    `strip_track_prefix` exists because the two sides need different treatment. Track
    files are rendered with their number (`1-01 Title.m4a`), so the number is noise on
    both sides and stripping it is what makes a re-download match its own existing file.
    Album *directories* must keep it: `4pi` and `1st EP` are real album names, and
    stripping there unbinds the album. So the index is built with the flag off and every
    lookup uses the flag off; a mismatch between build and lookup makes every album
    lookup miss and nothing is ever skipped.

    Returns "" for an unusable title -- empty, whitespace, or punctuation only, which the
    real library contains 6 of (Review Focus #2). "" is a value that equals every other
    "", so **a caller must treat an empty key as "cannot decide" and refuse to skip**;
    never let it match. That guard lives at the dedup call site, not here: this function
    only reports the fact.
    """
    # NFKC, not NFC (§7.5 step 1). Both reconcile composed and decomposed forms of the same
    # character, which is what stops an NTFS-written track from failing to match itself.
    # NFKC additionally folds ideographic width, which matters here: a Japanese library
    # routinely holds both "ＡＢＣ Title" and "ABC Title" for what is one track, and the
    # question this function answers is "are these the same title", not "is this string
    # byte-identical to its canonical form". It also folds the fullwidth "．" and "－" that
    # a fullwidth-rendered track name would otherwise hide from the two strip steps.
    key = unicodedata.normalize("NFKC", name).casefold()
    # casefold already ran, so the suffix reaching `stem_of` is lowercase and
    # `AUDIO_EXTS` membership is case-insensitive without further folding.
    key = stem_of(key)
    if strip_track_prefix:
        # `\d{1,3}` deliberately does not match a 4-digit year, so "1979 - Song" keeps
        # its number and matches the rendered "01-1979 - Song" instead of collapsing onto
        # an unrelated "Song". See §7.5 step 4.
        key = _TRACK_PREFIX_RE.sub("", key)
    key = _WHITESPACE_RE.sub(" ", key).strip()
    # A title with no alphanumeric character carries no identifying information, so it is
    # reported as unusable rather than as a literal key. Returning "..." verbatim would
    # make every dot-only track in an album match every other one, which is the same
    # failure mode as an empty title but harder to notice.
    return key if any(char.isalnum() for char in key) else ""
