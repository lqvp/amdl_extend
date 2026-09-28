#!/usr/bin/env python3
"""Generate the golden corpus the Go port is checked against.

The port's whole claim is that it agrees with `hub/` character for character, so
the check cannot be a hand-written table of examples -- it has to be an answer
produced *by the Python implementation*, over inputs chosen to be hostile.

This script imports the real `hub.normalize`, `hub.library_scan`, `hub.dedup` and
`hub.config`, builds a synthetic library on disk, and writes
`testdata/golden/*.json`. `internal/.../golden_test.go` re-runs the same
questions through the Go port and compares.

Run it from the Go module root:

    python3 tools/gen_golden.py

Two things it does on purpose, both of which make the comparison meaningful:

  * roots are recorded *relative to the module root*, and both sides run with the
    module root as their working directory, so neither implementation gets to
    resolve a path the other did not;
  * the tree it builds contains the shapes the docstrings name -- a loose file
    next to a subdirectory, a `.part` leftover, a cover-art-only directory, a
    fullwidth name, a symlinked directory, a root that holds audio directly --
    rather than only the clean `artist/album/` case.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent
PY_HUB = MODULE.parent / "hub"

sys.path.insert(0, str(PY_HUB))

from hub import dedup, library_scan, normalize  # noqa: E402
from hub import config as hub_config  # noqa: E402

GOLDEN = MODULE / "testdata" / "golden"
SYNTH = MODULE / "testdata" / "synth"

# Names that come from the docstrings of the modules being ported, plus the
# characters that decide whether a port is right: NFKC folds width and
# compatibility forms, casefold is not lower(), `\s` is Python's set, and `\d` is
# Nd rather than ASCII.
FULLWIDTH = ["Ａ", "Ｂ", "１", "２", "．", "－", "＿", "！", "？", "～", "　"]
ODD = [
    "ß", "İ", "ǅ", "ﬁ", "ﬀ", "Ⅻ", "ⅰ", "Ⅷ", "①", "㍿", "㎡", "ｶ", "ﾞ", "ガ",
    "Ω", "µ", "Å", "Ǻ", "\u0301", "\u0308", "\u0327", "\u0e33", "\u0f77",
    "\u212b", "\u2126", "\uf900", "\ufa10", "\u1e9b", "\u0587", "\u0958",
    "\u1fbf\u0345", "\u3042", "\u30a2", "\uac00", "\ud55c", "\u1100\u1161",
    "\u05d0", "\u0645", "\u0661", "\u0662", "\u0663", "\u06f4", "\u0be6",
    "\u1c50", "\u3007", "\u30fb", "\u2026", "\u00a0", "\u3000", "\u001c",
    "\u001f", "\u0085", "\u200b", "\ufe0f", "\U0001f600", "\u00b2", "\u00bc",
]
SEPARATORS = ["", ".", "-", "_", "．", "－", "＿", " ", "  ", "\u3000", " - "]
EXTS = [".m4a", ".M4A", ".Flac", ".flac", ".mp4", ".wav", ".opus", ".aac", ".ec3",
        ".jpg", ".lrc", ".part", ".m4a.part", "", ".", ".m4", ".ogg", ".m4b"]
TITLES = [
    "Caribbean Blue", "Song. Pt. 2", "01. Artist - Title", "1. Title",
    "1979 - Song", "…to mo da ti _", "...And Then", "4pi", "1st EP",
    "A／B", "01. A／B", "To mo da ti", "intro", "escapism", "yoake", "mu",
    "!", "_", "-", "。", "!!", "", " ", "　", "01", "1-01", "12.", "123",
    "1234", "1-2-3", "01-02-03 Title", "0１．Title", "０１．Title",
    "Ｋ－ＰＯＰ", "ｶﾞｶﾞ", "Beyoncé", "びょういん", "ｂｙｏｕｉｎ", "Title!",
    "Title?", "Title　Title", "Title Title", "Ｔｉｔｌｅ", "tITle",
    "Album", "Album ", " Album", "01 - Album", "THE ALBUM", "the album",
]


def name_corpus() -> list[str]:
    names: set[str] = set(TITLES)
    for odd in ODD:
        names.add(odd)
        names.add(f"01. {odd}")
        names.add(f"{odd}.m4a")
        names.add(f"Title {odd}")
        names.add(f"{odd}{odd}")
    for fw in FULLWIDTH:
        names.add(fw)
        names.add(f"01{fw}Title")
        names.add(f"01{fw}Title.m4a")
        names.add(f"Title{fw}{fw}Title")
    rng = random.Random(20260928)
    alphabet = list("aA1 zZ") + FULLWIDTH + ODD + list(".-_␣") + list("０１２９ 〜")
    for _ in range(1500):
        length = rng.randrange(0, 9)
        body = "".join(rng.choice(alphabet) for _ in range(length))
        sep = rng.choice(SEPARATORS)
        ext = rng.choice(EXTS)
        names.add(body + sep + ext)
    for _ in range(400):
        # Pure filename shapes: number separators, extensions, weird dots.
        digits = "".join(rng.choice("0123456789０１２٣") for _ in range(rng.randrange(0, 5)))
        sep = rng.choice(SEPARATORS)
        body = rng.choice(TITLES)
        names.add(f"{digits}{sep}{body}{rng.choice(EXTS)}")
    return sorted(names)


def build_tree() -> list[str]:
    """Build the synthetic library and return the roots, module-relative.

    Every shape the ported docstrings call out, as a real directory walk rather
    than a fixture object: the two implementations have to agree about what a
    walk *yields*, and that is only observable on a filesystem.
    """
    shutil.rmtree(SYNTH, ignore_errors=True)
    (SYNTH / "lib1" / "TEMPLIME" / "Escapism").mkdir(parents=True)
    (SYNTH / "lib1" / "TEMPLIME").mkdir(exist_ok=True)
    (SYNTH / "lib1" / "薄塩指数" / "!_" / "cover").mkdir(parents=True)
    (SYNTH / "lib1" / "NTFS Drive" / "Album With Caps").mkdir(parents=True)
    (SYNTH / "lib1" / "ALAC" / "Atmos" / "TEMPLIME" / "POP-AID").mkdir(parents=True)
    (SYNTH / "lib1" / "Nyarons").mkdir(parents=True)
    (SYNTH / "lib1" / "4pi").mkdir(parents=True)
    (SYNTH / "lib1" / "9Lana" / "x").mkdir(parents=True)
    (SYNTH / "lib1" / "01. Various").mkdir(parents=True)
    (SYNTH / "lib1" / "429 & nyankobrq" / "429 & nyankobrq").mkdir(parents=True)
    (SYNTH / "lib2" / "9Lana" / "x").mkdir(parents=True)
    (SYNTH / "lib2" / "new-dl").mkdir(parents=True)
    (SYNTH / "lib-unreadable").mkdir(parents=True)
    (SYNTH / "missing-root").mkdir(parents=True)

    def touch(path: Path, *names: str) -> None:
        for name in names:
            (path / name).write_bytes(b"")

    touch(SYNTH / "lib1", "Hatsuboshi Gakuen & Kotone Fujita - Yellow Big Bang!.m4a")
    touch(SYNTH / "lib1" / "TEMPLIME", "HIKO.flac", "01. HIKO.flac")
    touch(SYNTH / "lib1" / "TEMPLIME" / "Escapism", "01. E.m4a", "02. intro.m4a")
    touch(SYNTH / "lib1" / "薄塩指数" / "!_", "。”.m4a", "… .m4a", "!!!.flac")
    touch(SYNTH / "lib1" / "薄塩指数" / "!_" / "cover", "cover.jpg")
    touch(SYNTH / "lib1" / "NTFS Drive" / "Album With Caps", "01. Track.M4A", "02. TRACK.m4a")
    touch(SYNTH / "lib1" / "ALAC" / "Atmos" / "TEMPLIME" / "POP-AID", "01. POP.m4a")
    touch(SYNTH / "lib1" / "Nyarons", "A.flac")
    touch(SYNTH / "lib1" / "4pi", "01. 4pi.m4a")
    touch(SYNTH / "lib1" / "9Lana" / "x", "01. Track.m4a", "01. track.m4a")
    touch(SYNTH / "lib1" / "01. Various", "01. Artist - Title.m4a", "02. ｶﾞｶﾞ.m4a")
    touch(SYNTH / "lib1" / "429 & nyankobrq" / "429 & nyankobrq", "01. T.m4a")
    touch(SYNTH / "lib2" / "9Lana" / "x", "01. Track.m4a")
    touch(SYNTH / "lib2" / "new-dl", "loose.m4a", "half.m4a.part")
    # Dropped in and out of the git index: a directory with no audio in it is not
    # an album scope, and a file that is only a cover must not make one.
    (SYNTH / "lib1" / "Empty Album").mkdir(exist_ok=True)

    # A symlinked directory: `os.walk` lists it and does not descend into it,
    # and the port has to agree about both halves of that.
    os.symlink("../TEMPLIME", SYNTH / "lib1" / "Linked")
    # A dangling symlink named like audio: a file, not a directory, to both.
    os.symlink("nowhere.m4a", SYNTH / "lib1" / "dangling.m4a")

    (SYNTH / "missing-root").rmdir()
    return [
        str((SYNTH / "lib1").relative_to(MODULE)),
        str((SYNTH / "lib2").relative_to(MODULE)),
        str((SYNTH / "unreachable-does-not-exist").relative_to(MODULE)),
    ]


def scan_payload(roots: list[str]) -> dict:
    scan = library_scan.scan_roots([Path(r) for r in roots])
    return {
        "roots": [str(r) for r in scan.roots],
        "reachable": list(scan.reachable),
        "degraded": [str(p) for p in scan.degraded],
        "per_root": list(scan.per_root()),
        "albums": [
            {
                "root_index": album.root_index,
                "relpath": album.relpath,
                "name": album.name,
                "artist": album.artist,
                "track_keys": sorted(album.track_keys),
                "resolved": str(scan.roots[album.root_index] / album.relpath),
            }
            for album in scan.albums
        ],
        "by_name": {
            key: [album.relpath for album in members]
            for key, members in sorted(scan.by_name.items())
        },
    }


def dedup_payload(roots: list[str]) -> list[dict]:
    scan = library_scan.scan_roots([Path(r) for r in roots])
    cases: list[tuple[str, str, str | None, str]] = [
        # One release filed in two places: loose and strict both find it.
        ("x", "Track", "9Lana", "loose"),
        ("x", "Track", "9Lana", "strict"),
        ("x", "track", "9Lana", "strict"),
        # A collab fanned out across every credited artist's folder: strict
        # misses it, which is the documented trade.
        ("429 & nyankobrq", "T", "429 & nyankobrq", "strict"),
        ("429 & nyankobrq", "T", "nyankobrq", "strict"),
        ("429 & nyankobrq", "T", "nyankobrq", "loose"),
        # An album whose name is fullwidth on disk and ASCII in the request.
        ("Various", "Artist - Title", None, "loose"),
        ("０１．Ｖａｒｉｏｕｓ", "０１．Artist - Title", "", "loose"),
        # The unusable-name refusals: `normalize` answers "" for both sides.
        ("!_", "!!!", None, "loose"),
        ("!", "!", None, "loose"),
        ("", "", None, "loose"),
        ("。”", "…", "", "loose"),
        # A title that exists in a *different* album with the same name.
        ("Escapism", "intro", "TEMPLIME", "loose"),
        ("POP-AID", "POP", "TEMPLIME", "strict"),
        # An album directory that sits directly in the root: no artist level, so
        # strict refuses it and loose still finds it.
        ("x", "Track", None, "strict"),
        ("x", "Track", "", "strict"),
    ]
    out = []
    for album, title, artist, scope in cases:
        hit = dedup.find_duplicate(
            scan,
            album_name=album,
            track_title=title,
            artist_name=artist,
            artist_scope=scope,
        )
        out.append(
            {
                "album_name": album,
                "track_title": title,
                "artist_name": artist,
                "artist_scope": scope,
                "matched": list(hit.matched) if hit else None,
                "resolved": list(hit.resolved) if hit else None,
            }
        )
    return out


def normalize_payload() -> dict:
    names = name_corpus()
    return {
        "names": [
            {
                "input": name,
                "normalized": normalize.normalize(name),
                "normalized_keep_prefix": normalize.normalize(name, strip_track_prefix=False),
                "album_key": library_scan.album_key(name),
                "is_audio_file": normalize.is_audio_file(name),
                "stem": normalize.stem_of(name),
                "nfkc": unicodedata.normalize("NFKC", name),
                "casefold": unicodedata.normalize("NFKC", name).casefold(),
            }
            for name in names
        ]
    }


CONFIG_CASES: list[dict] = [
    {"name": "defaults", "env": {"AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library"}},
    {
        "name": "everything",
        "env": {
            "AMD_PASSWORD": "pw",
            "AMD_BIND": "127.0.0.1",
            "AMD_PORT": "9000",
            "AMD_LIBRARY_ROOTS": "/library, /other ,, ",
            "AMD_RIP_CONCURRENCY": "7",
            "AMD_WRAPPER_BINARY": "/usr/local/bin/wrapper-lite-rootless",
            "AMD_WRAPPER_BASE_DIR": "/data/wrapper",
            "AMD_WRAPPER_HOST": "127.0.0.1",
            "AMD_WRAPPER_PORT": "12341",
            "AMD_DEDUP_ARTIST_SCOPE": "strict",
            "AMD_DB_PATH": "/data/other.db",
            "AMD_SESSION_SECRET": "x" * 40,
        },
    },
    {"name": "blank values fall back", "env": {
        "AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library",
        "AMD_BIND": "   ", "AMD_PORT": "", "AMD_DEDUP_ARTIST_SCOPE": " "}},
    {"name": "password missing", "env": {"AMD_LIBRARY_ROOTS": "/library"}},
    {"name": "password blank", "env": {"AMD_PASSWORD": "   ", "AMD_LIBRARY_ROOTS": "/library"}},
    {"name": "roots missing", "env": {"AMD_PASSWORD": "pw"}},
    {"name": "roots blank", "env": {"AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": " , , "}},
    {"name": "port is not a number", "env": {
        "AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library", "AMD_PORT": "8080x"}},
    {"name": "port is out of range", "env": {
        "AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library", "AMD_PORT": "70000"}},
    {"name": "concurrency is not a number", "env": {
        "AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library", "AMD_RIP_CONCURRENCY": "four"}},
    {"name": "concurrency is zero", "env": {
        "AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library", "AMD_RIP_CONCURRENCY": "0"}},
    {"name": "scope is a typo", "env": {
        "AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library", "AMD_DEDUP_ARTIST_SCOPE": "loosey"}},
    {"name": "session secret is short", "env": {
        "AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library", "AMD_SESSION_SECRET": "short"}},
    {"name": "session secret is exactly the minimum", "env": {
        "AMD_PASSWORD": "pw", "AMD_LIBRARY_ROOTS": "/library", "AMD_SESSION_SECRET": "y" * 32}},
]


def config_payload() -> list[dict]:
    out = []
    for case in CONFIG_CASES:
        entry: dict = {"name": case["name"], "env": case["env"]}
        try:
            settings = hub_config.load_settings(case["env"])
        except RuntimeError as exc:
            entry["error"] = str(exc)
        else:
            entry["settings"] = {
                "password": settings.password,
                "bind": settings.bind,
                "port": settings.port,
                "library_roots": [str(p) for p in settings.library_roots],
                "rip_concurrency": settings.rip_concurrency,
                "wrapper_binary": str(settings.wrapper_binary),
                "wrapper_base_dir": str(settings.wrapper_base_dir),
                "wrapper_host": settings.wrapper_host,
                "wrapper_port": settings.wrapper_port,
                "dedup_artist_scope": settings.dedup_artist_scope,
                "db_path": str(settings.db_path),
                # A generated secret is random by design, so only its length is
                # comparable; a configured one is compared exactly.
                "session_secret_len": len(settings.session_secret),
                "session_secret": (
                    settings.session_secret.decode()
                    if case["env"].get("AMD_SESSION_SECRET", "").strip()
                    else None
                ),
            }
        out.append(entry)
    return out


def main() -> int:
    os.chdir(MODULE)
    GOLDEN.mkdir(parents=True, exist_ok=True)
    roots = build_tree()

    payloads = {
        "normalize.json": normalize_payload(),
        "scan.json": scan_payload(roots),
        "dedup.json": {"roots": roots, "cases": dedup_payload(roots)},
        "config.json": {"cases": config_payload()},
    }
    for name, payload in payloads.items():
        path = GOLDEN / name
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n")
        print(f"wrote {path.relative_to(MODULE)} ({path.stat().st_size / 1024:.0f} KiB)")
    print(f"roots: {roots}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
