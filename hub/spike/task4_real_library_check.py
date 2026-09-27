"""Task 4 verification against the real libraries. Read-only; not part of the test suite.

Run from `hub/`:  uv run python spike/task4_real_library_check.py
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

from hub.dedup import find_duplicate
from hub.library_scan import album_key, scan_roots
from hub.normalize import normalize

ROOTS = [
    Path("/home/m/apple-dl_extend/AppleMusicDecrypt/downloads"),
    Path("/run/media/m/1A5E05A75E057D2F/Music"),
]


def main() -> None:
    t0 = time.perf_counter()
    scan = scan_roots(ROOTS)
    scan_s = time.perf_counter() - t0

    print(f"roots                 : {[str(r) for r in scan.roots]}")
    print(f"reachable             : {list(scan.reachable)}  degraded={[str(p) for p in scan.degraded]}")
    print(f"album dirs            : {len(scan.albums)}")
    print(f"distinct album names  : {len(scan.by_name)}")
    print(f"scan_roots            : {scan_s:.3f} s")

    # -- 1. how many of the 223 duplicated album-dir names are found by a name lookup ----
    # Reported per root, because §7.2.1's "223 names / 238 redundant dirs" was measured on
    # the external NTFS library alone, and only that number is comparable to the spec.
    for label, sub in [("external only", scan_roots(ROOTS[1:])), ("both roots", scan)]:
        dup_names = {k: v for k, v in sub.by_name.items() if len(v) > 1}
        redundant = sum(len(v) - 1 for v in dup_names.values())
        # An album-name lookup must reach every one of them. Asserted rather than
        # assumed: the failure mode is a lookup key built differently from the index key,
        # which silently answers "no duplicate" for everything and re-downloads the library.
        reachable = 0
        no_usable_title: list[str] = []
        unreachable: list[str] = []
        for members in dup_names.values():
            # one of the member's own tracks, asked about with the album name. A member
            # whose only keys are "" cannot be asked about at all: the title guard
            # refuses it, correctly, so it is counted separately rather than as a miss.
            usable = sorted({k for m in members for k in m.track_keys if k})
            if not usable:
                no_usable_title.append(members[0].name)
                continue
            hit = find_duplicate(sub, album_name=members[0].name, track_title=usable[0],
                                 artist_name=None, artist_scope="loose")
            if hit is not None and set(hit.matched) >= {m.relpath for m in members
                                                         if usable[0] in m.track_keys}:
                reachable += 1
            else:
                unreachable.append(f"{members[0].name!r} (title {usable[0]!r}) -> {hit}")
        print()
        print(f"[1] {label}: duplicated album names (>=2 dirs sharing a name): {len(dup_names)}")
        print(f"    redundant directories                             : {redundant}")
        print(f"    found by an album-name lookup                     : {reachable}/{len(dup_names)}")
        print(f"    not askable (only unusable track keys in the group): {len(no_usable_title)}")
        if unreachable:
            print(f"    NOT FOUND: {unreachable[:5]}")

    dup_names = {k: v for k, v in scan.by_name.items() if len(v) > 1}

    # 種別 split: 1 placement (collab fan-out) vs >=2 under one artist (種別 A)
    fanout = sum(1 for v in dup_names.values() if len({m.artist for m in v}) == len(v))
    same_artist = sum(1 for v in dup_names.values() if len({m.artist for m in v}) < len(v))
    print(f"    every copy under a distinct artist (collab)   : {fanout}")
    print(f"    >=2 copies under one artist (種別 A)           : {same_artist}")
    strict_hits = sum(
        1
        for v in dup_names.values()
        for m in v
        if find_duplicate(scan, album_name=m.name, track_title=next(iter(m.track_keys)),
                          artist_name=m.artist, artist_scope="strict") is not None
    )
    loose_hits = sum(
        1
        for v in dup_names.values()
        for m in v
        if find_duplicate(scan, album_name=m.name, track_title=next(iter(m.track_keys)),
                          artist_name=m.artist, artist_scope="loose") is not None
    )
    print(f"    per-copy detection, loose                      : {loose_hits}/{len(scan.albums)}")
    print(f"    per-copy detection, strict (artist matches)    : {strict_hits}/{len(scan.albums)}")

    # -- 2. `intro` resolves to six separate scopes -------------------------------------
    print()
    print("[2] title 'intro' (and the other five-fold titles)")
    for title in ("intro", "escapism", "mu", "yoake", ""):
        key = normalize(title)
        scopes = sorted(a.relpath for a in scan.albums if key in a.track_keys)
        distinct_albums = {album_key(a.name) for a in scan.albums if key in a.track_keys}
        print(f"    {title or '<empty>'!r:10} in {len(scopes)} album dirs, "
              f"{len(distinct_albums)} distinct album names")
        if len(scopes) <= 12:
            for rel in scopes:
                print(f"        {rel}")
        # The invariant, which is *not* "returns exactly one directory": a lookup for one
        # album name may legitimately return several directories, because that is what a
        # duplicate IS. The invariant is that a hit never crosses an album-name boundary.
        if scopes:
            checked = 0
            for a in [a for a in scan.albums if key in a.track_keys][:12]:
                # The index key is used as the query, and `find_duplicate` normalizes what
                # it is given -- so for the 6 of 8,721 keys that are not idempotent the
                # lookup misses, which is the safe direction and not a scope question. Only
                # the no-leak invariant is asserted here; the miss rate is measured in [4].
                hit = find_duplicate(scan, album_name=a.name, track_title=key,
                                     artist_name=None, artist_scope="loose")
                got = set(hit.matched) if hit else set()
                expected = {o.relpath for o in scan.albums
                            if key in o.track_keys and album_key(o.name) == album_key(a.name)}
                assert got <= expected, (
                    f"scope leak: asked for {a.relpath!r} (album {a.name!r}), "
                    f"got {sorted(got)}, album scope is {sorted(expected)}"
                )
                checked += 1
            print(f"        -> {checked} per-album lookups, none crossed an album-name "
                  f"boundary ({len(distinct_albums)} scopes, {len(scopes)} dirs)")

    # a *different* album must not borrow it
    other = next(a for a in scan.albums if "intro" not in a.track_keys and a.relpath != ".")
    borrowed = find_duplicate(scan, album_name=other.name, track_title="intro",
                              artist_name=None, artist_scope="loose")
    print(f"    unrelated album {other.name!r} asked for 'intro' -> "
          f"{'LEAK ' + str(borrowed.matched) if borrowed else 'no match (correct)'}")

    # -- 3. the empty album key ---------------------------------------------------------
    print()
    print("[3] empty album key")
    print(f"    '' in by_name : {'' in scan.by_name}")
    if "" in scan.by_name:
        members = scan.by_name[""]
        print(f"    members ({len(members)}):")
        for m in members:
            print(f"        {m.relpath}   (artist={m.artist!r}, "
                  f"{len(m.track_keys)} track keys)")
        # every non-empty track key in that group would be a false skip without the guard
        victim_titles = sorted({k for m in members for k in m.track_keys if k})[:5]
        for t in victim_titles:
            hit = find_duplicate(scan, album_name="・・・", track_title=t,
                                 artist_name=None, artist_scope="loose")
            assert hit is None, f"empty-album guard leaked: {hit}"
        print(f"    refused for punctuation-only album names against {victim_titles} -> None")
        for bad in ("・・・", "!", "_", "..."):
            hit = find_duplicate(scan, album_name=bad, track_title=victim_titles[0],
                                 artist_name=None, artist_scope="loose")
            assert hit is None, f"guard leaked for {bad!r}: {hit}"
        print("    refused for '・・・' / '!' / '_' / '...' -> None")
    # the same rule on the title side
    empty_titles = [a for a in scan.albums if "" in a.track_keys]
    print(f"    album dirs holding an unusable track key: {len(empty_titles)}")
    for bad in ("", "...", "・", "01 ..m4a"):
        hit = find_duplicate(scan, album_name=empty_titles[0].name, track_title=bad,
                             artist_name=None, artist_scope="loose")
        assert hit is None, f"empty-title guard leaked for {bad!r}: {hit}"
    print("    refused for '' / '...' / '・' / '01 ..m4a' -> None")
    # A directory named "【 呪文 】 - EP" is a real album with a *real* name; the guard must
    # not have been reached by refusing it, only by refusing its "title".
    real = next((a for a in scan.albums if a.name == "【 呪文 】 - EP"), None)
    if real is not None:
        usable = sorted(k for k in real.track_keys if k)
        print(f"    '【 呪文 】 - EP' -> {real.relpath}, {len(real.track_keys)} keys, "
              f"usable: {usable[:3]}")
        if usable:
            hit = find_duplicate(scan, album_name=real.name, track_title=usable[0],
                                 artist_name=None, artist_scope="loose")
            print(f"    asked for a real title of that album -> {hit.matched if hit else None}")
            assert hit is not None, "a real album with a real title must be found"

    # -- 4. title sharing across albums (why scoping is required) -----------------------
    print()
    per_title: Counter[str] = Counter()
    for a in scan.albums:
        for k in a.track_keys:
            per_title[k] += 1
    shared = {k: c for k, c in per_title.items() if c > 1}
    print(f"[4] both roots: normalized titles in >1 album dir: {len(shared)}/{len(per_title)} "
          f"({100 * len(shared) / len(per_title):.1f}%)")
    for label, sub in [("external only", scan_roots(ROOTS[1:])), ("downloads only", scan_roots(ROOTS[:1]))]:
        pt = Counter({k: sum(1 for a in sub.albums if k in a.track_keys) for k in per_title})
        pt = Counter({k: c for k, c in pt.items() if c})
        sh = {k: c for k, c in pt.items() if c > 1}
        print(f"    {label:15}: {len(sh)}/{len(pt)} ({100 * len(sh) / len(pt):.1f}%) "
              f"in {len(sub.albums)} album dirs")
    print("    worst offenders:", Counter(shared).most_common(8))
    # A lookup whose argument is already an index key is not what the product does -- the
    # product passes a rendered filename or a tag title. This counts the keys for which
    # the two differ, i.e. the size of the "pass a tag title and mis-key it" hazard that
    # `find_duplicate`'s docstring warns Task 9 about.
    non_idem = sorted(k for k in per_title if normalize(k) != k)
    print(f"    keys not idempotent under a second normalize(): {len(non_idem)}")
    for k in non_idem:
        print(f"        {k!r} -> {normalize(k)!r}")

    # -- 5. the collab default, concretely ---------------------------------------------
    print()
    print("[5] 'らぶふぉーゆー - Single' (the measured 種別 B case)")
    members = [a for a in scan.albums if a.name == "らぶふぉーゆー - Single"]
    for m in sorted(members, key=lambda a: a.relpath):
        print(f"    {m.relpath}  artist={m.artist!r}  {len(m.track_keys)} tracks")
    titles = sorted({k for m in members for k in m.track_keys if k})
    print(f"    titles: {titles}")
    for scope in ("loose", "strict"):
        # artist_name=None so that `strict` is measured on its album-name half alone; the
        # per-artist behaviour is measured below.
        hits = {t: find_duplicate(scan, album_name="らぶふぉーゆー - Single",
                                  track_title=t, artist_name=None, artist_scope=scope)
                for t in titles}
        widths = sorted({len(h.matched) if h else 0 for h in hits.values()})
        print(f"    {scope:6} over all {len(titles)} titles -> hit sizes {widths} "
              f"(dirs in the group: {len(members)})")
    # The reason loose is the default, on the real data: for every title, strict with the
    # *right* artist finds 1 of the 3 placements, so 2/3 of the album is re-downloaded.
    one_title = titles[0]
    for artist in sorted({m.artist for m in members if m.artist}):
        for scope in ("loose", "strict"):
            hit = find_duplicate(scan, album_name="らぶふぉーゆー - Single",
                                 track_title=one_title, artist_name=artist,
                                 artist_scope=scope)
            print(f"    title={one_title!r} artist={artist!r} {scope:6} -> "
                  f"{sorted(hit.matched) if hit else None}")


if __name__ == "__main__":
    main()
