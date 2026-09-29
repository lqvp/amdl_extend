"""Read-only library discovery, search, and duplicate candidates.

Album directories are derived from a fresh filesystem walk; no tag reads or stale index
are involved. Search matches artist, album, and absolute path. The duplicate report groups
normalized album/title names across directories as *candidates*, includes every path for
human review, and never mutates files. A name match is not proof that two releases are the
same. The artist is the parent directory's name, following `library_scan._artist`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import Query, Request

from hub.api import guarded
from hub.library_scan import LibraryScan, album_key, scan_roots
from hub.normalize import normalize

router = guarded()


async def library_listing(state, search: str | None = None) -> dict:
    """Every album scope across every root, plus which roots could not be read.

    One walk, in a worker thread. `os.walk` over a 341 GB external drive blocks for long
    enough to stall the event loop, and the loop is what serves WebSocket updates -- so a
    library page would otherwise stop the queue updating for everyone while it ran.

    `per_root` is positional with `roots` and is the answer to the question a total cannot
    answer: *is each root actually contributing?* An external drive that is not plugged in
    can be mounted-and-empty rather than missing, and that reads as a healthy root with
    nothing in it -- no degraded row, no warning, and a silent re-download of everything that
    lived on it. The counts are what make that visible, so they are in the response and on
    the page rather than only in a harness somebody has to remember to run.
    """
    scan = await asyncio.to_thread(scan_roots, state.settings.library_roots)
    albums = [_album_dict(scan, album) for album in scan.albums]
    query = (search or "").strip().casefold()
    filtered = [
        album
        for album in albums
        if not query
        or any(
            query in str(album[field] or "").casefold()
            for field in ("name", "artist", "path")
        )
    ]
    return {
        "roots": [str(root) for root in scan.roots],
        "degraded_roots": [str(root) for root in scan.degraded],
        "per_root": list(scan.per_root()),
        "total_albums": len(albums),
        "search": search or "",
        "albums": filtered,
        "artists": sorted({album["artist"] for album in filtered if album["artist"]}),
    }


def _album_dict(scan, album) -> dict:
    """One album scope, with its absolute path.

    The absolute path is `roots[root_index] / relpath` and nothing else: `scan_roots` does
    not resolve anything, because the user's own path is a symlink and resolving one side of
    a comparison is how a scan ends up finding nothing. The same expression is what
    `DuplicateHit.resolved` builds and therefore what a skipped job's `skip_reason` carries,
    so the two name a directory by the same route -- which is the reason `skip_reason` uses
    that form and not the bare relpaths of `DuplicateHit.matched`, which resolve under no
    root at all once more than one is configured.
    """
    root = scan.roots[album.root_index]
    return {
        "name": album.name,
        "artist": album.artist,
        "root": str(root),
        "relpath": album.relpath,
        "path": str(root / album.relpath),
        "tracks": len(album.track_keys),
    }


@router.get("/api/library/albums")
async def albums(request: Request, q: str = Query(default="", max_length=200)) -> dict:
    return await library_listing(request.app.state, search=q)


@router.get("/api/library/artists")
async def artists(request: Request, q: str = Query(default="", max_length=200)) -> dict:
    listing = await library_listing(request.app.state, search=q)
    return {"artists": listing["artists"]}


@router.post("/api/library/scan")
async def scan(request: Request) -> dict:
    """A scan, and its result.

    The scan endpoint is the API's "invalidate the walk cache" (「walk キャッシュ破棄」) --
    and there is
    nothing to invalidate, because no walk is ever held. So it is a scan on demand, which is
    the useful half of the same idea: an operator who has just plugged a drive in can check
    that the hub sees it without waiting for the next page load.
    """
    listing = await library_listing(request.app.state)
    return {"roots": listing["roots"], "degraded_roots": listing["degraded_roots"],
            "per_root": listing["per_root"], "albums": len(listing["albums"])}


def duplicate_candidates(scan: LibraryScan) -> list[dict]:
    """Group normalized album/title pairs that occur in multiple album directories.

    This is a *candidate* report, not a destructive or definitive deduplication decision:
    two different releases can share both names. Every hit carries all paths so a person
    can adjudicate it. Empty normalized keys are ignored because they carry no identity.
    """
    groups: dict[tuple[str, str], dict[str, dict]] = {}
    for album in scan.albums:
        album_name_key = album_key(album.name)
        if not album_name_key:
            continue
        path = str(scan.roots[album.root_index] / album.relpath)
        display_by_key: dict[str, set[str]] = {}
        for filename in album.track_names:
            key = normalize(filename)
            if key:
                display_by_key.setdefault(key, set()).add(Path(filename).stem)
        for title_key in album.track_keys:
            if not title_key:
                continue
            entry = groups.setdefault((album_name_key, title_key), {})
            existing = entry.setdefault(
                path,
                {
                    "path": path,
                    "album_name": album.name,
                    "track_names": set(),
                },
            )
            existing["track_names"].update(display_by_key.get(title_key, {title_key}))

    candidates = []
    for (album_name_key, title_key), directories in groups.items():
        if len(directories) < 2:
            continue
        matches = [
            {
                "path": directory["path"],
                "album_name": directory["album_name"],
                "track_name": sorted(directory["track_names"])[0],
            }
            for directory in sorted(
                directories.values(), key=lambda row: (row["path"].casefold(), row["path"])
            )
        ]
        candidates.append(
            {
                "album_name": sorted(item["album_name"] for item in matches)[0],
                "track_name": sorted(item["track_name"] for item in matches)[0],
                "normalized_album": album_name_key,
                "normalized_track": title_key,
                "directories": matches,
                "directory_count": len(matches),
            }
        )
    return sorted(
        candidates,
        key=lambda item: (item["normalized_album"], item["normalized_track"]),
    )


@router.get("/api/library/duplicates")
async def duplicates(request: Request) -> dict:
    scan = await asyncio.to_thread(scan_roots, request.app.state.settings.library_roots)
    candidates = duplicate_candidates(scan)
    warning = (
        "Candidates are based on normalized album and track names. Matching names do not "
        "prove that files are the same release; review the listed paths. This report is "
        "read-only and never deletes files."
    )
    if scan.degraded:
        warning += " Unreadable roots were omitted: " + ", ".join(map(str, scan.degraded)) + "."
    return {
        "roots": [str(root) for root in scan.roots],
        "degraded_roots": [str(root) for root in scan.degraded],
        "warning": warning,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
