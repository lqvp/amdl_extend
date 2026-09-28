#!/usr/bin/env python3
"""Build the synthetic library the port is measured against.

Written outside the repository on purpose: it is 24,000 empty files, and the
question it answers ("how long does a walk take at this scale") is answered by
the shape of the tree, not by what is in the files.

The name mix is deliberate rather than convenient. A benchmark over ASCII-only
names would measure `os.walk` and nothing else, and the fold is where the two
implementations actually differ: this library is 40% Japanese, a tenth of the
albums carry fullwidth characters that NFKC folds, and one album in twenty has a
loose file next to a subdirectory -- the shape the real library contains and the
one that decides whether a directory is an album scope.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import time
from pathlib import Path

ARTISTS = [
    "TEMPLIME", "9Lana", "Nyarons", "薄塩指数", "429 & nyankobrq", "Kotone Fujita",
    "Ａｒｉｓｕ", "Beyoncé", "星野源", "Ado", "ｶﾞｶﾞ", "Yoasobi", "ZUTOMAYO",
]
ALBUM_SHAPES = [
    "{artist}/{album}",
    "{artist}/{album}",
    "{artist}/{album}",
    "ALAC/Atmos/{artist}/{album}",
    "{artist}/{album}",  # the common case, repeated so the mix is realistic
    "{artist}",  # loose files directly in the artist directory
]
TITLE_WORDS = [
    "Escapism", "HIKO", "POP-AID", "intro", "yoake", "mu", "Caribbean Blue",
    "…to mo da ti _", "ＡＢＣ", "Song. Pt. 2", "4pi", "1979 - Song", "１ｓｔ",
    "タイトル", "びょういん", "！！", "Ａ／Ｂ", "Ａｒｉａ",
]


def build(root: Path, albums: int, tracks: int, seed: int) -> None:
    rng = random.Random(seed)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    for index in range(albums):
        artist = rng.choice(ARTISTS)
        album = f"{rng.choice(TITLE_WORDS)} {index}"
        rel = rng.choice(ALBUM_SHAPES).format(artist=artist, album=album)
        directory = root / rel
        directory.mkdir(parents=True, exist_ok=True)
        for track in range(1, tracks + 1):
            title = rng.choice(TITLE_WORDS)
            (directory / f"{track:02d}. {title}.m4a").write_bytes(b"")
        if rng.random() < 0.05:
            (directory / "cover.jpg").write_bytes(b"")
        if rng.random() < 0.02:
            (directory / "half.m4a.part").write_bytes(b"")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/tmp/amdhub-bench/library")
    parser.add_argument("--albums", type=int, default=4000)
    parser.add_argument("--tracks", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20260928)
    args = parser.parse_args()

    started = time.perf_counter()
    build(Path(args.root), args.albums, args.tracks, args.seed)
    files = 0
    for _, _, names in os.walk(args.root):
        files += len(names)
    print(
        f"built {args.root}: {args.albums} album directories, {files} files, "
        f"{time.perf_counter() - started:.1f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
