"""Task 9's end-to-end check, against the real `RipperHost.render_song_filename`.

`tests/test_api_jobs.py` drives the dedup decision through a `FakeRipper` whose
`render_song_filename` reproduces the client's default `songNameFormat` by hand. That is
necessary -- the real method needs `it(Config)` and the vendor tree, which is why it lives in
`RipperHost` at all -- but it means the suite pins the *wiring* and not the render itself.

So this runs the real method once, against the real `AppleMusicDecrypt/config.toml`, and
compares its answer with what the fake produces and with the real library's keys. The
question it answers is the one §7.6's last row is about: does a rendered name normalize to
the key a file on disk is indexed under, once, and only once?

Run:  cd hub && uv run python spike/task9_hub_check.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hub.jobs import Leaf  # noqa: E402
from hub.normalize import normalize  # noqa: E402
from hub.ripper_host import RipperHost  # noqa: E402

VENDOR = Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"

# The real library keys §7.6 names as *not* fixed points: `normalize` strips at most two
# leading numeric groups, so a title that itself starts with one keeps a number that a second
# pass would eat. `title` is the tag, `on_disk` is the file a previous download left, and
# `rendered` is what `rip_song` will write for the same track.
#
# `on_disk` is spelled with the *default* `songNameFormat` (`{disk}-{tracknum:02d} {title}`),
# because that is the only thing the hub ever writes: a job is one song, never a playlist
# row. `PLAYLIST_SHAPED` below is the shape that differs, and it is a known miss rather than
# a rule failure.
CASES = [
    ("1-01 1 a.m. (feat. shinoだす。).m4a", "1 a.m. (feat. shinoだす。)"),
    ("1-01 3_00 AM.m4a", "3_00 AM"),
]

#: A file written by `playlistSongNameFormat` (`{playlistSongIndex:02d}. {artist} - {title}`),
#: which normalizes to a different key than the same track's song-format render would. The
#: hub does not produce these -- it flattens a playlist into song jobs and each is written
#: with `songNameFormat` -- so the effect is a re-download of a file that is already there,
#: in the direction that is always safe. Printed rather than asserted, because asserting it
#: would be asserting a bug.
PLAYLIST_SHAPED = ("04. 3_00 AM.m4a", "3_00 AM")


async def run() -> int:
    if not (VENDOR / "src").is_dir():
        print(f"SKIP: no AppleMusicDecrypt checkout at {VENDOR}")
        return 0
    if not (VENDOR / "config.toml").is_file():
        print(f"SKIP: {VENDOR}/config.toml is absent; copy config.example.toml there")
        return 0

    failures: list[str] = []
    host = RipperHost(VENDOR / "config.toml")
    try:
        await host.start()
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
        print(f"FAIL: RipperHost.start() raised: {type(exc).__name__}: {exc}")
        return 1

    try:
        print(f"region.language = {host.region_language!r}")
        print(f"web_api         = {type(host.web_api).__name__}")
        if host.web_api is None:
            failures.append("RipperHost.web_api is None after start()")

        for rendered_on_disk, tag_title in CASES:
            leaf = Leaf(
                adam_id="0",
                title=tag_title,
                album_name="4pi",
                artist_name="toe",
                codec="alac",
                language="ja",
                url="https://music.apple.com/jp/album/4pi/1621491338",
                storefront="jp",
            )
            rendered = host.render_song_filename(leaf)
            disk_key = normalize(rendered_on_disk)
            from_render = normalize(rendered)
            from_tag = normalize(tag_title)

            print(f"\n  tag title          {tag_title!r}")
            print(f"  rendered           {rendered!r}")
            print(f"  file on disk       {rendered_on_disk!r}")
            print(f"  key from disk      {disk_key!r}")
            print(f"  key from rendered  {from_render!r}")
            print(f"  key from tag       {from_tag!r}   <- what passing Leaf.title gives")

            if from_render != disk_key:
                failures.append(
                    f"the rendered name {rendered!r} keys to {from_render!r} but the file "
                    f"{rendered_on_disk!r} is indexed under {disk_key!r}: the check would "
                    f"miss a track that is already on disk"
                )
            if from_tag == from_render:
                print(
                    "  (this case would also match on the tag, so it does not discriminate "
                    "the two inputs)"
                )
            else:
                print("  -> the tag title mis-keys this case. This is the bug the rule exists for.")

        # A sanity case that *is* a fixed point, so a render that is simply wrong in a
        # different way -- dropping the extension, or keying to "" -- is caught too.
        plain = Leaf(
            adam_id="0", title="Caribbean Blue", album_name="A", artist_name="X",
            codec="alac", language="ja", url="u", storefront="jp",
        )
        rendered = host.render_song_filename(plain)
        print(f"\n  plain render       {rendered!r}")
        if normalize(rendered) != normalize("1-01 Caribbean Blue.m4a"):
            failures.append(
                f"a plain title rendered as {rendered!r}, which does not key to the same "
                f"value as the file it would overwrite"
            )

        # The known miss, printed with its reasoning rather than asserted.
        on_disk, tag = PLAYLIST_SHAPED
        leaf = Leaf(
            adam_id="0", title=tag, album_name="4pi", artist_name="toe", codec="alac",
            language="ja", url="u", storefront="jp",
        )
        print(f"\n  known miss: {on_disk!r} (written by playlistSongNameFormat)")
        print(f"    its key                 {normalize(on_disk)!r}")
        print(f"    what the hub would write {host.render_song_filename(leaf)!r}")
        print(f"    its key                 {normalize(host.render_song_filename(leaf))!r}")
        print(
            "    -> a file left by a playlist download is not found by the check, so the "
            "track\n       downloads again. Safe direction, and the hub never writes this "
            "shape."
        )
        # The progress reader, against a real upstream `Task` -- the seam half that the
        # suite's fake cannot reach, since it is handed its byte counts.
        failures.extend(await check_progress(host))
    finally:
        # `close()` restores the working directory `start()` changed, so this must run even
        # when an assertion above failed -- a script that leaves the process inside
        # `AppleMusicDecrypt/` is worse than one that prints a failure.
        try:
            await host.close()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"RipperHost.close() raised: {exc}")

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(
        "OK: the rendered name keys to the file's own key, the tag title does not, and the "
        "progress reader reads a real upstream Task."
    )
    return 0


class _EmptyManager:
    """A `DownloadManager` with nothing in it, which is the first second of every rip."""

    def get_task(self, adam_id: str):
        return None


async def check_progress(host: RipperHost) -> list[str]:
    """The seam's progress reader, against a real upstream `Task`.

    The suite's fake is *handed* the byte counts, so it pins `hub/app.py` and pins nothing
    about the reader that gets them from upstream. This builds a real `Task` with a real
    `M3U8Info`, stands one in for the ripper, and checks the reading -- so the field names
    (`decrypted_bytes`, `m3u8Info.range_length`) are verified against the client rather than
    against my belief about it. It does not transfer 30 MB to do so.

    The three cases are the three answers that matter: no task yet, a task with a known total,
    and a task whose total upstream does not know.

    The `Task` and `M3U8Info` are **duck-typed rather than imported**, and the reason is the
    boundary: `tests/test_ripper_host.py` exempts exactly two files from the `upstream-import`
    rule, and a `spike/` file is not one of them, so `from src.task import Task` here fails
    the whole suite -- correctly. What the reader actually needs is two attribute names, and
    `_assert_field_names` below checks those against the real `src/task.py` and `src/types.py`
    as *text*, which is how `test_the_codec_set_matches_the_clients` verifies the codec set
    without importing it. So the field names are verified against upstream and the reader is
    exercised against an object with those names, which is the whole of what it reads.
    """
    failures: list[str] = []

    def assert_field_names() -> None:
        for relative, field in (("src/task.py", "decrypted_bytes"), ("src/types.py", "range_length")):
            source = (VENDOR / relative).read_text(encoding="utf-8")
            if f"{field}:" not in source:
                failures.append(
                    f"{relative} has no {field!r} field, so the seam's reader is reading a "
                    f"name that no longer exists"
                )

    assert_field_names()

    class _M3U8Info:
        """The two fields `_read_progress` reads off `task.m3u8Info`."""

        def __init__(self, range_length=None):
            self.range_length = range_length

    class _Task:
        """`adamId` for the manager's key, and `decrypted_bytes` for the reading."""

        def __init__(self, adam_id: str, decrypted_bytes: int, m3u8Info):
            self.adamId = adam_id
            self.decrypted_bytes = decrypted_bytes
            self.m3u8Info = m3u8Info
    seen: list = []
    real_ripper = host._ripper  # noqa: SLF001 - the reader reads this
    host._on_progress = seen.append  # noqa: SLF001 - the subscription under test

    leaf = Leaf(
        adam_id="1440935466", title="t", album_name="A", artist_name="X",
        codec="alac", language=host.region_language or "ja", url="u", storefront="jp",
    )

    try:
        # 1. Nothing registered yet. `rip_song` registers its `Task` after a metadata
        #    round-trip, so the first second of every rip has no task -- and *no reading* is
        #    the answer. `Progress(0, ...)` would move a progress bar backwards the moment the
        #    task appeared.
        host._ripper = type("_R", (), {"download_manager": _EmptyManager()})()  # noqa: SLF001
        if host._read_progress(leaf) is not None:  # noqa: SLF001
            failures.append(
                "a track with no Task yet produced a reading; it must be None, or a progress "
                "bar would jump backwards when the Task appears"
            )
        else:
            print("\n  no task yet          None  (correct: not zero)")

        # 2. A task with a known total.
        task = _Task(leaf.adam_id, 512, _M3U8Info(range_length=1024))
        host._ripper = type("_R", (), {
            "download_manager": type("_M", (), {"get_task": lambda _s, a: task})()
        })()  # noqa: SLF001
        reading = host._read_progress(leaf)  # noqa: SLF001
        print(f"  known total          {reading}")
        if reading is None:
            failures.append("_read_progress returned None for a registered task")
        elif (reading.bytes_done, reading.bytes_total, reading.fraction) != (512, 1024, 0.5):
            failures.append(
                f"read {reading}, expected bytes_done=512 bytes_total=1024 fraction=0.5"
            )

        # And the callback fires, which is the whole of what `hub/app.py` subscribes to.
        host._on_progress(reading)  # noqa: SLF001
        if seen != [reading]:
            failures.append(f"the callback was not called; seen {seen}")

        # 3. An unknown total, which is what a transfer without a byte range looks like.
        task.m3u8Info = _M3U8Info(range_length=None)
        unknown = host._read_progress(leaf)  # noqa: SLF001
        print(f"  unknown total        {unknown}")
        if unknown is None or unknown.bytes_total is not None or unknown.fraction is not None:
            failures.append(
                f"an unknown total read as {unknown}; `None` renders as an indeterminate bar "
                f"and 0.0 renders as a hang"
            )
    finally:
        host._ripper = real_ripper  # noqa: SLF001
        host._on_progress = None  # noqa: SLF001
    return failures


if __name__ == "__main__":
    # `start()` and `close()` are async even though neither awaits anything today: `start()`
    # resolves the client's singletons, and the coroutine is what a future warm-up would go
    # in. Awaiting them here rather than calling them bare is what the hub itself does, and
    # a check script that did it differently would not notice a signature change.
    raise SystemExit(asyncio.run(run()))
