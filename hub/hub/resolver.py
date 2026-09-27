"""Flatten a URL the user pasted into the tracks that will actually be fetched.

**Why the hub expands a container instead of handing it to the downloader.** The
queue is one table with one partial unique index on `(adam_id, codec)`
(`hub.jobs.JOB_TABLE_SQL`), and the progress bar is per track. Both of those need a flat
list up front: an album URL is not a key, and a parent job's progress is not the sum of
its children unless the children are already rows. So `expand()` is the boundary, and
everything downstream -- deduplication, the per-file decision in `hub.app`'s scheduler,
the progress bar -- works on leaves rather than on URLs. It also matches the shape the
existing TUI already shows (`src/tui/task_tree.py` registers an album group and its
tracks).

**The `web_api` parameter is the design, not a convenience.** It is keyword-only and has
no default, so a `WebAPI` can only come from the caller. That is what keeps this module
testable with no network at all, and it is also the safer shape for a service that
receives URLs from a browser: there is no code path here that can quietly construct a
client and start fetching. A convenience wrapper that built one would be invisible to a
green suite and would put a real `WebAPI` behind `expand` in production, so
`tests/test_resolver.py` reads the source to check for it rather than trusting the
tests.

**Three things this module is careful about, because each fails silently.**

*Ordering.* A playlist is in the order the user queued it, and
`playlist_write_song_index` (`src/utils.py`) writes a 1-based index per track so the
filename can carry that position. Sorting or de-duplicating the returned list destroys
the correspondence with no error anywhere, so nothing here sorts, and a track that
appears twice in a playlist produces two leaves -- "already being fetched" is the
dedup index's job (`hub.jobs`), not the resolver's, and a leaf dropped here is a track
the user was never told about.

*Completeness.* A container that stops paginating early enqueues some of its tracks and
reports success, and the missing ones are never downloaded. So the exits from the page
loop are enumerated, documented, and each has a test that fails if that exit is removed.
The authoritative end condition is the container's own `trackCount`, compared against the
count of **distinct** ids; a short page is the fallback for when there is no count, and a
page whose id sequence has been served before is the guard against a client that ignores
`offset`, under a hard page cap for the case where it permutes without repeating.
**`next` is not one of the signals**: `get_album_info` fills the embedded list and never
clears `next`, so against the real client it is true for every album and says nothing
about completeness.

And nothing the endpoint serves is dropped. `seen` in the page loop is a progress
ledger, not a filter: an earlier version de-duplicated by id and silently discarded
every entry a page repeated, which is a real shape (a user playlist listing a song
twice, a paginated endpoint serving a track on two pages) and not an impossibility.

*The empty container.* An album with no tracks, or a playlist that resolves to nothing,
is `[]`. It is not an error and not an exception, because the caller has something
better to say about it than a stack trace, and turning an empty playlist into a 500 is
the kind of bug that reads as a server fault. "No tracks" is only an error when the
lookup found *no container at all* -- a different statement, and a wrong one to make.

**No URL string is ever built here.** `Leaf.url` is the string upstream handed over, or
the container's own parsed URL when a track has none. Reconstructing
`f"https://music.apple.com/{storefront}/song/{id}"` would drop the `?i=` that is the
only part of a share link saying *which* track it is, and it would be a second place
that has to be right about Apple's URL shapes.

**Where each field comes from** -- asked for explicitly, because it is not uniform:

- *Album.* `name` and `artistName` off the **album's** own attributes, not the track's.
  `rip_album` reads them from there for the log line, and `SongMetadata` treats them as
  the album's. A compilation's tracks routinely disagree with the album about the
  artist, and the album is the container the user asked for.
- *Playlist.* There is no album lookup behind a playlist, so `albumName` and
  `artistName` come from **each track's own** attributes, per track. A playlist is
  precisely the case where those differ track to track, so a single playlist-level
  value would be a lie for every track but the first. This is
  `PlaylistInfo.Datum2.attributes` (`src/models/playlist_info.py`) -- the same fields
  `AlbumTracks.Datum.attributes` carries, which is why one read of `track.attributes`
  serves both and there is no branch on the container kind to get wrong.
- *Song.* One `get_song_info` call fills all three (`SongData.Datum.attributes`: `name`,
  `albumName`, `artistName` -- the same model `SongMetadata.parse_from_song_data` reads).
  Nothing is synthesised. Round 0 had this as `""` and was wrong: an empty `title` or an
  empty `album_name` independently defeats the whole dedup lookup, because `normalize("")`
  is falsy and `dedup.find_duplicate` returns `None` for either being falsy. Round 0
  argued that `Leaf.title` is "log display only", which is true of the *store* and not
  of everything downstream of it.
- *Music video.* `""` for all three, and not a gap that can be closed from here:
  **`WebAPI` has no music-video info method at all.** `MVRipper.rip` reads the WebKit
  manifest off `.../music-videos/{id}` itself (`src/mv.py:288`), so there is no
  catalogue record this client exposes to ask. `is_music_video=True` is the field that
  matters, and it is set.

**The scheme allowlist is checked before anything else.** `AppleMusicURL.parse_url`
validates the host with a regex and never looks at the scheme, so a `http://` or a
`file://` URL would be handed to `get_real_url` -- a request to whatever the string
names. That is a caller-controlled fetch of an arbitrary URL in a service that takes
URLs from a browser, so the scheme is checked first and nothing unvetted is forwarded.

**`src.*` is not imported here.** `tests/test_ripper_host.py` walks the AST of every
module in `hub/hub/` and fails on any importer of the upstream tree but the seam and
`hub/vendor.py`, and that is deliberate: `AppleMusicDecrypt/` is meant to become a
submodule, so a second importer couples the whole hub to a tree nobody here controls.
The parser is reached through `vendor.parse_apple_music_url`, which is the thin module
that exists for exactly that.

**The one logger.** `loguru`, for the single case where a possibly-truncated container
has to be distinguishable from a complete one (`_collect_pages`'s non-progress exit).
`loguru` is already a hard hub dependency for `ripper_host.py` and is what upstream's own
logging is built on (`src/logger.py`, bridged into the TUI by `src/tui/log_sink.py`), so a
line written here and a line written upstream land in the same place. `warnings.warn` was
the alternative and is the wrong tool twice over: it is deduplicated by default, so a
repeat would be silent, and it is routinely filtered in production.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol
from urllib.parse import urlparse

from loguru import logger

from hub.jobs import Leaf
from hub.vendor import parse_apple_music_url

# The two `offset` steps, and they are upstream's numbers rather than this module's
# taste: `WebAPI.get_album_tracks` recurses with `offset + 300` and
# `get_playlist_tracks` with `offset + 100` (`src/api.py`). The page loop's only
# length-based exit is "a short page is the last page", which is a claim about the
# endpoint's page size, so a constant chosen here instead of read from there would be
# a claim with no evidence behind it. `tests/test_resolver.py` reads both out of
# upstream's AST and fails if either moves.
ALBUM_TRACK_PAGE_SIZE = 300
PLAYLIST_TRACK_PAGE_SIZE = 100

# The outer bound on a page walk, and it is a safety net rather than a policy: nothing
# that exists comes near it. 1000 pages is 300,000 album tracks or 100,000 playlist
# entries. It exists because `_collect_pages`'s other exits are all *facts about the
# output* -- an empty page, a short page, a repeated page, a reached count -- and a client
# that ignores `offset` while permuting its output on every call satisfies none of them
# while still going for ever. The re-review measured that at 41 pages and still going.
MAX_CONTAINER_PAGES = 1000

# `URLType`'s five values, as literals. `AppleMusicURL` carries `type` as a plain
# `str`, and `hub.jobs.PARENT_TYPES` is already a closed set of the same five, so
# naming them here as strings matches a set the rest of the hub already agrees on
# rather than importing a class to read five attributes off it.
_SONG = "song"
_ALBUM = "album"
_PLAYLIST = "playlist"
_ARTIST = "artist"
_MUSIC_VIDEO = "music-video"

# The hosts a `get_real_url` redirect may be spent on, and they are a short list because
# this resolver is reachable from a browser. Following a redirect is worth doing for a
# legacy `itunes.apple.com` share link -- pre-2015 links, which upstream resolves the
# same way in `cmd.py` -- and worth nothing for anything else. Without this gate,
# `POST /api/jobs` would GET an arbitrary caller-supplied URL from the hub's network
# position, which is a server-side request forgery primitive with a JSON body. Suffixes
# rather than exact hosts because Apple serves these under several names.
_APPLE_HOST_SUFFIXES = ("music.apple.com", "itunes.apple.com")


class ResolveError(RuntimeError):
    """A URL that cannot be turned into tracks, and why not.

    `RuntimeError` and not something bespoke, for the same reason
    `ripper_host.RipperHostError` is: the hub's own startup path raises `RuntimeError`
    for operator errors, so a caller that handles one and not the other has a bug in it
    both times.

    **Not raised for a container that is simply empty.** A playlist with nothing in it
    is `[]`, because the caller has a better answer for that than a traceback. What is
    raised is a URL that is not an Apple Music one, a scheme that is not `https://`, a
    lookup that came back with no container in it, and a track with no id -- each of
    which is a statement about the request rather than about the catalogue.
    """


class WebAPI(Protocol):
    """The part of `src.api.WebAPI` this module calls, and nothing else.

    A `Protocol` rather than the real class for two reasons: the real one cannot be
    named here (the boundary test), and this is a genuinely structural dependency --
    `FakeWebAPI` in the tests satisfies it by shape, and so does anything else. The
    return types are left as `Any` because they are upstream pydantic models whose
    field names are the contract, and those are read where they are used and
    defensively (`or ""`), not declared once and trusted.
    """

    async def get_album_info(self, album_id: str, storefront: str, lang: str) -> Any: ...

    async def get_album_tracks(
        self, album_id: str, storefront: str, lang: str, offset: int = 0
    ) -> Any: ...

    async def get_playlist_info_and_tracks(
        self, playlist_id: str, storefront: str, lang: str
    ) -> Any: ...

    async def get_playlist_tracks(
        self, playlist_id: str, storefront: str, lang: str, offset: int = 0
    ) -> Any: ...

    async def get_albums_from_artist(
        self, artist_id: str, storefront: str, lang: str, offset: int = 0
    ) -> Any: ...

    async def get_song_info(self, song_id: str, storefront: str, lang: str) -> Any: ...

    async def get_real_url(self, url: str) -> str: ...


# --------------------------------------------------------------------------- #
# Reading upstream's models without importing them
# --------------------------------------------------------------------------- #
def _text(value: Any) -> str:
    """An upstream attribute as a `str`, with `None` and `""` both becoming `""`.

    Every field read here is declared `Optional` in the upstream models, so this is the
    normal case rather than the defensive one. It is not a way of hiding a missing
    field: `adam_id` and `storefront` go through `_required`, which *does* raise, and
    the descriptive fields have no consumer that compares them.
    """
    return value if isinstance(value, str) else ""


def _required(value: Any, *, what: str, url: str, subject: str = "") -> str:
    """An attribute that must be present, or a `ResolveError` naming what lacked it.

    Applied to `adam_id` and `storefront` only. An empty `adam_id` is not a track with
    no name -- `hub.jobs.JobStore._check_key` refuses it at enqueue time, so a leaf
    carrying one is a track the user was told about and never receives, which is the
    failure mode this module spends the rest of its length on avoiding. An empty
    `storefront` is worse: it silently queries a different catalogue and comes back
    with an unrelated album of the same id.

    `subject` is whatever identifies the offending item -- a track's title, say -- so
    that a 200-track album names the one that is broken rather than only the album.
    """
    text = _text(value)
    if not text.strip():
        raise ResolveError(
            f"{subject or url} resolved to a {what} with no id, so there is nothing to "
            f"fetch. An empty id would be enqueued as an unusable dedup key and the "
            f"track would never be downloaded; the whole expansion is refused instead "
            f"of returning a short list that looks complete."
        )
    return text


def _first(payload: Any, *, kind: str, url: str) -> Any:
    """`payload.data[0]`, or a `ResolveError` because the lookup found no container.

    Every catalogue response is `{"data": [ ... ]}`, and an empty one means the id did
    not resolve to an album or a playlist at all. That is **not** the empty-container
    case: an album with no tracks is a container that was found and has nothing in it,
    and reporting the two the same way would tell the user "that album has no tracks"
    when the truth is "there is no such album".
    """
    data = getattr(payload, "data", None) or []
    if not data:
        raise ResolveError(
            f"the catalogue returned no {kind} for {url}. That is different from an "
            f"empty one -- an album with no tracks expands to no leaves and is not an "
            f"error, so an empty `data` means the id does not name a {kind} in that "
            f"storefront."
        )
    return data[0]


# --------------------------------------------------------------------------- #
# The page loop
# --------------------------------------------------------------------------- #
async def _collect_pages(
    fetch: Callable[[int], Awaitable[Iterable[Any]]],
    *,
    page_size: int,
    expected: int | None,
) -> list[Any]:
    """Walk an `offset`-paginated endpoint to the end, **keeping every entry it serves**.

    `fetch(offset)` asks for one page. Four exits, and each is a real termination rather
    than a way of giving up:

    1. **an empty page** -- the end, however the endpoint says it.
    2. **a page whose id sequence has been served before** -- the client is not
       honouring `offset`, and asking again would ask forever.
    3. **`expected` reached** -- the container's own `trackCount`, the one authoritative
       total available, and the exit that makes the page-size heuristic unnecessary when
       the container states one.
    4. **a short page** -- fewer than `page_size` results cannot be followed by more,
       since `page_size` is what the endpoint serves.

    In that order, deliberately: (3) and (4) are both "there is nothing more", but (3)
    is a fact rather than an inference and (2) is a safety net that must be able to
    fire when neither holds. `expected` is compared against the count of **distinct**
    ids, never the count of entries, because `trackCount` is a count of tracks and a
    track served twice is one track -- `test_the_trackcount_exit_counts_tracks_not_entries`
    holds that direction.

    **Everything the endpoint serves is appended, and the id ledger is for progress, not
    for filtering.** An earlier version of this function skipped any item whose id was
    already held, which looked like harmless de-duplication and was not: it silently
    dropped every entry a page repeated, and a repeat is a real shape rather than an
    impossibility. Apple's own paginated endpoint is one source of them -- pages can
    overlap -- and so is a user playlist that lists a song twice. 100 entries with the
    first 5 repeated on the second page has to be 105 leaves, and
    `test_a_repeated_entry_is_kept_rather_than_deduplicated` says so for both the album
    and the playlist path. Deduplicating "already being fetched" is `hub.jobs`' job, and
    it can report the collision with the holder's id; a leaf dropped here is a track the
    user was never told about.

    **A partially overlapping page therefore yields the served count, not the distinct
    count, and that is deliberate rather than an oversight.** 12 entries of which 8 are
    unique come back as 12 leaves, not 8. Losing a track is the failure mode this module
    is arranged to avoid; over-reporting one is not -- the queue's partial unique index
    folds the repeat and reports it as `deduplicated` with the holder's id, which is a
    report the UI can show, whereas a dropped leaf is silence.

    **Exit (2) is keyed on the page's id sequence, not on a running set.** "This page
    contains only ids already collected" would be wrong twice over: it fires on the
    5-repeated page above, which is exactly the case that must be kept, and it cannot
    tell a client that ignored `offset` from one that served a legitimate overlap. What
    identifies the former is that it is **serving a page it has already served**, so the
    comparison is against the set of page sequences seen so far and needs nothing but
    those. A repeated page is discarded rather than appended -- keeping its entries would
    double a container already complete. It is also the only exit of the four that can
    mean something is *wrong* rather than that the container ended, so it is the only one
    that logs.

    **The whole set of sequences, not just the previous page**, and that is the fix for
    the hang this rule reintroduced. Comparing only to the *predecessor* stops a client
    that returns one fixed page twice, and does not stop one that permutes -- A, B, A, B
    -- which is the same bug wearing a hat. The re-review measured 41 pages of a permuting
    client with no exit firing, and it is worst exactly where `expected` is `None`: every
    playlist, since `PlaylistInfo.Tracks` carries no count, and any album without a
    `trackCount`. Round 0 terminated that case with an "added no new id" check, which C1
    showed to be the wrong test; the set is the replacement that keeps C1's correctness and
    regains the bound.

    **And a hard page cap under it**, because a set is not a bound. A client that
    permutes *without ever repeating* -- a random slice each time -- never trips the set,
    and there is no fact about its output to detect that with. `MAX_CONTAINER_PAGES` is the
    outer bound for that case: 1000 pages is 300,000 album tracks or 100,000 playlist
    entries, roughly a hundred times any real container, so it cannot truncate one that
    exists. It is read as a module global rather than a literal so a test can exercise the
    mechanism at a small value instead of building a thousand pages of pydantic models.
    """

    collected: list[Any] = []
    seen: set[str] = set()
    served_pages: set[tuple[str, ...]] = set()
    offset = 0

    for _page_number in range(MAX_CONTAINER_PAGES):
        page = await fetch(offset)
        if not page:
            return collected

        keys = tuple(_text(getattr(item, "id", None)) for item in page)
        if keys in served_pages:
            logger.warning(
                "expand(): the paginated endpoint at offset {} returned a page it has "
                "already served ({} entries), so this container may be truncated. "
                "Stopping rather than asking again, because a client that ignores "
                "`offset` would be asked forever.",
                offset, len(page),
            )
            return collected
        served_pages.add(keys)

        seen.update(key for key in keys if key)
        collected.extend(page)

        if expected is not None and len(seen) >= expected:
            return collected
        if len(page) < page_size:
            return collected
        offset += page_size

    logger.warning(
        "expand(): a container served more than {} pages of {} entries and never "
        "ended, so it is truncated here. Apple serves at most 100 per playlist page and "
        "300 per album page, so this is not a container that exists -- it is a client "
        "that is not honouring `offset`.",
        MAX_CONTAINER_PAGES, page_size,
    )
    return collected


# --------------------------------------------------------------------------- #
# Leaves
# --------------------------------------------------------------------------- #
def _video_leaf(
    parsed: Any, *, codec: str, language: str, url: str
) -> Leaf:
    """One music video, from a URL that already names it.

    `title`, `album_name` and `artist_name` are `""` and that is pinned by a test. It is
    not the same gap as a song's used to be: **`WebAPI` has no music-video info method
    at all.** `MVRipper.rip` (`src/mv.py`) reads the WebKit manifest off
    `.../music-videos/{id}` itself, so there is nothing here to ask -- the metadata is
    inside the stream, not in a catalogue record this client exposes. Filling these in
    would mean a new upstream method, which is a change to `AppleMusicDecrypt` and not
    this task's to make.

    `is_music_video=True` is the flag that matters, and it is not optional: it selects
    the Widevine path over FairPlay (`Leaf`'s own docstring, `ripper_host.run_song`).

    `url` is `parsed.url` -- the exact string the user pasted -- because
    `RipperHost.run_song` hands it to upstream's `MusicVideo` verbatim and a rebuilt URL
    would be a different string from the one that was pasted.
    """
    return Leaf(
        adam_id=_required(getattr(parsed, "id", None), what="music video", url=url),
        title="",
        album_name="",
        artist_name="",
        codec=codec,
        language=language,
        url=url,
        storefront=_required(getattr(parsed, "storefront", None), what="music video", url=url),
        is_music_video=True,
    )


async def _song_leaves(
    parsed: Any, *, codec: str, language: str, web_api: WebAPI
) -> list[Leaf]:
    """One song, and the only expansion that costs a request before it can be described.

    **`get_song_info` fills all three descriptive fields in one call** -- `name` becomes
    `title`, `albumName` becomes `album_name`, `artistName` becomes `artist_name`, off
    `SongData.Datum.attributes` (`src/models/song_data.py`), which is the same model
    `SongMetadata.parse_from_song_data` reads. Nothing is synthesised: every one of the
    six descriptive-and-identity fields is either parsed or looked up, and none is
    invented. `adam_id`, `url` and `storefront` stay from the parsed URL, so a share
    link reaches the queue as the string the user pasted, `?i=` and all.

    **Why this is not optional, which it was in round 0.** An empty `title` and an empty
    `album_name` each independently defeat the whole dedup lookup: `normalize("")` is
    falsy, and `dedup.find_duplicate` returns `None` for either being falsy. So a leaf
    with empty metadata is a leaf that can never be recognised as already-downloaded.
    `Leaf.title` is documented as "log display only; never compared", which is true of
    the *store* and not of everything downstream of it -- and the review is right that
    the dedup risk lands on the API layer, where the filename is rendered from this
    metadata.
    One request per single-track paste is a small, bounded price for a leaf that is
    actually describable; a whole class of "it re-downloaded a track I already have" bug
    is not.

    **`None` from the lookup is an error, not a blank leaf.** The id came out of a URL
    and the catalogue in that storefront says there is no such song, so returning a leaf
    with empty metadata would report success for a track nothing can download.
    """
    song = await web_api.get_song_info(parsed.id, parsed.storefront, language)
    if song is None:
        raise ResolveError(
            f"the catalogue has no song {parsed.id} in storefront {parsed.storefront}, "
            f"which {parsed.url} names. `get_song_info` returned nothing for it. A leaf "
            f"with no title and no album name cannot be checked against the library at "
            f"all, so this is refused rather than enqueued as an unidentifiable row."
        )

    attributes = getattr(song, "attributes", None)
    return [
        Leaf(
            adam_id=_required(getattr(parsed, "id", None), what="song", url=parsed.url),
            title=_text(getattr(attributes, "name", None)),
            album_name=_text(getattr(attributes, "albumName", None)),
            artist_name=_text(getattr(attributes, "artistName", None)),
            codec=codec,
            language=language,
            url=parsed.url,
            storefront=_required(
                getattr(parsed, "storefront", None), what="song", url=parsed.url
            ),
            is_music_video=False,
        )
    ]


def _track_leaf(
    track: Any,
    *,
    codec: str,
    language: str,
    storefront: str,
    container_url: str,
    album_name: str,
    artist_name: str,
) -> Leaf:
    """One track out of a container, with the container's naming where it has one.

    `album_name` / `artist_name` are the container's when it supplied them (an album
    does, and its answer beats the track's) and the track's own when it did not (a
    playlist has no album to ask). Reading both from `attributes` either way is not a
    shortcut: `AlbumTracks.Datum.attributes` and `PlaylistInfo.Datum2.attributes` are
    two different classes that both declare `albumName` and `artistName`, so there is no
    branch here that could pick the wrong field for the container kind.

    `url` is upstream's own `attributes.url` for the track, falling back to the
    container's parsed URL when it is absent. Neither is constructed.

    `is_music_video` reads the catalogue's own `type` label rather than guessing.
    `music-videos` on an audio track is not a thing the API produces, and `Leaf`'s flag
    selects the Widevine path, so a wrong `True` would be a loud failure at
    `run_music_video` rather than a silent wrong decryption -- which is the asymmetry
    that makes checking worth it. Upstream never inspects it, so this is the one place
    the resolver knows something the client does not.

    **That asymmetry argument is sound, but the label itself is unverified against the
    live API.** Nothing offline can confirm Apple sends `music-videos` here, and
    `AlbumTracks.Datum.type` / `PlaylistInfo.Datum2.type` are bare `Optional[str]`
    fields, so an upstream change to the label would not raise anywhere -- it would
    simply stop flagging, and a music video inside a playlist would go down the
    FairPlay path and fail visibly at `run_music_video` instead. The test below pins the
    behaviour *given* the label; it does not pin the label.
    """
    attributes = getattr(track, "attributes", None)
    title = _text(getattr(attributes, "name", None))
    return Leaf(
        adam_id=_required(
            getattr(track, "id", None), what="track", url=container_url, subject=title
        ),
        title=title,
        album_name=album_name or _text(getattr(attributes, "albumName", None)),
        artist_name=artist_name or _text(getattr(attributes, "artistName", None)),
        codec=codec,
        language=language,
        url=_text(getattr(attributes, "url", None)) or container_url,
        storefront=storefront,
        is_music_video=getattr(track, "type", None) == "music-videos",
    )


# --------------------------------------------------------------------------- #
# Per-kind expansion
# --------------------------------------------------------------------------- #
async def _album_leaves(
    parsed: Any, *, codec: str, language: str, web_api: WebAPI
) -> list[Leaf]:
    """An album: one leaf per track, in the album's own order.

    The album lookup is asked first and used for the naming -- `name` and `artistName`
    off its own attributes -- and its embedded `relationships.tracks.data` is **trusted
    when `trackCount` says it holds every track.**

    `trackCount` is the only completeness signal there is, and it is the right one to
    trust because it comes from the album's own attributes in the *same* response: if it
    says 12, the album has 12 tracks, whatever the embedded list happens to contain. So
    against the real client this costs exactly the requests `rip_album` costs and no
    more -- `get_album_info` replaces the embedded list with the whole album whenever
    `relationships.tracks.next` is set (`src/api.py:165-168`), and `rip_album` reads the
    result with no loop at all.

    **`next` is explicitly *not* used as a completeness signal**, which round 1 got wrong
    and which the probe in that review's finding I1 measured: `get_album_info` fills the
    list and **never clears `.next`**, so `bool(next)` is true for every album against
    the real client. Trusting it re-walked the whole catalogue once per container -- a
    full extra `get_album_tracks` recursion for every album, always, with output
    identical to what the embedded list already held. When `trackCount` is absent the
    embedded count cannot be checked, and then `next` is the only thing left, so it is
    used; that is the honest fallback, not the main path.

    Otherwise the paginated endpoint is walked from offset 0. Starting at 0 rather than
    past the embedded page is what keeps this independent of how many tracks the album
    lookup happened to embed -- a caller that embedded 12 and set `next` is not assumed
    to have served a 300-track page.
    """
    album_id = parsed.id
    storefront = parsed.storefront
    album = await web_api.get_album_info(album_id, storefront, language)
    data = _first(album, kind="album", url=parsed.url)

    attributes = getattr(data, "attributes", None)
    album_name = _text(getattr(attributes, "name", None))
    artist_name = _text(getattr(attributes, "artistName", None))
    track_count = getattr(attributes, "trackCount", None)

    tracks_relationship = getattr(getattr(data, "relationships", None), "tracks", None)
    embedded = list(getattr(tracks_relationship, "data", None) or ())
    total = track_count if isinstance(track_count, int) else None

    # A stated total is the authority. Without one, `next` is all that is left, and it
    # is worth spending a walk on because a client that did not paginate for us would
    # otherwise hand back page one and call it the album.
    if total is not None:
        incomplete = len(embedded) < total
    else:
        incomplete = bool(getattr(tracks_relationship, "next", None))

    if incomplete:
        tracks = await _collect_pages(
            lambda offset: web_api.get_album_tracks(album_id, storefront, language, offset),
            page_size=ALBUM_TRACK_PAGE_SIZE,
            expected=total,
        )
    else:
        tracks = embedded

    return [
        _track_leaf(
            track,
            codec=codec,
            language=language,
            storefront=storefront,
            container_url=parsed.url,
            album_name=album_name,
            artist_name=artist_name,
        )
        for track in tracks
    ]


async def _playlist_leaves(
    parsed: Any, *, codec: str, language: str, web_api: WebAPI
) -> list[Leaf]:
    """A playlist: one leaf per entry, **in the order the endpoint returned**.

    No sorting, and no de-duplicating. Both are invisible when they happen and both
    destroy something load-bearing -- `playlist_write_song_index` numbers the entries
    from 0 in exactly this order so the TUI can put a playlist track in slot N, and the
    dedup index already answers "this track is already being fetched" with an id. A
    playlist that lists a song twice yields two leaves, and the second becomes a
    `deduplicated` entry in `BatchResult`, which is the report the UI shows.

    The album and artist names come from each entry's own attributes, because a playlist
    has no album to look up. Asking for one per track would turn a 500-track playlist
    into 500 requests, and a playlist is exactly the case where the two names differ
    entry to entry, so one playlist-level value would be wrong for most of them.
    """
    playlist_id = parsed.id
    info = await web_api.get_playlist_info_and_tracks(playlist_id, parsed.storefront, language)
    data = _first(info, kind="playlist", url=parsed.url)

    tracks_relationship = getattr(getattr(data, "relationships", None), "tracks", None)
    embedded = list(getattr(tracks_relationship, "data", None) or ())

    if getattr(tracks_relationship, "next", None):
        tracks = await _collect_pages(
            lambda offset: web_api.get_playlist_tracks(
                playlist_id, parsed.storefront, language, offset
            ),
            page_size=PLAYLIST_TRACK_PAGE_SIZE,
            expected=None,
        )
    else:
        tracks = embedded

    return [
        _track_leaf(
            track,
            codec=codec,
            language=language,
            storefront=parsed.storefront,
            container_url=parsed.url,
            album_name="",
            artist_name="",
        )
        for track in tracks
    ]


async def _artist_leaves(
    parsed: Any, *, codec: str, language: str, web_api: WebAPI
) -> list[Leaf]:
    """An artist: every album it lists, each expanded in turn.

    Each album URL is parsed and dispatched through the same path as a URL the user
    pasted, so a storefront that differs per release is queried where it exists rather
    than with the artist URL's.

    **The order of the leaves here is not meaningful and cannot be made to be.**
    `WebAPI.get_albums_from_artist` ends in `list(set(albums))`, so upstream discards
    the order the API returned before the resolver ever sees it. Within one album the
    order is the album's and is preserved; across albums it is a set's, and re-sorting
    it here would promise an ordering the source does not have. `--include-participate-
    songs`, which upstream reaches through `get_songs_from_artist`, is not offered
    either: `expand`'s signature is pinned without a flag for it, and choosing one
    silently would be a default nobody asked for.

    An entry that is not an Apple Music URL is a `ResolveError` rather than a skip. A
    skip would drop those tracks from the queue with nothing to say so, which is the
    outcome this module is arranged to avoid everywhere else.
    """
    album_urls = await web_api.get_albums_from_artist(
        parsed.id, parsed.storefront, language
    )

    leaves: list[Leaf] = []
    for raw in album_urls or ():
        album = parse_apple_music_url(raw)
        if album is None:
            raise ResolveError(
                f"the artist {parsed.id} lists {raw!r}, which is not an Apple Music URL. "
                f"Refusing the whole expansion: skipping it would report an artist as "
                f"expanded while the tracks under it were never queued."
            )
        leaves.extend(await _expand(album, codec=codec, language=language, web_api=web_api))
    return leaves


async def _expand(parsed: Any, *, codec: str, language: str, web_api: WebAPI) -> list[Leaf]:
    """Dispatch on the kind `parse_url` decided, not on the URL's path.

    The distinction is real and is why this is one function rather than a `match` on
    the raw string: a share link is an *album* URL carrying `?i=<songId>`, and
    `parse_url` is what turns it into a song. Dispatching on the path would turn every
    share a user pasted into a whole-album download.
    """
    kind = getattr(parsed, "type", None)

    if kind == _ALBUM:
        return await _album_leaves(parsed, codec=codec, language=language, web_api=web_api)
    if kind == _SONG:
        return await _song_leaves(parsed, codec=codec, language=language, web_api=web_api)
    if kind == _PLAYLIST:
        return await _playlist_leaves(parsed, codec=codec, language=language, web_api=web_api)
    if kind == _ARTIST:
        return await _artist_leaves(parsed, codec=codec, language=language, web_api=web_api)
    if kind == _MUSIC_VIDEO:
        return [_video_leaf(parsed, codec=codec, language=language, url=parsed.url)]
    raise ResolveError(
        f"{parsed.url} parsed as {kind!r}, which is not one of "
        f"{sorted((_SONG, _ALBUM, _PLAYLIST, _ARTIST, _MUSIC_VIDEO))}. A future "
        f"Apple Music URL kind has to be added here deliberately: expanding it as a "
        f"song would queue a track nothing can fetch, and this branch is unreachable "
        f"from today's parses -- which is exactly why it exists, since the release "
        f"that adds a sixth kind is the one where the suite is least likely to run."
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _is_apple_host(url: str) -> bool:
    """Whether a URL is on an Apple host, judged on the parsed netloc.

    The host part only, and compared whole: `music.apple.com.attacker.example` ends
    with a name that looks Apple's and is not, and it is the part a redirect is
    actually sent to. `parse_url`'s own regex has the same blind spot, which is part of
    why this exists rather than a comment on the regex.
    """
    host = (urlparse(url).hostname or "").lower()
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in _APPLE_HOST_SUFFIXES)


async def expand(url: str, *, codec: str, language: str, web_api: WebAPI) -> list[Leaf]:
    """Every track `url` names, flattened, in the order the user asked for them.

    `codec` and `language` are passed through to every leaf unchanged, so one
    expansion is one decision about both -- a second album in the same request cannot
    pick a different codec, which is also why they are parameters and not settings read
    from here.

    `web_api` is the upstream client, injected and never constructed, which is what
    keeps this off the network under test. See the module docstring.

    Raises `ResolveError` for a URL this cannot turn into tracks: a scheme that is not
    `https://`, a host that is not Apple's, a URL upstream's parser does not recognise,
    a lookup that came back with no container in it, and a track with no id. Returns
    `[]` -- not an error -- for a container that genuinely has nothing in it.
    """
    cleaned = url.strip() if isinstance(url, str) else ""

    # First, and before anything that can be handed to the network. `parse_url` checks
    # the host with a regex and never looks at the scheme, so without this a `file://`
    # or `http://` string would reach `get_real_url` as a request to a caller-chosen
    # target. Stripping first is not politeness: `urlparse` keeps a trailing space in
    # the last path segment, so an unstripped paste yields the id `"1688539265 "` and a
    # 404 the user cannot act on.
    if not cleaned.startswith("https://"):
        raise ResolveError(
            f"{url!r} is not an https:// URL, so it was refused without being sent "
            f"anywhere. Only Apple Music links are accepted, and the scheme is checked "
            f"before the URL is parsed or fetched: a caller-supplied string is not a "
            f"request this service should make on someone's behalf."
        )

    parsed = parse_apple_music_url(cleaned)
    if parsed is None:
        parsed = await _follow_apple_redirect(cleaned, web_api=web_api)
    if parsed is None:
        raise ResolveError(
            f"{cleaned} is not an Apple Music URL. Acceptable forms are "
            f"https://music.apple.com/<storefront>/<song|album|playlist|artist|"
            f"music-video>/.../<id>, with a share link's ?i=<songId> naming a single "
            f"track. Nothing was fetched for it."
        )

    return await _expand(parsed, codec=codec, language=language, web_api=web_api)


async def _follow_apple_redirect(url: str, *, web_api: WebAPI) -> Any:
    """Resolve a legacy Apple link to the canonical one, or return `None`.

    `parse_url` matches `https://music.apple.com/...` only, and the share links Apple
    handed out before 2015 are `itunes.apple.com`, which redirect to the equivalent
    `music.apple.com` URL. Upstream spends one `get_real_url` on this for the same
    reason (`src/cmd.py`), and the leaf then carries the **canonical** URL rather than
    the pasted one -- deliberately, because the pasted form is not a URL the rest of
    the client can read and there is nothing in it worth preserving.

    Gated on an Apple host, and that gate is the reason this is not simply upstream's
    behaviour copied. This is called from a web request, so an ungated fallback would
    let `POST /api/jobs` name any https URL and have the hub fetch it. A non-Apple host
    gets `None` without a request at all, which the caller turns into a `ResolveError`.

    A transport failure propagates rather than being turned into a `ResolveError`: the
    URL may be perfectly good and the network may not be, and reporting "this is not an
    Apple Music URL" about a link that is one is the kind of wrong answer that sends a
    user off debugging the wrong thing.
    """
    if not _is_apple_host(url):
        return None
    real_url = await web_api.get_real_url(url)
    return parse_apple_music_url(real_url) if real_url else None
