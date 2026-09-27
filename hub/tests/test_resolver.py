"""Flattening a pasted URL into the leaves the queue will hold.

`expand()` is the boundary between "a URL the user gave" and "the tracks we will
actually fetch", so what is tested here is mostly *which* questions the resolver must
not get wrong. Four of them are silent, which is what makes them worth a test each:

**A partial expansion.** A container whose pagination stopped early enqueues some of
its tracks and reports success; the missing ones are never downloaded and nothing says
so. `test_album_pagination_keeps_asking_until_trackcount` and its two neighbours are
about the four ways the loop can end and about which of them is allowed to.

**A reordered container.** `playlist_write_song_index` (`src/utils.py`) exists so the
TUI can write a playlist track into slot N; that index is the order the user queued.
Sorting or de-duplicating the returned list destroys it with no error, so
`test_playlist_preserves_upstream_order` and
`test_a_playlist_may_hold_the_same_track_twice` are the two shapes that can catch it
-- a shuffled list and a genuine duplicate, which is legal and which must stay two
leaves.

**An empty container read as a failure.** An album with no tracks, or a playlist that
resolves to nothing, is `[]`. Raising would turn a legitimately empty playlist into a
500, so both of those are pinned as `[]` and not as `ResolveError`.

**The injected-`web_api` rule.** `expand` takes the upstream client as a parameter and
must never build its own; that is the only reason this file runs with no network at
all. `test_expand_cannot_run_without_an_injected_web_api` pins the signature that
makes a fallback impossible, and `test_the_resolver_never_constructs_a_web_api` reads
the source rather than trusting that.

**The fixtures are the real pydantic models, not dicts standing in for them.** Each
one is a raw API-shaped dict run through `AlbumMeta` / `AlbumTracks` / `PlaylistInfo`'s
own `model_validate`, so a field the resolver reads is a field upstream actually
serialises, and a rename upstream would break these fixtures the same day it breaks
the resolver. That is also why this file has to reach into the vendor checkout: the
models live there, and `tests/` is not covered by the boundary test in
`test_ripper_host.py` that forbids the rest of the hub from importing `src.*`.

**The five real URLs are a test, not a verification I ran once.** `README.md`'s link
list is the vocabulary this feature has to answer to, and a change to the upstream
parser that broke one of them would otherwise be found by a user.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from loguru import logger

from hub import resolver, ripper_host
from hub.resolver import (
    ALBUM_TRACK_PAGE_SIZE,
    PLAYLIST_TRACK_PAGE_SIZE,
    ResolveError,
    expand,
)
from hub.vendor import parse_apple_music_url

# The five kinds of link `AppleMusicDecrypt/README.md` lists, verbatim. `SONG_SHARE_URL`
# is the one that matters most: it is an *album* URL carrying `?i=`, so a resolver that
# dispatches on the path instead of on `parse_url` expands a single song into an album.
SONG_SHARE_URL = "https://music.apple.com/jp/album/nameless-name-single/1688539265?i=1688539274"
SONG_URL = "https://music.apple.com/jp/song/caribbean-blue/339592231"
ALBUM_URL = "https://music.apple.com/jp/album/nameless-name-single/1688539265"
PLAYLIST_URL = "https://music.apple.com/jp/playlist/bocchi-the-rock/pl.u-Ympg5s39LRqp"
ARTIST_URL = "https://music.apple.com/jp/artist/%E3%83%88%E3%82%B2%E3%83%8A%E3%82%B7%E3%83%88%E3%82%A2%E3%83%AA/1688539273"
VIDEO_URL = "https://music.apple.com/jp/music-video/1800449196"

# A second storefront, for the one thing a `/jp/` fixture cannot tell apart. Every other
# URL in this file is `/jp/`, so a resolver that hardcoded `"jp"` -- or that took the
# storefront from anywhere but the parsed URL -- would agree with all of them. The
# review's finding I3 is that this is exactly the kind of assumption that survives a
# green suite, and `storefront` is one of the nine pinned fields and decides which
# catalogue is queried.
ALBUM_URL_US = "https://music.apple.com/us/album/nameless-name-single/1688539265"

RESOLVER_SOURCE = Path(resolver.__file__).resolve()
# The vendor tree, derived the same way the seam derives it: this file's own location
# and two levels up is the repository root.
VENDOR_SRC = Path(ripper_host.__file__).resolve().parents[2] / "AppleMusicDecrypt" / "src"


# --------------------------------------------------------------------------- #
# Fixtures: raw API payloads, validated by the real models
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session", autouse=True)
def vendor_on_path():
    """Put the vendor tree on `sys.path`, or skip the whole module.

    Through the seam, because that is the only route to it -- the same one
    `hub/resolver.py` uses, so a fixture that reached upstream by any other route
    would be testing a tree the resolver cannot see. Only `tests/` is exempt from
    the boundary test; `hub/hub/` is not.

    A skip here is not a pass. It means the `AppleMusicDecrypt/` checkout is absent
    and the URL vocabulary below is unverified rather than verified.
    """
    try:
        parse_apple_music_url(SONG_URL)
    except ripper_host.RipperHostError as exc:  # pragma: no cover - environment only
        pytest.skip(f"the AppleMusicDecrypt checkout is not usable here: {exc}")


def track_payload(
    adam_id: str,
    *,
    name: str,
    album_name: str | None = None,
    artist_name: str | None = None,
    kind: str = "songs",
    url: str | None = None,
) -> dict:
    """One track as the catalogue API serialises it.

    Deliberately *one* shape for all three endpoints that return a track. Upstream has
    three models for it -- `AlbumTracks.Datum`, `AlbumMeta.Datum1` and
    `PlaylistInfo.Datum2` -- whose attribute models differ in declaration order and in
    which fields they declare, but every field here is `Optional` in all three, so a
    payload that validates against one validates against the others. That is not a
    coincidence to be tidy about: it is the reason one fixture can stand for an album
    page, a self-paginated album lookup and a playlist track without lying about any
    of them.

    `url=""` is honoured rather than replaced by the default, so a fixture can express
    "upstream omitted this" -- which an `or` here would have quietly turned back into a
    value, leaving a test that meant to assert the omission asserting nothing.
    """
    return {
        "id": adam_id,
        "type": kind,
        "href": f"https://amp-api.music.apple.com/v1/catalog/jp/songs/{adam_id}",
        "attributes": {
            "name": name,
            "albumName": album_name,
            "artistName": artist_name,
            "url": url if url is not None
            else f"https://music.apple.com/jp/album/nameless-name-single/{adam_id}",
            "discNumber": 1,
            "trackNumber": 1,
            "durationInMillis": 180000,
        },
    }


def album_payload(
    album_id: str,
    *,
    name: str,
    artist_name: str,
    track_count: int | None,
    tracks: list[dict],
    paginated: bool = False,
    storefront: str = "jp",
) -> dict:
    """An `AlbumMeta` body, with `record-labels` present.

    `AlbumMeta.Relationships.record_labels` is `Field(..., alias='record-labels')` --
    the one *required* field in that model, and the reason a hand-written fixture that
    forgot it would fail on a validation error rather than on an assertion. It is a
    `RecordLabels` model rather than a bare list, which is the other thing this
    fixture gets wrong the first time.
    """
    return {
        "data": [
            {
                "id": album_id,
                "type": "albums",
                "href": f"https://amp-api.music.apple.com/v1/catalog/jp/albums/{album_id}",
                "attributes": {
                    "name": name,
                    "artistName": artist_name,
                    "upc": "00602508693030",
                    "trackCount": track_count,
                    "isSingle": True,
                    "isComplete": True,
                    "url": f"https://music.apple.com/{storefront}/album/x/{album_id}",
                    "releaseDate": "2023-09-06",
                },
                "relationships": {
                    "tracks": {
                        "href": (
                            "https://amp-api.music.apple.com/v1/catalog/jp/albums/"
                            f"{album_id}/tracks"
                        ),
                        "next": (
                            "https://amp-api.music.apple.com/v1/catalog/jp/albums/"
                            f"{album_id}/tracks?offset={ALBUM_TRACK_PAGE_SIZE}"
                            if paginated
                            else None
                        ),
                        "data": tracks,
                    },
                    "record-labels": {"href": "", "data": []},
                },
            }
        ]
    }


def playlist_payload(
    playlist_id: str,
    *,
    name: str,
    curator: str,
    tracks: list[dict],
    paginated: bool = False,
) -> dict:
    """A `PlaylistInfo` body.

    `PlaylistInfo.Datum.relationships.tracks` is required (not optional), so this one
    cannot be built half-populated even if a test meant to.
    """
    return {
        "data": [
            {
                "id": playlist_id,
                "type": "playlists",
                "href": f"https://amp-api.music.apple.com/v1/catalog/jp/playlists/{playlist_id}",
                "attributes": {
                    "name": name,
                    "curatorName": curator,
                    "playlistType": "editorial",
                    "url": PLAYLIST_URL,
                },
                "relationships": {
                    "curator": {"href": "", "data": []},
                    "tracks": {
                        "href": (
                            "https://amp-api.music.apple.com/v1/catalog/jp/playlists/"
                            f"{playlist_id}/tracks"
                        ),
                        "next": "https://example.invalid/next" if paginated else None,
                        "data": tracks,
                    },
                },
            }
        ]
    }


def album_case(album_id: str, *, name: str, artist_name: str, tracks: list[dict], **kwargs):
    """A self-consistent `(album_meta, tracks_by_id)` pair for `FakeWebAPI`."""
    return (
        album_payload(
            album_id, name=name, artist_name=artist_name,
            track_count=len(tracks), tracks=tracks, **kwargs
        ),
        {album_id: tracks},
    )


def song_payload(
    song_id: str, *, name: str, album_name: str, artist_name: str
) -> dict:
    """A `SongData` body, which is what `get_song_info` validates.

    `SongData.Datum.relationships` is **required**, and so are `albums` and `artists`
    inside it -- unlike every other track model in this file, where `attributes` alone
    is enough. The `relationships` are populated with real-looking album and artist ids
    because that is what `include=albums` returns, and because a fixture that left them
    out would be testing a response shape Apple does not send.
    """
    return {
        "data": [
            {
                "id": song_id,
                "type": "songs",
                "href": f"https://amp-api.music.apple.com/v1/catalog/jp/songs/{song_id}",
                "attributes": {
                    "name": name,
                    "albumName": album_name,
                    "artistName": artist_name,
                    "discNumber": 1,
                    "trackNumber": 1,
                    "durationInMillis": 180000,
                    "isAppleDigitalMaster": True,
                    "url": f"https://music.apple.com/jp/album/x/{song_id}",
                },
                "relationships": {
                    "albums": {
                        "href": "",
                        "data": [
                            {
                                "id": "1688539265",
                                "type": "albums",
                                "href": "",
                                "attributes": {
                                    "name": album_name,
                                    "artistName": artist_name,
                                    "upc": "00602508693030",
                                    "trackCount": 1,
                                    "url": ALBUM_URL,
                                },
                            }
                        ],
                    },
                    "artists": {
                        "href": "",
                        "data": [
                            {
                                "id": "1688539273",
                                "type": "artists",
                                "href": "",
                                "attributes": {"name": artist_name},
                            }
                        ],
                    },
                },
            }
        ]
    }

# --------------------------------------------------------------------------- #
# The stand-in for src.api.WebAPI
# --------------------------------------------------------------------------- #
class FakeWebAPI:
    """A page-at-a-time stand-in for `src.api.WebAPI`, recording every call.

    **It paginates, because the real one does not have to.** Upstream's
    `get_album_tracks` recurses on `AlbumTracks.next` and `get_album_info` replaces
    its embedded page with the result, so the real client answers `get_album_info`
    already complete. A fake that always did that would prove nothing about the
    resolver's loop, and a fake that never did would only prove it against a client
    that does not exist. So the *fixture* decides which shape the album lookup has --
    all tracks and no `next`, or page one and a `next` -- and the fake serves pages at
    `offset` either way. `test_the_resolver_works_against_both_client_shapes` runs the
    same URL through both and requires the same leaves.

    `calls` is a list of `(method, *args)` so a test can assert *which* lookups
    happened, not just how many leaves came back. That is the only way to pin
    "a bare song needs no request at all" and "a playlist never looks an album up".
    """

    def __init__(
        self,
        *,
        albums: dict[str, dict] | None = None,
        tracks: dict[str, list[dict]] | None = None,
        playlists: dict[str, dict] | None = None,
        playlist_tracks: dict[str, list[dict]] | None = None,
        artist_albums: dict[str, list[str]] | None = None,
        real_urls: dict[str, str] | None = None,
        songs: dict[str, dict | None] | None = None,
    ) -> None:
        self.calls: list[tuple] = []
        self._albums = albums or {}
        self._tracks = tracks or {}
        self._playlists = playlists or {}
        self._playlist_tracks = playlist_tracks or {}
        self._artist_albums = artist_albums or {}
        self._real_urls = real_urls or {}
        # `get_song_info` answers `None` for an id it has no record of, exactly as
        # upstream does when no datum in the response carries the requested id.
        self._songs = songs if songs is not None else {}

    # -- a name for every method, so `calls` says which one ---------------------
    def method_names(self) -> list[str]:
        return [call[0] for call in self.calls]

    async def get_album_info(self, album_id: str, storefront: str, lang: str):
        self.calls.append(("album_info", album_id, storefront, lang))
        from src.models import AlbumMeta

        return AlbumMeta.model_validate(self._albums[album_id])

    async def get_album_tracks(self, album_id: str, storefront: str, lang: str, offset: int = 0):
        self.calls.append(("album_tracks", album_id, storefront, lang, offset))
        from src.models import AlbumTracks

        all_tracks = self._tracks[album_id]
        page = all_tracks[offset:offset + ALBUM_TRACK_PAGE_SIZE]
        # Upstream returns `AlbumTracks.data`, a *list*, not the model: the pagination
        # happens inside the method and the caller only ever sees tracks. Returning the
        # model here would make this a fake the resolver is shaped around rather than
        # one that matches the client.
        return AlbumTracks.model_validate(
            {
                "next": (
                    f"https://example.invalid/offset={offset + ALBUM_TRACK_PAGE_SIZE}"
                    if len(all_tracks) > offset + ALBUM_TRACK_PAGE_SIZE
                    else None
                ),
                "data": page,
            }
        ).data or []

    async def get_playlist_info_and_tracks(self, playlist_id: str, storefront: str, lang: str):
        self.calls.append(("playlist_info", playlist_id, storefront, lang))
        from src.models import PlaylistInfo

        return PlaylistInfo.model_validate(self._playlists[playlist_id])

    async def get_playlist_tracks(self, playlist_id: str, storefront: str, lang: str, offset: int = 0):
        self.calls.append(("playlist_tracks", playlist_id, storefront, lang, offset))
        from src.models import PlaylistTracks

        all_tracks = self._playlist_tracks[playlist_id]
        page = all_tracks[offset:offset + PLAYLIST_TRACK_PAGE_SIZE]
        return PlaylistTracks.model_validate({"next": None, "data": page}).data or []

    async def get_albums_from_artist(self, artist_id: str, storefront: str, lang: str, offset: int = 0):
        self.calls.append(("artist_albums", artist_id, storefront, lang, offset))
        return list(self._artist_albums[artist_id])

    async def get_songs_from_artist(self, artist_id, storefront, lang, offset=0):
        raise AssertionError(
            "get_songs_from_artist must not be called: expand() has no flag for "
            "upstream's --include-participate-songs, and choosing one silently would "
            "be a default nobody asked for"
        )

    async def get_song_info(self, song_id: str, storefront: str, lang: str):
        self.calls.append(("song_info", song_id, storefront, lang))
        from src.models import SongData

        payload = self._songs.get(song_id)
        if payload is None:
            return None
        data = SongData.model_validate(payload).data
        for datum in data:
            if datum.id == song_id:
                return datum
        return None

    async def get_real_url(self, url: str) -> str:
        self.calls.append(("real_url", url))
        return self._real_urls[url]


# --------------------------------------------------------------------------- #
# A song: the share link, and the one lookup a single-track paste needs
# --------------------------------------------------------------------------- #
def song_fake(**kwargs) -> FakeWebAPI:
    """A client that knows about the one song the share-link fixtures name."""
    return FakeWebAPI(
        songs={
            "1688539274": song_payload(
                "1688539274", name="nameless", album_name="nameless", artist_name="Eve"
            ),
            "339592231": song_payload(
                "339592231", name="Caribbean Blue", album_name="nameless",
                artist_name="Eve",
            ),
        },
        **kwargs,
    )


async def test_a_share_link_expands_to_the_song_and_not_to_its_album():
    """`?i=<songId>` on an album URL is a song, and it is the common case.

    The Apple Music share sheet produces exactly this shape, so a resolver that
    dispatched on the path segment would turn every share a user pasted into a
    whole-album download. Exactly one call is made, and it is for the *song*: an album
    lookup here would be a 200-track fan-out from a link the user asked to have one
    track downloaded.
    """
    fake = song_fake()
    leaves = await expand(SONG_SHARE_URL, codec="alac", language="ja", web_api=fake)

    assert [leaf.adam_id for leaf in leaves] == ["1688539274"]
    assert fake.calls == [("song_info", "1688539274", "jp", "ja")]


async def test_a_song_leaf_carries_the_pasted_url_and_the_parsed_storefront():
    """Neither is rebuilt here.

    `run_song` hands `leaf.url` to upstream's `Song` verbatim, so a reconstructed
    `.../song/<id>` would be a different string from the one the user pasted, and the
    share link's `?i=` -- the only part that says *which* track -- would be gone.
    """
    (leaf,) = await expand(SONG_SHARE_URL, codec="alac", language="ja", web_api=song_fake())

    assert leaf.url == SONG_SHARE_URL
    assert leaf.storefront == "jp"
    assert leaf.adam_id == "1688539274"
    assert (leaf.codec, leaf.language) == ("alac", "ja")
    assert leaf.is_music_video is False


async def test_a_song_leaf_is_described_by_its_own_catalogue_record():
    """All three descriptive fields, from one `get_song_info` call.

    Round 0 returned `""` here on the argument that `Leaf.title` is "log display only;
    never compared", and that was the wrong reading. `dedup.find_duplicate` returns
    `None` when either the title key or the scope key is falsy, and `normalize("")` is
    falsy -- so an empty `title` *or* an empty `album_name` independently defeats the
    whole §7.3 lookup. A leaf that cannot be identified cannot be recognised as already
    on disk.

    Every field is read, none is synthesised: `name`, `albumName` and `artistName` off
    `SongData.Datum.attributes`, the same model `SongMetadata.parse_from_song_data`
    reads. The request is the price, and it is one bounded request per single-track
    paste -- no album lookup, no track fan-out.
    """
    fake = song_fake()
    (leaf,) = await expand(SONG_URL, codec="alac", language="ja", web_api=fake)

    assert (leaf.title, leaf.album_name, leaf.artist_name) == (
        "Caribbean Blue", "nameless", "Eve",
    )
    assert (leaf.adam_id, leaf.storefront) == ("339592231", "jp")
    assert fake.method_names() == ["song_info"]


async def test_a_song_the_catalogue_does_not_have_is_refused_not_enqueued_blank():
    """`get_song_info` returning `None` is an error, not an empty leaf.

    The id came out of a URL and the catalogue in that storefront says there is no such
    song. Returning a leaf with empty metadata would both report success for a track
    nothing can download and hand §7.3 a row it can never match.
    """
    fake = FakeWebAPI()  # knows about no songs

    with pytest.raises(ResolveError, match="339592231"):
        await expand(SONG_URL, codec="alac", language="ja", web_api=fake)


async def test_a_music_video_url_is_one_leaf_flagged_for_the_widevine_path():
    """`""` for the three descriptive fields, because there is nothing to ask for.

    `WebAPI` has no music-video info method at all: `MVRipper.rip` reads the WebKit
    manifest off `.../music-videos/{id}` itself (`src/mv.py:288`). So this is not the
    song's gap repeated -- it is a different and unclosable one, and it is pinned so
    that adding a video lookup later is a deliberate change.

    No request is made either, and the fake's `get_song_info` would return `None` for
    this id, so a resolver that routed a video through the song path would raise.
    """
    fake = FakeWebAPI()
    leaves = await expand(VIDEO_URL, codec="alac", language="ja", web_api=fake)

    assert len(leaves) == 1
    assert leaves[0].is_music_video is True
    assert leaves[0].adam_id == "1800449196"
    assert leaves[0].url == VIDEO_URL
    assert (leaves[0].title, leaves[0].album_name, leaves[0].artist_name) == ("", "", "")
    assert fake.calls == []


# --------------------------------------------------------------------------- #
# An album: which fields come from where, and how pagination ends
# --------------------------------------------------------------------------- #
ALBUM_TRACKS = [
    track_payload("t1", name="first", url="https://music.apple.com/jp/album/x/1?i=t1"),
    track_payload("t2", name="second", url="https://music.apple.com/jp/album/x/1?i=t2"),
]


def album_fake(**kwargs) -> FakeWebAPI:
    meta, tracks = album_case(
        "1688539265", name="nameless", artist_name="Eve", tracks=ALBUM_TRACKS, **kwargs
    )
    return FakeWebAPI(albums={"1688539265": meta}, tracks=tracks)


async def test_an_album_yields_one_leaf_per_track_in_order():
    leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=album_fake())

    assert [leaf.adam_id for leaf in leaves] == ["t1", "t2"]
    assert [leaf.title for leaf in leaves] == ["first", "second"]
    assert all(leaf.album_name == "nameless" for leaf in leaves)
    assert all(leaf.artist_name == "Eve" for leaf in leaves)
    assert all((leaf.codec, leaf.language) == ("alac", "ja") for leaf in leaves)
    assert all(leaf.storefront == "jp" for leaf in leaves)
    assert all(leaf.is_music_video is False for leaf in leaves)


async def test_album_names_come_from_the_album_not_from_the_track():
    """The album's own `name` / `artistName`, which is what `rip_album` logs and what
    `SongMetadata` treats as the album artist.

    The track payload here carries *different* values on purpose. An album's tracks
    routinely disagree with the album about both -- a compilation credits a different
    artist per track, and a deluxe reissue can be named differently from what the
    track attributes say -- and the album is the container the user asked for, so the
    album's answer is the one that belongs on every leaf.
    """
    tracks = [
        track_payload("t1", name="first", album_name="wrong album", artist_name="wrong artist"),
        track_payload("t2", name="second", album_name="wrong album", artist_name="wrong artist"),
    ]
    meta, by_id = album_case("1688539265", name="nameless", artist_name="Eve", tracks=tracks)
    leaves = await expand(
        ALBUM_URL, codec="alac", language="ja",
        web_api=FakeWebAPI(albums={"1688539265": meta}, tracks=by_id),
    )

    assert {leaf.album_name for leaf in leaves} == {"nameless"}
    assert {leaf.artist_name for leaf in leaves} == {"Eve"}


async def test_an_album_track_url_is_upstreams_own_never_a_reconstruction():
    """`attributes.url` for the track, verbatim.

    The hub never builds a URL string. The fallback when upstream omits one is the
    *container's* parsed URL, which is still not a reconstruction -- it is the string
    the user gave -- and the second assertion puts a track with no `attributes.url` in
    the fixture so that path is exercised rather than assumed.
    """
    tracks = [
        track_payload("t1", name="first"),
        track_payload("t2", name="second"),
        track_payload("t3", name="third", url=""),
    ]
    meta, by_id = album_case("1688539265", name="nameless", artist_name="Eve", tracks=tracks)
    leaves = await expand(
        ALBUM_URL, codec="alac", language="ja",
        web_api=FakeWebAPI(albums={"1688539265": meta}, tracks=by_id),
    )

    assert [leaf.url for leaf in leaves] == [
        "https://music.apple.com/jp/album/nameless-name-single/t1",
        "https://music.apple.com/jp/album/nameless-name-single/t2",
        ALBUM_URL,
    ]


async def test_the_resolver_works_against_both_client_shapes():
    """The same URL, once against a client that paginated the album lookup for us and
    once against one that did not.

    Upstream's `get_album_info` replaces `relationships.tracks.data` with the whole
    album when `next` is set, so against the real client the embedded list is already
    complete -- and `rip_album` trusts exactly that, with no loop at all. A resolver
    that only handled the paginated shape would therefore be correct against a fake
    and untested against production, and the two must not be able to drift apart.
    """
    complete_fake = album_fake()
    paginated_meta, paginated_tracks = album_case(
        "1688539265", name="nameless", artist_name="Eve", tracks=ALBUM_TRACKS, paginated=True
    )
    paginated_fake = FakeWebAPI(
        albums={"1688539265": paginated_meta}, tracks=paginated_tracks
    )

    complete = await expand(ALBUM_URL, codec="alac", language="ja", web_api=complete_fake)
    paginated = await expand(ALBUM_URL, codec="alac", language="ja", web_api=paginated_fake)

    assert complete == paginated
    # And the second one actually had to ask: the lookup embedded 2 of 2 but set `next`
    #... which it must NOT have to ask, if `trackCount` is the authority. Both fixtures
    # here state `trackCount=2` and embed both tracks, so the embedded list is complete
    # and `get_album_tracks` is never called -- see the two tests below for the
    # discriminator that says so.
    assert complete_fake.method_names() == ["album_info"]
    assert paginated_fake.method_names() == ["album_info"]


async def test_a_stated_trackcount_makes_the_embedded_list_authoritative():
    """I1: `next` is not a completeness signal, and `trackCount` is.

    Upstream's `get_album_info` fills the embedded list and **never clears `next`**
    (`src/api.py:165-168`), so `bool(next)` is true for *every* album against the real
    client. Round 0 trusted `next` anyway, which meant one extra full catalogue walk per
    container, always -- a `get_album_tracks` recursion whose result was identical to
    the list already in hand. The review's probe measured it at one call for a 2-track
    album, a 301-track one and a 901-track one, at offset `[0]` every time.

    The fixture here is exactly upstream's shape: `next` set, and the embedded list
    holding every track. `trackCount` says 2 and the list has 2, so it is taken, and
    `get_album_tracks` is never called.
    """
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=len(ALBUM_TRACKS), tracks=ALBUM_TRACKS, paginated=True,
    )
    fake = FakeWebAPI(albums={"1688539265": meta}, tracks={"1688539265": ALBUM_TRACKS})

    leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)

    assert [leaf.adam_id for leaf in leaves] == ["t1", "t2"]
    assert "album_tracks" not in fake.method_names(), (
        "the embedded list was already complete by trackCount, so walking the "
        "paginated endpoint again is a full extra catalogue fetch per container"
    )


async def test_an_empty_album_makes_no_paginated_request_at_all():
    """`trackCount = 0` against an empty list is *complete*, and the zero is the point.

    An album with no tracks must cost the one `get_album_info` it costs. Getting this
    wrong in the other direction -- treating "no tracks" as "there may be more" -- spends
    a request on nothing; getting it wrong by fetching would also mean a container with
    genuinely no tracks goes down the pagination path, which is where a lying client
    could keep it asking.
    """
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=0, tracks=[], paginated=False,
    )
    fake = FakeWebAPI(albums={"1688539265": meta}, tracks={"1688539265": []})

    assert await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake) == []
    assert fake.method_names() == ["album_info"]


# --------------------------------------------------------------------------- #
# The page loop: one test per exit, because each is removable on its own
# --------------------------------------------------------------------------- #
async def test_exit_a_full_page_makes_it_ask_again():
    """Not an exit -- the *continuation*. It has no test of its own otherwise, and it is
    the condition a regression in any of the three exits below would be hidden by."""
    tracks = [track_payload(f"t{i}", name=f"track {i}") for i in range(ALBUM_TRACK_PAGE_SIZE + 1)]
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=len(tracks), tracks=tracks[:ALBUM_TRACK_PAGE_SIZE], paginated=True,
    )
    fake = FakeWebAPI(albums={"1688539265": meta}, tracks={"1688539265": tracks})

    leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)

    assert len(leaves) == ALBUM_TRACK_PAGE_SIZE + 1
    assert [c[-1] for c in fake.calls if c[0] == "album_tracks"] == [0, ALBUM_TRACK_PAGE_SIZE]


async def test_exit_1_an_empty_page_ends_the_walk():
    """The first exit, previously untested.

    The album lookup embeds nothing and sets `next`, and the paginated endpoint answers
    the first request with nothing at all. So the endpoint was asked once, at offset 0,
    and the empty answer ended it -- which is the only assertion that distinguishes this
    exit from the case where the request was never made. The result is `[]` either way,
    so without the call assertion this test would pass against a resolver that never
    walked anything.
    """
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=5, tracks=[], paginated=True,
    )
    fake = FakeWebAPI(albums={"1688539265": meta}, tracks={"1688539265": []})

    leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)

    assert leaves == []
    assert [c[-1] for c in fake.calls if c[0] == "album_tracks"] == [0]


async def test_exit_2_a_page_already_served_ends_the_walk_and_says_so():
    """The only exit that can mean something is *wrong*, so it is also the only one that
    logs. Everything else is an ordinary end of a container; this one is a client that
    ignored `offset`, and the list is about to be returned as though it were the whole
    thing -- the partial expansion reported as success that this module exists to avoid.

    Asserted three ways: the walk stops, it stops *quickly* (the call budget turns a
    regression into a failure rather than a hang, which is what removing this exit
    otherwise produces), and the log line is emitted. The last one is the review's M1.
    """
    tracks = [track_payload(f"t{i}", name=f"track {i}") for i in range(ALBUM_TRACK_PAGE_SIZE)]
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=None, tracks=tracks, paginated=True,
    )
    budget = 4

    class _IgnoresOffset(FakeWebAPI):
        async def get_album_tracks(self, album_id, storefront, lang, offset=0):
            self.calls.append(("album_tracks", album_id, storefront, lang, offset))
            if len([c for c in self.calls if c[0] == "album_tracks"]) > budget:
                raise AssertionError(
                    f"get_album_tracks was called more than {budget} times for an album "
                    f"of {len(tracks)} tracks: the page loop is not terminating against "
                    f"a client that ignores `offset`"
                )
            from src.models import AlbumTracks

            return AlbumTracks.model_validate(
                {"next": "https://example.invalid/next", "data": tracks}
            ).data or []

    fake = _IgnoresOffset(albums={"1688539265": meta}, tracks={"1688539265": tracks})
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)
    finally:
        logger.remove(sink_id)

    assert len(leaves) == ALBUM_TRACK_PAGE_SIZE
    assert [c[-1] for c in fake.calls if c[0] == "album_tracks"] == [0, ALBUM_TRACK_PAGE_SIZE]
    assert any("already served" in m for m in messages), messages


async def test_exit_2_also_catches_a_client_that_permutes_its_output():
    """N1: the predecessor comparison was not a bound, and this is the case that proved it.

    Round 1 keyed the no-progress check on "this page's ids equal the previous page's",
    which stops a client that returns one fixed page twice and **does not stop one that
    permutes** -- A, B, A, B. The re-review measured 41 pages and still going.

    It is worst exactly where `expected` is `None`, because the `trackCount` exit cannot
    fire either: **every playlist**, since `PlaylistInfo.Tracks` carries no count, and any
    album whose lookup stated no `trackCount`. This fixture is therefore a *playlist*, the
    case the bug is actually reachable in, with `paginated=True` and no count anywhere.

    The rule is now "a page sequence already served", so the fourth request repeats page
    one and stops. The call budget is what makes a regression a failure rather than a hang.

    Two **full** pages, because a short page is exit 4 and would end the walk on the first
    request -- which is what a first attempt at this fixture did, and it is the reason
    `PLAYLIST_TRACK_PAGE_SIZE` is in the arithmetic rather than left implicit.
    """
    entries = [
        track_payload(f"p{i:03d}", name=f"track {i}")
        for i in range(PLAYLIST_TRACK_PAGE_SIZE * 2)
    ]
    pages = [
        entries[:PLAYLIST_TRACK_PAGE_SIZE], entries[PLAYLIST_TRACK_PAGE_SIZE:]
    ]  # what a correct client serves, in order
    budget = 6

    class _Permutes(FakeWebAPI):
        async def get_playlist_tracks(self, playlist_id, storefront, lang, offset=0):
            self.calls.append(("playlist_tracks", playlist_id, storefront, lang, offset))
            if len([c for c in self.calls if c[0] == "playlist_tracks"]) > budget:
                raise AssertionError(
                    f"get_playlist_tracks was called more than {budget} times for a "
                    f"{len(entries)}-entry playlist: the page walk is not terminating "
                    f"against a client that permutes its output"
                )
            from src.models import PlaylistTracks

            # `pages[call - 1]`: page one on the first call, page two on the second, and
            # page one again on the third -- so a predecessor comparison would not see it.
            index = (len([c for c in self.calls if c[0] == "playlist_tracks"]) - 1) % 2
            return PlaylistTracks.model_validate(
                {"next": "https://example.invalid/next", "data": pages[index]}
            ).data or []

    fake = _Permutes(
        playlists={
            "pl.u-Ympg5s39LRqp": playlist_payload(
                "pl.u-Ympg5s39LRqp", name="bocchi", curator="Apple Music",
                tracks=pages[0], paginated=True,
            )
        }
    )
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        leaves = await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=fake)
    finally:
        logger.remove(sink_id)

    assert [c[-1] for c in fake.calls if c[0] == "playlist_tracks"] == [
        0, PLAYLIST_TRACK_PAGE_SIZE, PLAYLIST_TRACK_PAGE_SIZE * 2,
    ]
    assert any("already served" in m for m in messages), messages
    # And the two pages it did serve are both kept -- the exit discards the *repeat*,
    # not everything since the first page.
    assert [leaf.adam_id for leaf in leaves] == [f"p{i:03d}" for i in range(200)]


async def test_a_client_that_permutes_without_ever_repeating_is_stopped_by_the_page_cap(monkeypatch):
    """N1's outer bound, for the one case the page set cannot detect.

    A client that returns a *different* full page every call satisfies every fact-based
    exit: the page is not empty, it is full, its id sequence has not been served before,
    and with no `trackCount` there is no total to reach. There is nothing about its output
    to detect it with, so the bound has to be a number.

    Exercised at `MAX_CONTAINER_PAGES = 3` by monkeypatching, because building a thousand
    pages of pydantic models to test a `for` loop's range is not a trade worth making. The
    real constant is 1000, which is ~100x any container Apple serves.
    """
    monkeypatch.setattr(resolver, "MAX_CONTAINER_PAGES", 3)
    entries = [track_payload(f"t{i:03d}", name=f"track {i}")
               for i in range(ALBUM_TRACK_PAGE_SIZE)]
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=None, tracks=entries, paginated=True,
    )

    class _EndlessRotation(FakeWebAPI):
        async def get_album_tracks(self, album_id, storefront, lang, offset=0):
            self.calls.append(("album_tracks", album_id, storefront, lang, offset))
            from src.models import AlbumTracks

            # A rotation of the same full page, by an amount that differs on every call
            # -- keyed on the call count, *not* on `offset % page_size`, which would
            # hand back page one again on the second call and be caught by the much
            # simpler "same as the previous page" rule. Three calls cannot exhaust the
            # rotation, so the page set cannot catch this and only the cap can.
            seen_calls = len([c for c in self.calls if c[0] == "album_tracks"])
            start = (seen_calls * 7) % ALBUM_TRACK_PAGE_SIZE
            page = entries[start:] + entries[:start]
            return AlbumTracks.model_validate({"next": "x", "data": page}).data or []

    fake = _EndlessRotation(albums={"1688539265": meta}, tracks={"1688539265": entries})
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)
    finally:
        logger.remove(sink_id)

    assert [c[-1] for c in fake.calls if c[0] == "album_tracks"] == [
        0, ALBUM_TRACK_PAGE_SIZE, ALBUM_TRACK_PAGE_SIZE * 2,
    ]
    assert any("never" in m and "truncated" in m for m in messages), messages
    # The cap truncates, and says so. It is a safety net, not a way of being right.
    assert len(leaves) == 3 * ALBUM_TRACK_PAGE_SIZE


async def test_the_trackcount_exit_counts_tracks_not_entries():
    """N5: `expected` is compared against distinct ids, never against entries served.

    `trackCount` is a count of *tracks*, so a track the endpoint serves twice is one
    track -- and the `len(collected) >= expected` direction would stop the walk as soon as
    enough *entries* had gone by, which for an overlapping container is sooner than the
    album is finished. Swapping the two left the whole suite green.

    The fixture is contrived on purpose and says so: an album claiming 450 tracks whose
    pages contain only 300 distinct ids. A real album does not do that. What is real is
    the rule it pins, and the discriminator is the **call count** -- both directions return
    600 leaves here, so only the offsets tell them apart.
    """
    entries = [track_payload(f"t{i:03d}", name=f"track {i}")
               for i in range(ALBUM_TRACK_PAGE_SIZE)]
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=450, tracks=[], paginated=True,
    )

    class _OverlappingPages(FakeWebAPI):
        async def get_album_tracks(self, album_id, storefront, lang, offset=0):
            self.calls.append(("album_tracks", album_id, storefront, lang, offset))
            from src.models import AlbumTracks

            if offset >= ALBUM_TRACK_PAGE_SIZE * 2:
                return []
            # A rotation, so page two is a different sequence of the *same* 300 ids.
            # Keyed on the call count for the same reason as the fixture above: an
            # `offset % page_size` rotation would serve page one again and trip the
            # cheaper "same as the previous page" exit instead of reaching exit 3.
            seen_calls = len([c for c in self.calls if c[0] == "album_tracks"])
            start = (seen_calls * 7) % ALBUM_TRACK_PAGE_SIZE
            page = entries[start:] + entries[:start]
            return AlbumTracks.model_validate({"next": "x", "data": page}).data or []

    fake = _OverlappingPages(albums={"1688539265": meta}, tracks={})
    leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)

    assert [c[-1] for c in fake.calls if c[0] == "album_tracks"] == [
        0, ALBUM_TRACK_PAGE_SIZE, ALBUM_TRACK_PAGE_SIZE * 2,
    ], (
        "the walk stopped once 450 *entries* had gone by, although only 300 distinct "
        "tracks had been seen and trackCount counts tracks"
    )
    assert len({leaf.adam_id for leaf in leaves}) == ALBUM_TRACK_PAGE_SIZE


async def test_exit_3_trackcount_ends_the_walk_on_a_full_page():
    """The exit that only `trackCount` can fire, isolated.

    **The test that used to be named after this one did not test it.** Its docstring
    claimed "the page-size heuristic alone would have stopped at 300", which is false:
    with 301 tracks the short-page exit alone reaches 301 leaves at offsets `[0, 300]`,
    so it passed for a different reason than the one documented. Measured, not assumed.

    So the shape is chosen to make this the *only* exit that can fire: exactly one full
    page, `trackCount` equal to it. The short-page exit cannot fire on a full page, and
    the non-progress exit cannot fire on a page of 300 new ids, and the empty-page exit
    cannot fire because nothing is asked. Without the `trackCount` exit the loop would
    ask offset 300 and stop there on an empty answer -- so asserting `offsets == [0]`
    is what pins it, and one mutation (`expected=None`) makes that `[0, 300]`.
    """
    tracks = [track_payload(f"t{i}", name=f"track {i}") for i in range(ALBUM_TRACK_PAGE_SIZE)]
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=ALBUM_TRACK_PAGE_SIZE, tracks=[], paginated=True,
    )
    fake = FakeWebAPI(albums={"1688539265": meta}, tracks={"1688539265": tracks})

    leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)

    assert len(leaves) == ALBUM_TRACK_PAGE_SIZE
    assert [c[-1] for c in fake.calls if c[0] == "album_tracks"] == [0], (
        "the walk asked for a second page although trackCount had already been reached; "
        "without the trackCount exit this is [0, 300] and one extra request per album"
    )


async def test_exit_4_a_short_page_ends_the_walk_when_there_is_no_trackcount():
    """The fallback, and the one `trackCount` being absent must not break.

    A short page is the last page by definition, so with no total to compare against
    that is the only termination available -- and it is a good one, because Apple's
    album-tracks page size is 300 and a page shorter than that cannot be followed by
    more.
    """
    tracks = [track_payload(f"t{i}", name=f"track {i}") for i in range(4)]
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=None, tracks=tracks, paginated=True,
    )
    fake = FakeWebAPI(albums={"1688539265": meta}, tracks={"1688539265": tracks})

    leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)

    assert [leaf.adam_id for leaf in leaves] == ["t0", "t1", "t2", "t3"]
    assert [c[-1] for c in fake.calls if c[0] == "album_tracks"] == [0]


async def test_a_repeated_entry_is_kept_rather_than_deduplicated():
    """C1: a page that repeats entries from an earlier one must not lose them.

    Round 0's loop skipped any item whose id was already in `seen`, which read as
    harmless de-duplication and was not. The review's probe served 100 entries with the
    first 5 repeated on page two and counted **100 leaves instead of 105** -- no
    exception, no log, no test, and it contradicted three statements in the module's own
    docstring.

    A repeat is a real shape and not an impossibility. A user playlist that lists a song
    twice is one, and an endpoint serving a track on two pages is another. Deduplicating
    "already being fetched" is `hub.jobs`' job and it can name the holder; a leaf dropped
    here is a track the user was never told about.

    100 + 5 is exactly the probe's arithmetic at the playlist page size, and 300 + 5 is
    the same shape at the album one, so both paths are covered at their real page sizes
    rather than at a monkeypatched constant. The second page is *only* the repeats --
    that is the probe, and it is also the sharpest version of it: a page that is
    entirely already-seen entries, so a resolver that keys "no progress" on a running id
    set would drop all five here and return 100.
    """
    # -- playlist: 100 entries, the first 5 repeated on page two -> 105 leaves.
    playlist_entries = [
        track_payload(f"p{i:03d}", name=f"track {i}") for i in range(100)
    ]
    page_two = playlist_entries[:5]
    fake = FakeWebAPI(
        playlists={
            "pl.u-Ympg5s39LRqp": playlist_payload(
                "pl.u-Ympg5s39LRqp", name="bocchi", curator="Apple Music",
                tracks=playlist_entries, paginated=True,
            )
        },
        playlist_tracks={"pl.u-Ympg5s39LRqp": playlist_entries + page_two},
    )
    leaves = await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=fake)

    assert len(leaves) == 105, f"{len(leaves)} leaves for 105 served entries"
    # The five repeats are the *last five* of the result, which is the discriminator: a
    # loop that de-duplicated would have 100 leaves and no tail.
    assert [leaf.adam_id for leaf in leaves[100:]] == [
        "p000", "p001", "p002", "p003", "p004",
    ]

    # -- album: the same shape at ALBUM_TRACK_PAGE_SIZE. `trackCount` is left out on
    #    purpose -- it is 300, the first page would satisfy it, and the second page
    #    would never be fetched. The short-page exit is what ends this walk.
    album_entries = [
        track_payload(f"t{i:03d}", name=f"track {i}")
        for i in range(ALBUM_TRACK_PAGE_SIZE)
    ]
    album_page_two = album_entries[:5]

    class _RepeatsAcrossPages(FakeWebAPI):
        async def get_album_tracks(self, album_id, storefront, lang, offset=0):
            self.calls.append(("album_tracks", album_id, storefront, lang, offset))
            from src.models import AlbumTracks

            page = album_entries if offset == 0 else album_page_two
            return AlbumTracks.model_validate({"next": "x", "data": page}).data or []

    album_meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=None, tracks=[], paginated=True,
    )
    album_client = _RepeatsAcrossPages(
        albums={"1688539265": album_meta}, tracks={"1688539265": []}
    )
    album_leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=album_client)

    assert len(album_leaves) == ALBUM_TRACK_PAGE_SIZE + 5
    assert [leaf.adam_id for leaf in album_leaves[ALBUM_TRACK_PAGE_SIZE:]] == [
        "t000", "t001", "t002", "t003", "t004",
    ]
    assert [c[-1] for c in album_client.calls if c[0] == "album_tracks"] == [
        0, ALBUM_TRACK_PAGE_SIZE,
    ]


async def test_a_page_identical_to_its_predecessor_is_discarded_not_doubled():
    """The other half of the fix, and the reason exit (2) is keyed on the page.

    A client that ignores `offset` returns the *same* page for ever, and that page is
    composed entirely of entries already collected. The walk stops -- and stops
    *without* appending it, because the container was already complete and keeping the
    entries would double it. This is the distinction that a running-id-set check cannot
    make: the 5-repeated page in the test above and this 300-identical page are both
    "all ids already seen", and only one of them is a repeat worth keeping.
    """
    tracks = [track_payload(f"t{i:03d}", name=f"track {i}") for i in range(ALBUM_TRACK_PAGE_SIZE)]
    meta = album_payload(
        "1688539265", name="nameless", artist_name="Eve",
        track_count=None, tracks=tracks, paginated=True,
    )

    class _AlwaysPageZero(FakeWebAPI):
        async def get_album_tracks(self, album_id, storefront, lang, offset=0):
            self.calls.append(("album_tracks", album_id, storefront, lang, offset))
            from src.models import AlbumTracks

            return AlbumTracks.model_validate({"next": "x", "data": tracks}).data or []

    fake = _AlwaysPageZero(albums={"1688539265": meta}, tracks={"1688539265": tracks})
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        leaves = await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)
    finally:
        logger.remove(sink_id)

    assert len(leaves) == ALBUM_TRACK_PAGE_SIZE, "the repeated page was appended, doubling it"
    assert [c[-1] for c in fake.calls if c[0] == "album_tracks"] == [0, ALBUM_TRACK_PAGE_SIZE]
    assert any("already served" in m for m in messages), messages


async def test_a_track_with_no_id_is_refused_rather_than_dropped():
    """A leaf with no `adam_id` is not a leaf; it is a track that is never downloaded.

    `JobStore._check_key` raises on an empty `adam_id` at enqueue time, so dropping it
    silently here would turn one malformed datum into a track the user was told about
    and never receives -- the failure mode the rest of this file is arranged against.
    So the whole expansion fails, loudly, naming the track.
    """
    tracks = [track_payload("t1", name="first"), track_payload("", name="broken")]
    meta, by_id = album_case("1688539265", name="nameless", artist_name="Eve", tracks=tracks)
    fake = FakeWebAPI(albums={"1688539265": meta}, tracks=by_id)

    with pytest.raises(ResolveError, match="broken"):
        await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)


async def test_an_empty_album_is_no_leaves_and_not_an_error():
    meta, by_id = album_case("1688539265", name="nameless", artist_name="Eve", tracks=[])

    assert await expand(ALBUM_URL, codec="alac", language="ja",
                        web_api=FakeWebAPI(albums={"1688539265": meta}, tracks=by_id)) == []


async def test_an_album_the_lookup_found_nothing_for_is_an_error():
    """Distinct from an empty *album*: there is no album here at all, and `[]` would
    report "that album has no tracks", which is a different and wrong statement."""
    fake = FakeWebAPI(albums={"1688539265": {"data": []}})

    with pytest.raises(ResolveError, match="no album"):
        await expand(ALBUM_URL, codec="alac", language="ja", web_api=fake)


# --------------------------------------------------------------------------- #
# A playlist: the order, and the two fields with no album behind them
# --------------------------------------------------------------------------- #
# Out of order on purpose -- alphabetical, then by track number, would each "fix" this.
PLAYLIST_TRACKS = [
    track_payload("p3", name="third", album_name="an album", artist_name="an artist"),
    track_payload("p1", name="first", album_name="an album", artist_name="an artist"),
    track_payload("p2", name="second", album_name="another album", artist_name="another artist"),
]


def playlist_fake(**kwargs) -> FakeWebAPI:
    kwargs.setdefault("tracks", PLAYLIST_TRACKS)
    return FakeWebAPI(
        playlists={
            "pl.u-Ympg5s39LRqp": playlist_payload(
                "pl.u-Ympg5s39LRqp", name="bocchi", curator="Apple Music", **kwargs
            )
        },
        playlist_tracks={"pl.u-Ympg5s39LRqp": PLAYLIST_TRACKS},
    )


async def test_playlist_preserves_upstream_order():
    """The order the user queued, which is the order `playlist_write_song_index`
    (`src/utils.py`) writes a 1-based index for.

    Nothing in the resolver may reorder this. Sorting it is not a cosmetic choice: the
    index the TUI writes into filenames would no longer match the position the track
    occupies in the queue, and every file of a shuffled playlist would be misfiled.
    """
    leaves = await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=playlist_fake())

    assert [leaf.adam_id for leaf in leaves] == ["p3", "p1", "p2"]
    assert [leaf.title for leaf in leaves] == ["third", "first", "second"]


async def test_a_playlist_may_hold_the_same_track_twice():
    """Not a duplicate to be removed -- two entries in a user's queue.

    De-duplicating inside `expand` would be tempting and wrong twice over: the queue
    already has a unique index for "this track is already being fetched", and a leaf
    the resolver dropped is a track the user will never see queued. A playlist that
    lists a song twice must produce two leaves and let
    `job_active_dedupe` report the second as `deduplicated`.
    """
    tracks = [
        track_payload("p1", name="first", album_name="an album", artist_name="an artist"),
        track_payload("p1", name="first", album_name="an album", artist_name="an artist"),
    ]
    fake = FakeWebAPI(
        playlists={
            "pl.u-Ympg5s39LRqp": playlist_payload(
                "pl.u-Ympg5s39LRqp", name="bocchi", curator="Apple Music", tracks=tracks
            )
        }
    )

    leaves = await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=fake)

    assert [leaf.adam_id for leaf in leaves] == ["p1", "p1"]


async def test_playlist_album_and_artist_come_from_the_track_itself():
    """There is no album lookup behind a playlist, so the track's own `albumName` and
    `artistName` are the only source -- and per-track, not one value for the playlist.

    A playlist is the case where the two genuinely differ track to track, which is why
    a single playlist-level album name would be a lie for every track but the first.
    """
    leaves = await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=playlist_fake())

    assert [leaf.album_name for leaf in leaves] == [
        "an album", "an album", "another album",
    ]
    assert [leaf.artist_name for leaf in leaves] == [
        "an artist", "an artist", "another artist",
    ]


async def test_a_playlist_never_looks_an_album_up():
    """One lookup, not one per track: a 500-track playlist is not 500 album lookups.

    This is the assertion that would catch a "let me fetch the album so the name is
    right" change, which is how a playlist expansion turns into 500 requests.
    """
    fake = playlist_fake()

    await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=fake)

    assert fake.method_names() == ["playlist_info"]


async def test_playlist_pagination_keeps_asking_when_the_lookup_only_has_page_one():
    tracks = [
        track_payload(f"p{index}", name=f"track {index}", album_name="a", artist_name="b")
        for index in range(PLAYLIST_TRACK_PAGE_SIZE + 5)
    ]
    fake = FakeWebAPI(
        playlists={
            "pl.u-Ympg5s39LRqp": playlist_payload(
                "pl.u-Ympg5s39LRqp", name="bocchi", curator="Apple Music",
                tracks=tracks[:PLAYLIST_TRACK_PAGE_SIZE], paginated=True,
            )
        },
        playlist_tracks={"pl.u-Ympg5s39LRqp": tracks},
    )

    leaves = await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=fake)

    assert len(leaves) == PLAYLIST_TRACK_PAGE_SIZE + 5
    assert leaves[0].adam_id == "p0"
    assert leaves[-1].adam_id == f"p{PLAYLIST_TRACK_PAGE_SIZE + 4}"


async def test_an_empty_playlist_is_no_leaves_and_not_an_error():
    fake = FakeWebAPI(
        playlists={
            "pl.u-Ympg5s39LRqp": playlist_payload(
                "pl.u-Ympg5s39LRqp", name="bocchi", curator="Apple Music", tracks=[]
            )
        }
    )

    assert await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=fake) == []


async def test_a_music_video_inside_a_playlist_is_flagged_for_the_widevine_path():
    """Best-effort, and asymmetric on purpose.

    Apple labels a playlist entry `music-videos` rather than `songs`, and that is the
    only signal a playlist track carries. A false negative costs a loud failure at
    `run_music_video` time ("this is not a music video"); a false positive would be the
    same loud failure, from the other direction. Neither is silent, and an audio track
    can never be labelled `music-videos`, so the check cannot misroute a FairPlay
    track into Widevine. `Leaf.is_music_video` selects the decryption path, which is
    why it is worth checking at all.

    What it must never be is a *guess*: this reads upstream's own label and nothing
    else, and the assertion that the audio entry is still `False` keeps it that way.
    """
    tracks = [
        track_payload("p1", name="a song"),
        track_payload("v1", name="a video", kind="music-videos"),
    ]
    fake = FakeWebAPI(
        playlists={
            "pl.u-Ympg5s39LRqp": playlist_payload(
                "pl.u-Ympg5s39LRqp", name="bocchi", curator="Apple Music", tracks=tracks
            )
        }
    )

    leaves = await expand(PLAYLIST_URL, codec="alac", language="ja", web_api=fake)

    assert [leaf.is_music_video for leaf in leaves] == [False, True]


# --------------------------------------------------------------------------- #
# An artist
# --------------------------------------------------------------------------- #
async def test_an_artist_expands_to_its_albums_and_then_to_their_tracks():
    second_album = "https://music.apple.com/jp/album/second/1688539275"
    first_meta, first_tracks = album_case(
        "1688539265", name="nameless", artist_name="Eve", tracks=ALBUM_TRACKS
    )
    second_meta, second_by_id = album_case(
        "1688539275", name="second", artist_name="Eve",
        tracks=[track_payload("s1", name="only")],
    )
    fake = FakeWebAPI(
        albums={"1688539265": first_meta, "1688539275": second_meta},
        tracks={**first_tracks, **second_by_id},
        artist_albums={"1688539273": [ALBUM_URL, second_album]},
    )

    leaves = await expand(ARTIST_URL, codec="alac", language="ja", web_api=fake)

    assert [leaf.adam_id for leaf in leaves] == ["t1", "t2", "s1"]
    assert [leaf.album_name for leaf in leaves] == ["nameless", "nameless", "second"]
    # Each album's own storefront, not the artist URL's, so a storefront that differs
    # per release is queried where it exists.
    assert fake.calls[0] == ("artist_albums", "1688539273", "jp", "ja", 0)


async def test_an_artist_with_no_albums_is_no_leaves():
    fake = FakeWebAPI(artist_albums={"1688539273": []})

    assert await expand(ARTIST_URL, codec="alac", language="ja", web_api=fake) == []


async def test_an_artist_listing_a_url_that_is_not_one_is_refused():
    """A catalogue response the hub cannot read is not something to skip quietly --
    the tracks under it would be silently absent from the queue."""
    fake = FakeWebAPI(artist_albums={"1688539273": ["https://example.invalid/not-apple"]})

    with pytest.raises(ResolveError, match="not an Apple Music URL"):
        await expand(ARTIST_URL, codec="alac", language="ja", web_api=fake)


# --------------------------------------------------------------------------- #
# What gets refused, and what is never forwarded
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://music.apple.com/jp/album/nameless-name-single/1688539265", id="http"),
        pytest.param("file:///etc/passwd", id="file"),
        pytest.param("ftp://music.apple.com/jp/album/x/1", id="ftp"),
        pytest.param("HTTPS://music.apple.com/jp/album/x/1", id="uppercase-scheme"),
        pytest.param("  ", id="blank"),
        pytest.param("/jp/album/nameless-name-single/1688539265", id="no-scheme"),
    ],
)
async def test_a_non_https_url_is_refused_and_never_forwarded(url):
    """The scheme allowlist, and the assertion that it runs *before* anything else.

    `parse_url` checks the host with a regex and not the scheme, so `http://` and
    `file://` would both be handed to `get_real_url` -- which is a request to whatever
    the string names. The hub reaches this from a browser, so that is an
    operator-controlled fetch of a caller-supplied string, and `calls == []` is what
    proves it does not happen.
    """
    fake = FakeWebAPI()

    with pytest.raises(ResolveError):
        await expand(url, codec="alac", language="ja", web_api=fake)

    assert fake.calls == []


async def test_a_url_with_surrounding_whitespace_is_stripped_before_anything_else():
    """A paste is the likely source, and a trailing space survives `urlparse` into
    `paths[-1]` -- so the id would be `"1688539265 "` and the whole thing would be a
    404 the user cannot act on."""
    leaves = await expand(f"  {SONG_SHARE_URL}\n", codec="alac", language="ja",
                          web_api=song_fake())

    assert leaves[0].adam_id == "1688539274"
    assert leaves[0].url == SONG_SHARE_URL


async def test_a_non_apple_host_is_refused_without_a_request():
    """`https://example.com/album/1` is not an Apple Music URL, and the resolver must
    say so rather than GET it to find out.

    Following the redirect is only for a URL that is recognisably Apple's -- a legacy
    `itunes.apple.com` share link, which the strict regex in `parse_url` does not
    match. Any other host would make `POST /api/jobs` a fetcher for arbitrary URLs.
    """
    fake = FakeWebAPI()

    with pytest.raises(ResolveError, match="not an Apple Music URL"):
        await expand("https://example.com/album/1", codec="alac", language="ja", web_api=fake)

    assert fake.calls == []


async def test_a_legacy_itunes_link_is_followed_to_the_canonical_one():
    """The one case where a request before the refusal is correct.

    Pre-2015 share links are `itunes.apple.com`, which redirects to the equivalent
    `music.apple.com` URL. Upstream resolves them the same way (`cmd.py:310` and
    `:347`), and the leaf carries the *canonical* URL, not the pasted one -- which is
    deliberate, because the pasted form is not the URL the rest of the client expects
    to see and there is nothing in it that `parse_url` could not read.
    """
    legacy = "https://itunes.apple.com/jp/album/nameless-name-single/1688539265?i=1688539274"
    fake = song_fake(real_urls={legacy: SONG_SHARE_URL})

    leaves = await expand(legacy, codec="alac", language="ja", web_api=fake)

    assert fake.calls == [
        ("real_url", legacy), ("song_info", "1688539274", "jp", "ja"),
    ]
    assert [leaf.adam_id for leaf in leaves] == ["1688539274"]
    assert leaves[0].url == SONG_SHARE_URL
    assert leaves[0].storefront == "jp"
    # And the song lookup that follows the redirect is described, not blank.
    assert leaves[0].title == "nameless"


async def test_an_apple_host_that_resolves_to_nothing_is_refused():
    fake = FakeWebAPI(real_urls={"https://music.apple.com/jp/": "https://music.apple.com/jp/"})

    with pytest.raises(ResolveError, match="not an Apple Music URL"):
        await expand("https://music.apple.com/jp/", codec="alac", language="ja", web_api=fake)

    assert fake.calls == [("real_url", "https://music.apple.com/jp/")]


# --------------------------------------------------------------------------- #
# The rules the rest of the system depends on
# --------------------------------------------------------------------------- #
async def test_the_storefront_comes_from_the_url_not_from_a_hardcoded_default():
    """I3, and the reason every other URL in this file being `/jp/` was a hazard.

    `storefront` is one of the nine pinned fields and it decides which catalogue is
    queried, so a wrong one does not fail -- it returns a *different album with the same
    id*, or none at all. Round 0 hardcoded nothing but every fixture was `/jp/`, so
    `storefront="jp"` written into `_album_leaves` passed the whole suite.

    Two assertions, because the lookup argument and the leaf are separate decisions: the
    request must go to the URL's storefront, and the leaves must carry it.
    """
    us_tracks = [track_payload("u1", name="only")]
    meta, by_id = album_case(
        "1688539265", name="nameless", artist_name="Eve", tracks=us_tracks, storefront="us"
    )
    fake = FakeWebAPI(albums={"1688539265": meta}, tracks=by_id)

    leaves = await expand(ALBUM_URL_US, codec="alac", language="en-US", web_api=fake)

    assert fake.calls[0] == ("album_info", "1688539265", "us", "en-US")
    assert {leaf.storefront for leaf in leaves} == {"us"}


async def test_each_album_of_an_artist_is_queried_in_its_own_storefront():
    """A release's storefront can differ from the artist's, and each album is asked in its
    own -- which is why the artist dispatch re-parses every album URL rather than
    stamping the artist URL's storefront onto the lot.

    Round 0 asserted only that the *first* call was the artist lookup, and with both
    albums at `/jp/` the per-album storefront was never checked by anything.
    """
    jp_meta, jp_by_id = album_case(
        "1688539265", name="nameless", artist_name="Eve", tracks=ALBUM_TRACKS,
        storefront="jp",
    )
    us_meta, us_by_id = album_case(
        "1688539275", name="second", artist_name="Eve",
        tracks=[track_payload("s1", name="only")], storefront="us",
    )
    fake = FakeWebAPI(
        albums={"1688539265": jp_meta, "1688539275": us_meta},
        tracks={**jp_by_id, **us_by_id},
        artist_albums={"1688539273": [ALBUM_URL, ALBUM_URL_US.replace("1688539265", "1688539275")]},
    )

    leaves = await expand(ARTIST_URL, codec="alac", language="ja", web_api=fake)

    album_calls = [c for c in fake.calls if c[0] == "album_info"]
    assert album_calls == [
        ("album_info", "1688539265", "jp", "ja"),
        ("album_info", "1688539275", "us", "ja"),
    ]
    assert [leaf.storefront for leaf in leaves] == ["jp", "jp", "us"]


@pytest.mark.parametrize(
    "host_url",
    [
        # The one the resolver's own docstring names as the reason the gate exists:
        # a naive `host.endswith(suffix)` accepts it, and it is not Apple.
        pytest.param("https://music.apple.com.attacker.example/jp/album/x/1", id="suffix-attack"),
        pytest.param("https://notmusic.apple.com.attacker.example/x", id="double-suffix"),
        # Userinfo. `urlparse().hostname` drops everything before the `@`, so a naive
        # read of `netloc` would see `music.apple.com@evil.example` and pass it.
        pytest.param("https://music.apple.com@evil.example/jp/album/x/1", id="userinfo"),
        pytest.param("https://evil.example/?x=music.apple.com", id="query-only"),
        # Punycode that decodes to a lookalike.
        pytest.param("https://xn--music-apple-3vc.com/jp/album/x/1", id="punycode"),
        pytest.param("https://music.apple.como/jp/album/x/1", id="one-char-longer"),
        pytest.param("https://apple.com/music.apple.com", id="path-only"),
        pytest.param("https://music.apple.com.cn/jp/album/x/1", id="tld-swallow"),
        # -- these three are the only cases that separate the whole-host match from a
        # bare `host.endswith(suffix)`, which is the single most natural simplification
        # anyone would make at that line and which the review confirmed survived the
        # round-0 suite. A host that *ends with* the literal suffix but has no dot in
        # front of it is the whole difference between the two forms.
        #
        # Being honest about what that is worth: every one of them is a subdomain of
        # `apple.com`, so they are Apple-controlled and none of them is a live SSRF
        # bypass today. What they pin is that the comparison is a whole-host match, so
        # that the day the suffix list gains an entry Apple does *not* own -- a
        # CNAME'd shortener, a vanity domain -- the looser form cannot already be in
        # place and passing. `notmusic.apple.com` is the shape such an entry takes.
        pytest.param("https://notmusic.apple.com/jp/album/x/1", id="no-dot-before-suffix"),
        pytest.param("https://xmusic.apple.com/jp/album/x/1", id="no-dot-before-suffix-2"),
        pytest.param("https://notitunes.apple.com/jp/album/x/1", id="no-dot-before-itunes"),
    ],
)
def test_the_redirect_gate_refuses_every_host_that_is_not_apples(host_url):
    """I4: the comparison is a whole-host match, and that is what has to be pinned.

    The allowlist is correct and, as the review verified, unbypassable through
    `urlparse().hostname` -- which strips userinfo, strips the port and lowercases.
    But replacing the two-clause match with a bare `host.endswith(suffix)` **survived
    with the whole suite green**, because every URL in the file was a real Apple one.

    The cases that separate the two forms are the last three, and they are the ones that
    matter: a host that ends with the literal suffix with no dot in front of it. Each is
    a *rejection*, asserted through the real `expand`, and each is one a naive `endswith`
    would accept. `calls == []` is the load-bearing half: without the gate being before
    the request, a refused host that had been fetched would still raise a `ResolveError`
    and still pass.
    """
    import asyncio

    fake = FakeWebAPI()
    with pytest.raises(ResolveError, match="not an Apple Music URL"):
        asyncio.run(expand(host_url, codec="alac", language="ja", web_api=fake))
    assert fake.calls == [], f"{host_url} was fetched"


@pytest.mark.parametrize(
    "url",
    [
        # Apple, and the reason the list is suffixes rather than one host: pre-2015
        # share links are `itunes.apple.com` and upstream resolves them the same way.
        # Each is a URL `parse_url` *rejects* -- a bare host, or one with a port or in
        # a case the regex does not match -- so a `get_real_url` request is actually
        # spent, and that request is the assertion.
        pytest.param("https://music.apple.com/", id="music"),
        pytest.param("https://itunes.apple.com/", id="itunes"),
        pytest.param("https://embed.music.apple.com/", id="music-subdomain"),
        pytest.param("https://aod.itunes.apple.com/", id="itunes-subdomain"),
        # A port is not a different host, and `hostname` is what drops it. `parse_url`'s
        # regex wants a literal `music.apple.com/`, so this one falls through to the
        # redirect as well.
        pytest.param("https://music.apple.com:8443/jp/album/x/1", id="with-port"),
        # Case: `hostname` lowercases, and DNS is case-insensitive. The regex is
        # case-sensitive, so the upper-case form falls through.
        pytest.param("https://MUSIC.APPLE.COM/", id="uppercase"),
    ],
)
def test_the_redirect_gate_accepts_apples_hosts(url):
    """The other half: a gate that refused everything would pass every case above.

    The URL still ends up a `ResolveError` -- `get_real_url` is faked to return the same
    string, and `parse_url` cannot make anything of it either way. The assertion is that
    a request was *spent* on it, which is the difference between "the host was accepted
    for a redirect" and "the host was refused".
    """
    import asyncio

    fake = FakeWebAPI(real_urls={url: url})
    with pytest.raises(ResolveError, match="not an Apple Music URL"):
        asyncio.run(expand(url, codec="alac", language="ja", web_api=fake))
    assert fake.method_names() == ["real_url"], url


def test_an_unrecognised_url_kind_is_refused_rather_than_expanded(monkeypatch):
    """I5: the guard for a future sixth URL kind, pinned while it is still unreachable.

    Today `parse_url` returns one of five kinds, so this branch cannot fire -- which is
    exactly why it needs a test: the release that adds a sixth kind is the one where the
    suite is least likely to run, and a resolver that quietly expanded it as a song would
    queue a track nothing can fetch. The dispatch is monkeypatchable through
    `resolver.parse_apple_music_url`, so there is no excuse for leaving it bare.

    Also pinned: the error names the kind it did not recognise and the five it knows, so
    whoever hits it is told what to add rather than only that something is wrong.
    """
    class _Podcast:
        url = "https://podcasts.apple.com/jp/podcast/x/1"
        storefront = "jp"
        type = "podcast"
        id = "1"

    monkeypatch.setattr(resolver, "parse_apple_music_url", lambda url: _Podcast())

    with pytest.raises(ResolveError) as excinfo:
        import asyncio

        asyncio.run(expand("https://podcasts.apple.com/jp/podcast/x/1",
                           codec="alac", language="ja", web_api=FakeWebAPI()))

    message = str(excinfo.value)
    assert "podcast" in message
    for kind in ("album", "artist", "music-video", "playlist", "song"):
        assert kind in message


def test_expand_cannot_run_without_an_injected_web_api():
    """Keyword-only and without a default, which is what makes "fall back to building
    one" not a thing that can be written by accident."""
    parameters = inspect.signature(expand).parameters
    web_api = parameters["web_api"]

    assert web_api.default is inspect.Parameter.empty
    assert web_api.kind is inspect.Parameter.KEYWORD_ONLY
    assert set(parameters) == {"url", "codec", "language", "web_api"}


def test_the_resolver_never_constructs_a_web_api():
    """Read from the source rather than inferred from the tests passing.

    Every test here injects a fake, so "no test made a network call" is only evidence
    that *these* tests did not. This is the assertion about the code: no call whose
    name ends in `WebAPI`, and no module-level construction. A convenience wrapper that
    builds one would be invisible to a green suite and would put a real client behind
    `expand` in production.
    """
    tree = ast.parse(RESOLVER_SOURCE.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "WebAPI" or name.endswith("WebAPI"):
            offenders.append(f"line {node.lineno}: {name}()")

    assert offenders == [], offenders


def test_the_resolver_does_not_import_the_modules_it_was_told_not_to():
    """`normalize`, `dedup` and `library_scan` are other tasks' logic, and a resolver
    that imported one would be reimplementing its invariants in a place with no tests
    for them.

    The allowlist is exact rather than a denylist of the three, so that a new *hub*
    import has to be added here on purpose. Two of the seven are hub's own -- `vendor`
    is the route to upstream's parser and `jobs` is what this returns -- and the rest
    are stdlib and loguru: `__future__` and the typing generics are inert,
    `urllib.parse` is the host extraction (the only thing a module whose job is reading
    Apple's URLs should be reaching for), and `loguru` is the one warning this module
    emits, for the page loop's "this may be truncated" exit.
    """
    tree = ast.parse(RESOLVER_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert imported <= {
        "__future__", "collections.abc", "typing", "urllib.parse", "loguru",
        "hub.jobs", "hub.vendor",
    }, imported


def _upstream_api() -> ast.Module:
    """`src/api.py` parsed, not imported.

    **Imported, this module would need the whole creart bootstrap.** `WebAPI`'s class
    body evaluates `it(Config)` inside `@retry(...)` decorator arguments, so
    `from src.api import WebAPI` raises `TypeError: current environment does not
    contain support for src.config:Config` until the six creators are registered --
    which means a chdir into the vendor tree and process-global registrations this
    suite has to be careful about (`test_ripper_host.py` has a whole fixture
    explaining why). Reading the source is enough for what is being checked, which is
    that the *names and parameters* upstream offers are the ones the resolver calls.
    """
    return ast.parse((VENDOR_SRC / "api.py").read_text(encoding="utf-8"))


def _upstream_methods() -> dict[str, ast.AsyncFunctionDef]:
    """Every `async def` on `WebAPI`, by name.

    Scoped to the class rather than to the module so that a helper function with the
    same name elsewhere in `src/api.py` cannot satisfy the check.
    """
    for node in ast.walk(_upstream_api()):
        if isinstance(node, ast.ClassDef) and node.name == "WebAPI":
            return {
                child.name: child
                for child in node.body
                if isinstance(child, ast.AsyncFunctionDef)
            }
    raise AssertionError("src/api.py no longer defines a WebAPI class")


def test_the_real_web_api_still_has_every_method_the_resolver_calls():
    """The contract, pinned against upstream rather than against this file.

    The tests pass a hand-written fake, which agrees with the resolver by
    construction. This asks the real `WebAPI` for each name *and* each parameter list,
    so an upstream rename or a dropped `offset` fails here instead of at the first
    real URL in production -- which is where a `TypeError` from a keyword argument
    nobody type-checks would otherwise land. `self` is dropped: what is being checked
    is the shape the resolver calls, not how the method is bound.
    """
    methods = _upstream_methods()
    expected = {
        "get_album_info": ["album_id", "storefront", "lang"],
        "get_album_tracks": ["album_id", "storefront", "lang", "offset"],
        "get_playlist_info_and_tracks": ["playlist_id", "storefront", "lang"],
        "get_playlist_tracks": ["playlist_id", "storefront", "lang", "offset"],
        "get_albums_from_artist": ["artist_id", "storefront", "lang", "offset"],
        "get_song_info": ["song_id", "storefront", "lang"],
        "get_real_url": ["url"],
    }

    for name, parameters in expected.items():
        method = methods.get(name)
        assert method is not None, f"src.api.WebAPI has no async {name}() any more"
        assert [arg.arg for arg in method.args.args][1:] == parameters, name
        assert method.args.vararg is None and method.args.kwarg is None, name


def test_the_page_sizes_match_what_upstream_actually_requests():
    """300 for album tracks and 100 for playlist tracks, because those are the numbers
    `WebAPI.get_album_tracks` and `get_playlist_tracks` step `offset` by.

    Read out of upstream's own source rather than restated here, because the loop's
    only length-based exit is "a short page is the last page" -- which is a statement
    about the endpoint's page size, and silently wrong if that changes upstream. Read
    as `offset + <int>` in the AST rather than as a substring, so a `+ 30` typo there is
    caught instead of being quietly different from the constant here.
    """
    methods = _upstream_methods()

    for name, expected_step in (
        ("get_album_tracks", ALBUM_TRACK_PAGE_SIZE),
        ("get_playlist_tracks", PLAYLIST_TRACK_PAGE_SIZE),
    ):
        steps = [
            node.right.value
            for node in ast.walk(methods[name])
            if isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and isinstance(node.left, ast.Name)
            and node.left.id == "offset"
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, int)
        ]
        assert steps == [expected_step], f"{name} steps offset by {steps}, not {expected_step}"


@pytest.mark.parametrize(
    ("url", "expected_type", "expected_id"),
    [
        pytest.param(SONG_SHARE_URL, "song", "1688539274", id="song-share-link"),
        pytest.param(SONG_URL, "song", "339592231", id="song"),
        pytest.param(ALBUM_URL, "album", "1688539265", id="album"),
        pytest.param(PLAYLIST_URL, "playlist", "pl.u-Ympg5s39LRqp", id="playlist"),
        pytest.param(ARTIST_URL, "artist", "1688539273", id="artist"),
        pytest.param(VIDEO_URL, "music-video", "1800449196", id="music-video"),
    ],
)
def test_every_readme_url_still_resolves_to_what_it_used_to(url, expected_type, expected_id):
    """The five links `AppleMusicDecrypt/README.md` documents, and the leaf counts the
    report quotes.

    Asserted against upstream's own parser rather than a hand-rolled one, so this is a
    check on the URL vocabulary and not a restatement of the resolver: if a change
    broke the share link into an album, this is the line that says so.
    """
    parsed = parse_apple_music_url(url)

    assert parsed is not None, url
    assert (parsed.type, parsed.id) == (expected_type, expected_id)


async def test_readme_url_leaf_counts():
    """The counts, end to end, against a fake client.

    Song 1, album 2, artist 3 (2 + 1 across two albums), playlist 3, video 1. These are
    the numbers in the task report; pinning them means a change in what one of these
    URLs expands to is a diff in a test rather than something found by a user.
    """
    first_meta, first_tracks = album_case(
        "1688539265", name="nameless", artist_name="Eve", tracks=ALBUM_TRACKS
    )
    second_meta, second_by_id = album_case(
        "1688539275", name="second", artist_name="Eve",
        tracks=[track_payload("s1", name="only")],
    )
    client = FakeWebAPI(
        albums={"1688539265": first_meta, "1688539275": second_meta},
        tracks={**first_tracks, **second_by_id},
        playlists={
            "pl.u-Ympg5s39LRqp": playlist_payload(
                "pl.u-Ympg5s39LRqp", name="bocchi", curator="Apple Music",
                tracks=PLAYLIST_TRACKS,
            )
        },
        artist_albums={"1688539273": [ALBUM_URL, "https://music.apple.com/jp/album/second/1688539275"]},
        songs={
            "1688539274": song_payload(
                "1688539274", name="nameless", album_name="nameless", artist_name="Eve"
            ),
        },
    )

    counts = {}
    for name, url in (
        ("song", SONG_SHARE_URL), ("album", ALBUM_URL), ("playlist", PLAYLIST_URL),
        ("artist", ARTIST_URL), ("music-video", VIDEO_URL),
    ):
        counts[name] = len(await expand(url, codec="alac", language="ja", web_api=client))

    assert counts == {"song": 1, "album": 2, "playlist": 3, "artist": 3, "music-video": 1}
