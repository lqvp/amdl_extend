"""What is on disk, right now (spec §8, §8.1).

Phase 1 builds the *list*: the album directories across every root, and the artists derived
from the structure. That is the whole of what this needs and the whole of what it does, and
the restraint is deliberate rather than a stub:

- **No tag reads.** §8's one cacheable thing is the tag read (a `mutagen` parse per file,
  with a 300 s TTL), and nothing here needs one. An album list is a directory listing.
- **No file serving, no deletion, no duplicate report.** §9 lists
  `/api/library/files/{id}`, `/stream`, `DELETE`, and `/duplicates`, and every one of them
  is Phase 2 (§13's phase table: "FS ベースのライブラリ閲覧・検索・削除 ... 重複レポート").
  Registering them as 501s would put a route table in the codebase that answers the wrong
  thing; leaving them absent means `GET /api/library/files/1` is a 404, which is an honest
  "this build has no such route".
- **No cache.** §7.1.1 measured the walk at 0.06 s and §8 says so explicitly: a stale
  listing on a drive that was unplugged a minute ago is worse than a slow page, and there is
  no staleness window to reason about because nothing is held.

The artist is the *parent directory's* name, never one derived from `dirPathFormat`, because
the two real libraries on this machine use different conventions and only one of them
matches. That rule is `library_scan._artist`'s, and it is not restated here.
"""

from __future__ import annotations

import asyncio

from fastapi import Request

from hub.api import fail, guarded
from hub.library_scan import scan_roots

router = guarded()


async def library_listing(state) -> dict:
    """Every album scope across every root, plus which roots could not be read.

    One walk, in a worker thread. `os.walk` over a 341 GB external drive blocks for long
    enough to stall the event loop, and the loop is what serves the SSE stream -- so a
    library page would otherwise stop the queue updating for everyone while it ran.

    `per_root` is positional with `roots` and is the answer to the question a total cannot
    answer: *is each root actually contributing?* An external drive that is not plugged in
    can be mounted-and-empty rather than missing, and that reads as a healthy root with
    nothing in it -- no degraded row, no warning, and a silent re-download of everything that
    lived on it. The counts are what make that visible, so they are in the response and on
    the page rather than only in a harness somebody has to remember to run.
    """
    scan = await asyncio.to_thread(scan_roots, state.settings.library_roots)
    return {
        "roots": [str(root) for root in scan.roots],
        "degraded_roots": [str(root) for root in scan.degraded],
        "per_root": list(scan.per_root()),
        "albums": [_album_dict(scan, album) for album in scan.albums],
        "artists": sorted({album.artist for album in scan.albums if album.artist}),
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
async def albums(request: Request) -> dict:
    return await library_listing(request.app.state)


@router.get("/api/library/artists")
async def artists(request: Request) -> dict:
    listing = await library_listing(request.app.state)
    return {"artists": listing["artists"]}


@router.post("/api/library/scan")
async def scan(request: Request) -> dict:
    """A scan, and its result.

    spec §9 lists this as "walk キャッシュ破棄" -- invalidate the walk cache -- and there is
    nothing to invalidate, because no walk is ever held. So it is a scan on demand, which is
    the useful half of the same idea: an operator who has just plugged a drive in can check
    that the hub sees it without waiting for the next page load.
    """
    listing = await library_listing(request.app.state)
    return {"roots": listing["roots"], "degraded_roots": listing["degraded_roots"],
            "per_root": listing["per_root"], "albums": len(listing["albums"])}


@router.get("/api/library/duplicates")
async def duplicates(request: Request) -> dict:
    """Not in this build.

    spec §8.2's read-only duplicate report is Phase 2. It is *not* the same thing as
    `skip_reason`: that carries the paths a `loose` skip matched, for one track, so a human
    can overrule it -- and §2 refuses the cross-album group view outright, because a title
    shared by 1,207 of 8,721 library keys is not evidence of a duplicate. A caller reaching
    this gets an explanation rather than a 404.
    """
    return fail(
        501,
        "the read-only duplicate report is Phase 2 (spec §13). What this build does show is "
        "the other half: a skipped job's `skip_reason` names every path its `loose` match "
        "hit, in GET /api/jobs and in the queue page, which is what makes one skip "
        "adjudicable by hand.",
    )
