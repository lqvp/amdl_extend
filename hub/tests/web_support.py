"""Shared fakes, constants and plain helpers for the hub's web tests.

The fixtures that wire them into a running app live in `conftest.py`; the row
markup contract these fakes feed lives in the files that import this one. Kept out
of a test module so that two test modules can share it, and out of conftest so that
reading a fake does not mean reading fixture plumbing.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from hub.jobs import JobStore, Progress
from hub.wrapper_supervisor import LoginChallenge

PASSWORD = "correct horse battery staple"
SECRET = "s" * 32
BASE = "http://hub.test"

# Real Apple Music URL shapes. Upstream's `AppleMusicURL.parse_url` takes `paths[-1]` as the
# id, so the numeric id is the *last* segment and the human-readable slug comes first -- a
# URL written the other way round parses, and then looks the album up by its slug, which is
# why these are shaped like the links a user actually pastes.
ALBUM_URL = "https://music.apple.com/jp/album/4pi/1621491338"
ALBUM2_URL = "https://music.apple.com/jp/album/other-album/1621491339"
MV_URL = "https://music.apple.com/jp/music-video/liar/1440935466"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeWebAPI:
    """The four catalogue methods an album and a song expansion reach.

    `SimpleNamespace` rather than upstream's pydantic models, because `hub.resolver` reads
    attributes defensively (`getattr(..., None) or ""`) and declares its `WebAPI` as a
    `Protocol` returning `Any` precisely so the tests need not import `src.*` -- which the
    boundary test forbids from any file but the two exempt ones.
    """

    def __init__(self) -> None:
        self.songs: dict[str, tuple[str, str, str]] = {}
        self.albums: dict[str, tuple[str, str, list[tuple[str, str]]]] = {}
        self.calls: list[tuple] = []

    def add_song(self, adam_id: str, title: str, album: str, artist: str) -> None:
        self.songs[adam_id] = (title, album, artist)

    def add_album(self, album_id: str, name: str, artist: str, tracks) -> None:
        self.albums[album_id] = (name, artist, list(tracks))

    async def get_song_info(self, song_id: str, storefront: str, lang: str):
        self.calls.append(("song_info", song_id, storefront, lang))
        title, album, artist = self.songs[song_id]
        return SimpleNamespace(
            data=[SimpleNamespace(id=song_id, attributes=SimpleNamespace(name=title, albumName=album, artistName=artist))]
        )

    async def get_album_info(self, album_id: str, storefront: str, lang: str):
        self.calls.append(("album_info", album_id, storefront, lang))
        name, artist, tracks = self.albums[album_id]
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    id=album_id,
                    attributes=SimpleNamespace(name=name, artistName=artist, trackCount=len(tracks)),
                    relationships=SimpleNamespace(
                        tracks=SimpleNamespace(
                            data=[
                                SimpleNamespace(
                                    id=track_id,
                                    type="songs",
                                    attributes=SimpleNamespace(name=title, url=track_url),
                                )
                                for track_id, title, track_url in tracks
                            ]
                        )
                    ),
                )
            ]
        )

    async def get_album_tracks(self, album_id: str, storefront: str, lang: str, offset: int = 0):
        self.calls.append(("album_tracks", album_id, storefront, lang, offset))
        return SimpleNamespace(data=[])

    async def get_playlist_info_and_tracks(self, playlist_id, storefront, lang):
        self.calls.append(("playlist_info", playlist_id, storefront, lang))
        return SimpleNamespace(data=[])

    async def get_playlist_tracks(self, playlist_id, storefront, lang, offset: int = 0):
        self.calls.append(("playlist_tracks", playlist_id, storefront, lang, offset))
        return SimpleNamespace(data=[])

    async def get_albums_from_artist(self, artist_id, storefront, lang, offset: int = 0):
        self.calls.append(("artist_albums", artist_id, storefront, lang, offset))
        return SimpleNamespace(data=[])

    async def get_real_url(self, url: str) -> str:
        self.calls.append(("real_url", url))
        return url


class FakeSupervisor:
    """`WrapperSupervisor` without the child process.

    `regions` is the field that matters: `[]` is the state a fresh install boots into, and
    the two ways of being unready have to stay distinguishable all the way to the page.
    """

    def __init__(self, regions: list[str] | None = None) -> None:
        self.regions = ["jp"] if regions is None else regions
        self.started = False
        self.adopted = False
        self._running = False
        self.pid = 4242
        self.bound_port = 12340
        self.start_error: Exception | None = None
        self.challenge: LoginChallenge | None = None
        self.login_error: Exception | None = None
        #: Whether submitting a code puts the account on disk. False models a rejected
        #: attempt: the child exits, the token cache stays as it was, and the restarted
        #: wrapper still has no regions.
        self.accepts_2fa = True
        self.credentials: tuple[str, str] | None = None
        self.submitted: list[tuple[str, str]] = []
        self.log: list[str] = []
        self.login_gate: object | None = None

    async def start(self) -> None:
        self.log.append("start")
        if self.start_error is not None:
            raise self.start_error
        self._running = True
        self.started = True

    async def stop(self) -> None:
        self.log.append("stop")
        self._running = False

    async def status(self) -> dict:
        if not self._running:
            raise RuntimeError("no wrapper is running to ask")
        return {"regions": list(self.regions)}

    async def login(self, username: str, password: str) -> LoginChallenge:
        self.log.append("login")
        self.credentials = (username, password)
        if self.login_error is not None:
            raise self.login_error
        if self.login_gate is not None:
            await self.login_gate
        self.challenge = LoginChallenge(id="chal-1", expires_at=0.0)
        return self.challenge

    async def submit_2fa(self, challenge_id: str, code: str) -> None:
        self.log.append("submit_2fa")
        self.submitted.append((challenge_id, code))
        # Submitting the code is what puts the account on disk, so the regions appear here
        # and not in `login()`. The real payload is the same: the login child caches the
        # token and returns, and the serving child reads it when it next starts.
        if self.accepts_2fa:
            self.regions = ["jp"]

    @property
    def running(self) -> bool:
        return self._running


class FakeRipper:
    """`RipperHost` without the client, the creart registration or the chdir.

    `render_song_filename` is the interesting one: the *real* method needs `it(Config)` and
    `src.utils`, so a fake is the only way to keep this suite off the vendor tree. It
    reproduces the one behaviour the tests depend on -- the default `songNameFormat` of
    ``{disk}-{tracknum:02d} {title}`` plus `get_suffix` -- and the test that matters
    (`test_the_dedup_check_is_given_the_rendered_file_name`) fails if the *caller* stops
    asking for it, which is the direction the bug would come from.
    """

    def __init__(self, web_api: FakeWebAPI) -> None:
        self._web_api = web_api
        #: Stands for a *started* host, which is what the app normally has by the time a
        #: request arrives. Set to `None` to model the 503-shaped case where the client never
        #: came up and the API has to ask the caller for a language instead of inventing one.
        self.language: str | None = "ja"
        self.started = False
        self.closed = False
        self.songs: list[tuple] = []
        self.videos: list[tuple] = []
        self.rip_error: Exception | None = None
        self.in_flight = 0
        self.peak_in_flight = 0
        #: Reported to whoever asked, as the real host's `on_progress` callback is. The real
        #: seam *polls* upstream's `Task`; this fake is handed the numbers directly, so a test
        #: says exactly which bytes arrived when -- which a poll could not, without sleeping.
        self.reports: list[tuple[int, Progress]] = []
        #: When set, `run_song` reports progress up to this many bytes before returning.
        self.progress_up_to: int | None = None

    @property
    def web_api(self):
        return self._web_api

    @property
    def region_language(self) -> str | None:
        return self.language

    async def start(self) -> None:
        # creart's `WrapperClient` is process-global and cannot be evicted, so a `start()`
        # after a `close()` would hand back an `aclose()`d client. Modelled here so that
        # "the app does not start a second host" is observable rather than assumed.
        if self.closed:
            raise RuntimeError(
                "this process already closed the client; creart cannot re-create it, so a "
                "new host would hold a closed one"
            )
        self.started = True

    async def close(self) -> None:
        if self.in_flight:
            # The real `RipperHost.close()` refuses here, and the reason is load-bearing:
            # it holds the process working directory, which upstream resolves
            # `EMBEDDED_TEMPLATE_PATH` and `download.dirPathFormat` against, so restoring
            # it under a running rip loses the FairPlay template and writes to the wrong
            # tree with no error. A fake that closed anyway would make the shutdown-ordering
            # test pass without the ordering.
            raise RuntimeError(
                f"refusing to close: {self.in_flight} rip(s) still in flight"
            )
        self.closed = True

    def render_song_filename(self, leaf, *, track_number: int = 1) -> str:
        name = f"1-{track_number:02d} {leaf.title}"
        return name + {}.get(leaf.codec, ".m4a")

    async def run_song(self, leaf, *, force: bool) -> None:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        self.songs.append((leaf, force))
        try:
            if self.progress_up_to:
                for done in (0, self.progress_up_to // 2, self.progress_up_to):
                    self._report(leaf, done, self.progress_up_to)
                    # A yield *after* each reading, and it is load-bearing. The real seam's
                    # poll runs on a worker every `PROGRESS_INTERVAL` (0.1 s) while a real rip
                    # takes minutes, so readings *are* separated by real time, and the handler
                    # defers its write to the loop with `call_soon_threadsafe`. Without this
                    # the last reading's write is still queued when `run_song` returns, and the
                    # frame order becomes `0, half, done` -- the final byte count never reaching
                    # the stream, which is the one reading a user actually waits for.
                    await asyncio.sleep(0)
            if self.rip_error is not None:
                raise self.rip_error
        finally:
            self.in_flight -= 1

    async def run_music_video(self, leaf, *, force: bool) -> None:
        self.videos.append((leaf, force))

    def _report(self, leaf, done: int, total: int | None) -> None:
        """One reading, the way the real seam would deliver it.

        The real `RipperHost` polls upstream's `Task` and calls `on_progress` from a worker;
        `hub.app`'s handler hops to the loop with `call_soon_threadsafe`. A fake cannot
        reproduce the thread hop without being a fake of the hop, so it calls the handler
        directly -- and `test_progress_reaches_the_store_and_the_stream` therefore covers the
        handler, while the real poll is exercised by the acceptance run.
        """
        reading = Progress(
            bytes_done=done,
            bytes_total=total,
            fraction=(done / total) if total else None,
        )
        self.reports.append((leaf.adam_id, reading))
        handler = getattr(self, "on_progress", None)
        if handler is not None:
            handler(reading)

    async def wrapper_status(self) -> dict:
        return {"regions": ["jp"]}


async def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)



def _store(settings) -> JobStore:
    return JobStore(settings.db_path)



def _expansion_with_three_usable_leaves():
    """Three distinct tracks, so a bulk operation has three rows of its own to act on.

    One POST of the default fixture's album makes a single job, and posting the same URL
    again folds into it, so a test about a *set* of rows needs an expansion that produces
    a set. Mirrors `_expansion_with_one_unusable_leaf` for the same reason: patching the
    name the handler imported keeps the handler from reaching the real resolver.
    """
    from hub.jobs import Leaf

    async def fake_expand(url, *, codec, language, web_api):
        return [
            Leaf(adam_id=str(n), title=f"track {n}", album_name="4pi", artist_name="toe",
                 codec=codec, language=language, url=url, storefront="jp")
            for n in (1, 2, 3)
        ]

    return fake_expand


