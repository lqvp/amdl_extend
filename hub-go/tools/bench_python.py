#!/usr/bin/env python3
"""The Python side of the comparison, measuring the same three operations as
`bench/bench_test.go` over the same tree.

Run it through `tools/bench_compare.sh`, which builds the tree, runs both sides
and prints them side by side. It imports the real `hub` package -- these are not a
reimplementation of the thing being compared, they are the thing being compared.

Reported as JSON on stdout so the comparison script has one parseable source per
side, with the medians rather than the means: a walk's timing is dominated by the
page cache and by whether another process touched the disk, and one slow run
should not be allowed to move the number everything else is judged against.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "hub"))

from hub import dedup, library_scan, normalize  # noqa: E402


def time_runs(repeats: int, call) -> float:
    samples = []
    for _ in range(repeats):
        started = time.perf_counter()
        call()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--repeats", type=int, default=7)
    args = parser.parse_args()

    root = Path(args.root)
    root_paths = [root]

    # One warm-up, so the first measurement is not the one that pays for the
    # directories being read for the first time.
    scan = library_scan.scan_roots(root_paths)

    scan_seconds = time_runs(args.repeats, lambda: library_scan.scan_roots(root_paths))

    names = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        names.extend(filenames)
        if len(names) >= 50000:
            break
    if not names:
        print("no names to fold", file=sys.stderr)
        return 2
    normalize_seconds = time_runs(
        args.repeats, lambda: [normalize.normalize(name) for name in names]
    )

    queries = []
    for album in scan.albums:
        # The empty key is a real value in this library and `find_duplicate`
        # refuses to match on it, correctly; asking with it would measure the
        # refusal rather than the lookup.
        for key in sorted(album.track_keys):
            if key:
                queries.append((album.name, key, album.artist or ""))
                break
    queries.sort()
    if not queries:
        print("no albums to look up", file=sys.stderr)
        return 2

    def lookup() -> None:
        for album_name, title, artist in queries:
            dedup.find_duplicate(
                scan, album_name=album_name, track_title=title,
                artist_name=artist, artist_scope="loose",
            )

    lookup_seconds = time_runs(args.repeats, lookup)

    payload = {
        "language": "python",
        "albums": len(scan.albums),
        "names": len(names),
        "scan_seconds": scan_seconds,
        "normalize_ns_per_name": normalize_seconds / len(names) * 1e9,
        "dedup_ns_per_lookup": lookup_seconds / len(queries) * 1e9,
    }
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
