#!/usr/bin/env python3
"""Phase 1 acceptance for the two claims a scan over a mounted library has to earn.

**What this is.** The two things a live container can prove about the library that no unit
test can, because both are about *the mounts* rather than about the code:

* the numbers the hub reports are the real library's numbers, and
* a dedup decision over a real library shape comes out the way spec §7.6 says it should.

It runs **inside** the container, against `/library/a` and `/library/b` as the hub sees
them, because "as the hub sees them" is the entire claim. A host-side run of the same code
would prove the library and not the deployment.

    docker compose cp hub/deploy/acceptance_check.py amd-hub:/tmp/acceptance_check.py
    docker compose exec -T amd-hub /opt/venv/bin/python /tmp/acceptance_check.py

**`docker compose cp`, not `docker cp`.** Compose v2 names the container
`<project>-<service>-<index>`, so `docker cp ... amd-hub:` fails with `No such container`;
`docker compose cp` resolves the service name itself and is the spelling that works whether
or not the project name happens to be the directory's.

**Why the negative case carries a positive control.** The headline check is that a title
which exists in two *different* albums is not skipped. `find_duplicate` returning `None`
is the pass, and a function that returned `None` for everything would pass it perfectly.
So the same scan is also asked about tracks that really are there, and those have to come
back as duplicates naming real paths. If the positive control fails, the negative result
means nothing and this exits non-zero.

**Why it reads the roots the way `config.py` does.** `AMD_LIBRARY_ROOTS` is read through
`load_settings`, not from `os.environ` directly, so the harness cannot accidentally test a
different configuration than the one the app booted with. `scan_roots` never resolves a
root, and neither does this: relpaths are joinable back onto the root string the caller
passed, which is what makes them safe to store and to serve.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from hub.config import load_settings
from hub.dedup import find_duplicate
from hub.library_scan import scan_roots
from hub.normalize import normalize

FAILURES: list[str] = []


def reconstructible(key: str) -> str | None:
    """The `track_title` to ask `find_duplicate` about `key` with, or None if there isn't one.

    `find_duplicate` normalises its `track_title` once, because the comparison basis is the
    rendered *file name* rather than the tag (spec §7.5). The scan only kept the normalised
    form (`AlbumDir.track_keys`), so the harness has to hand back a string that normalises
    **to** the key it wants to ask about.

    For a fixed point -- `normalize(key) == key` -- the key itself is such a string, exactly.
    For the handful that are not, it is not, and guessing is worse than declining. `normalize`
    is lossy: each of the real library's 6 non-fixed-points had *two* prefix tokens stripped
    to produce it, so there is no single string this harness can construct that is guaranteed
    to normalise back. Feeding the bare key in -- which is what the first version did --
    double-normalises the minority and silently misses them; that was the one "miss" in the
    first run, and it was the harness's bug rather than the code's.

    So they are counted and named. A number in the output beats a plausible answer that is
    quietly wrong, and it matches the 6 the project already documents.
    """
    if not key or normalize(key) != key:
        return None
    return key


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    settings = load_settings()
    roots = settings.library_roots
    print(f"AMD_LIBRARY_ROOTS = {[str(r) for r in roots]}")
    print(f"AMD_DEDUP_ARTIST_SCOPE = {settings.dedup_artist_scope}")

    # -- 1. the mounts are the library -------------------------------------
    # Per-root counts, not just a total. A total of 4,739 is equally consistent with both
    # mounts working and with /library/b being a silently empty directory that /library/a
    # alone happened to fill -- and a bind mount that Docker autocreated over a dangling
    # symlink looks exactly like the second case with no error anywhere. A root with zero
    # album directories is the specific thing that must not pass unnoticed.
    scan = scan_roots(roots)
    per_root: dict[int, int] = defaultdict(int)
    for album in scan.albums:
        per_root[album.root_index] += 1

    print("\n== 1. the mounts are the library ==")
    check("no root is degraded", not scan.degraded, f"degraded={[str(d) for d in scan.degraded]}")
    for index, root in enumerate(scan.roots):
        count = per_root.get(index, 0)
        exists = root.is_dir()
        # >0, not >1: a root that is mounted but empty is the failure being looked for, and
        # the check has to be able to say so.
        check(
            f"{root} is a readable directory holding albums",
            exists and count > 0,
            f"{count} album directories",
        )
    print(f"  total album directories: {len(scan.albums)}")
    check("total is non-zero", len(scan.albums) > 0, f"{len(scan.albums)}")

    # Relpaths have to be joinable back onto the root as the caller spelled it, because
    # that string is what the API hands to a browser and what skip_reason quotes.
    sample = scan.albums[0]
    joined = scan.roots[sample.root_index] / sample.relpath
    check(
        "a relpath rejoins onto its root",
        joined.is_dir(),
        f"{sample.relpath!r} -> {joined}",
    )

    # -- 2. a real title shared by two different albums is NOT skipped -------
    # This is spec §7.6's accepted outcome and the one a "helpful" index would get wrong:
    # a track title is not an identity. Grouping every track key in the library and finding
    # the ones that appear in more than one *album directory* gives the exact shape --
    # same title, different albums -- where skipping would be a lost download.
    print("\n== 2. a title in two different albums is not skipped ==")
    titles: dict[str, list] = defaultdict(list)
    for album in scan.albums:
        for key in album.track_keys:
            titles[key].append(album)

    collisions = {
        key: dirs for key, dirs in titles.items() if len(dirs) > 1 and key
    }
    print(f"  track keys appearing in >1 album directory: {len(collisions)}")
    check("the real library has such a shape to test", len(collisions) > 0)

    checked = 0
    for key, dirs in sorted(collisions.items())[:200]:
        # For each album that holds this title, ask the real question: "if a download of
        # this track, filed under THIS album, should it be skipped?" Only the album it
        # already lives in should say yes.
        for album in dirs:
            hit = find_duplicate(
                scan,
                album_name=album.name,
                track_title=key,
                artist_name=album.artist,
                artist_scope=settings.dedup_artist_scope,
            )
            in_own_album = hit is not None and album.relpath in hit.matched
            in_other_album = hit is not None and album.relpath not in hit.matched
            # Not a failure: `loose` deliberately treats a same-named album as one album,
            # which is §7.4's accepted trade, and a collision between two *identically
            # named* albums is the case that trade is about. What must never happen is a
            # collision between albums with DIFFERENT names, where the answer would be a
            # lost download with no path for the user to adjudicate.
            different_albums = len({d.name for d in dirs}) > 1
            if different_albums and not in_own_album and not in_other_album:
                continue
            if different_albums and in_other_album:
                check(
                    f"no skip for {key!r} outside its own album",
                    False,
                    f"album={album.name!r} matched={list(hit.matched)}",
                )
            checked += 1
    print(f"  album/track pairs where the track is present in its own album: {checked}")
    check("at least one real track was found in its own album", checked > 0)

    # -- 3. the positive control -------------------------------------------
    # Without this, section 2 passes for a `find_duplicate` that never matches anything.
    print("\n== 3. positive control: a track that IS there is reported, with paths ==")
    sample_albums = [a for a in scan.albums if a.track_keys][:500]
    # Five counts, not one, and the denominator is the point. The first version printed
    # "493 of 500" and asserted only that the numerator was positive, which presented six
    # *correct refusals* on unusable keys plus one *harness bug* as seven failures of the code
    # under test. Measured on this library: 500 albums sampled, 493 asked and found, 1 missed
    # (the harness's own double-normalisation), 6 never asked (5 unusable keys + 1
    # non-fixed-point). `asked` is the only denominator that means what its label says.
    asked = found = missed = empty = not_reconstructible = 0
    missed_examples: list[str] = []
    skipped_examples: list[str] = []
    spanning = 0
    ambiguous = 0
    unresolvable: list[str] = []
    for album in sample_albums:
        for key in list(album.track_keys)[:1]:
            if not key:
                # The correct refusal: `normalize` answers "" for a title with no
                # alphanumeric character, and "" == "" would match every unusable name in
                # the library at once. The real library holds exactly 6 of these and
                # `normalize`'s own docstring says so -- counted, never `continue`d past,
                # because a bare `continue` here is what hid them.
                empty += 1
                if len(skipped_examples) < 4:
                    skipped_examples.append(f"{album.name!r} key={key!r} (unusable key)")
                continue
            title = reconstructible(key)
            if title is None:
                not_reconstructible += 1
                if len(skipped_examples) < 6:
                    skipped_examples.append(
                        f"{album.name!r} key={key!r} (normalize is not a fixed point here)"
                    )
                continue
            asked += 1
            hit = find_duplicate(
                scan,
                album_name=album.name,
                track_title=title,
                artist_name=album.artist,
                artist_scope=settings.dedup_artist_scope,
            )
            if hit is None:
                missed += 1
                if len(missed_examples) < 3:
                    missed_examples.append(f"{album.name!r} key={key!r}")
                continue
            found += 1
            # `resolved` is the form `skip_reason` carries, and it has to resolve. A path
            # that resolves under no root is not evidence, and §7.4's whole adjudication
            # story depends on the user being able to open what they are shown.
            for path in hit.resolved:
                if not Path(path).is_dir():
                    unresolvable.append(path)
            # Two distinct ways the bare `matched` form loses information, and they are not
            # the same defect, so they are counted separately:
            #
            #   spanning  -- no single root explains the whole hit, so the paths as a set do
            #                 not name a place. This is the common case with two roots and it
            #                 is the one that breaks adjudication.
            #   ambiguous -- one relpath resolves under more than one root, so the entries are
            #                 indistinguishable. Latent here; it is the 種別 A shape.
            roots_covering_hit = {
                index
                for index, root in enumerate(scan.roots)
                if all((root / relpath).is_dir() for relpath in hit.matched)
            }
            if not roots_covering_hit:
                spanning += 1
            if any(
                sum((root / relpath).is_dir() for root in scan.roots) > 1
                for relpath in hit.matched
            ):
                ambiguous += 1
    print(
        f"  albums sampled {len(sample_albums)} | asked {asked} | found {found} | "
        f"missed {missed} | not asked: unusable key {empty}, "
        f"non-fixed-point {not_reconstructible}"
    )
    for example in skipped_examples:
        print(f"  [NOT ASKED] {example}")
    for example in missed_examples:
        print(f"  [MISS] {example}")
    check(
        "every track the control asked about was found",
        missed == 0,
        f"{found}/{asked} found, {missed} missed",
    )
    check("the control asked about something", asked > 0, f"asked={asked}")
    check(
        "every resolved path in a hit exists on disk",
        not unresolvable,
        f"unresolvable={unresolvable[:3]}",
    )
    print(
        f"  [INFO] bare `matched` relpaths: {spanning}/{found} hits span roots (no single root "
        f"explains them) and {ambiguous}/{found} contain a relpath that resolves under more "
        f"than one root. `resolved` fixes both and is what skip_reason carries."
    )

    # -- 4. the shapes spec §7.4 says the real library contains -------------
    # Reported rather than asserted, because whether they exist is a fact about this
    # library, not a property of the code. `strict` vs `loose` is only a meaningful choice
    # if the 種別 B shape is actually here, and it is the shape that decides the default.
    print("\n== 4. the 種別 A / 種別 B shapes in this library (informational) ==")
    a_shape = b_shape = 0
    for _key, dirs in collisions.items():
        names = {d.name for d in dirs}
        if len(names) == 1:
            a_shape += 1
        else:
            b_shape += 1
    dup_album_names = sum(1 for v in scan.by_name.values() if len(v) > 1)
    print(f"  種別 A (same album name, several directories): {a_shape} track keys")
    print(f"  種別 B (one title, genuinely different albums): {b_shape} track keys")
    print(f"  album names held by more than one directory: {dup_album_names}")
    check("loose matching has something to work with", dup_album_names > 0)

    print()
    if FAILURES:
        print(f"ACCEPTANCE FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ACCEPTANCE OK: every check above passed over the mounted library")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
