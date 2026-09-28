"""The Go hub's window into the Apple client, over one pipe.

`hub-go` ports the hub, not the client. The client -- `AppleMusicDecrypt/src/*`, reached
through `hub/ripper_host.py` -- is four thousand lines of Apple API and FairPlay work that
the port has no business reimplementing: it is the one part of this system that talks to
somebody else's servers, it is already tuned against them, and a second implementation
would drift from upstream in ways nobody would notice until a track refused to download.
So it stays Python, and the Go side drives it across a process boundary.

**Why a worker rather than a per-op subprocess.** `RipperHost.start()` chdirs the process
into the vendor tree, puts `AppleMusicDecrypt/` on `sys.path`, registers six `creart`
creators and -- most of what matters -- builds the token cache and the HTTP client that make
`expand()` cheap. Doing that once per request would pay a second of setup and a fresh login
per album, and `creart`'s caches are process-global state a fresh process cannot carry. One
long-lived worker is what makes the boundary affordable.

**The protocol is one JSON object per line, both ways.** Line-delimited rather than
length-prefixed because a human has to be able to read a stuck pipe, and JSON because the
alternative is a second serialization format in a project that already has one.

    ->  {"id": 1, "op": "expand", "args": {"url": "...", "codec": "alac", "language": "jp"}}
    <-  {"id": 1, "ok": true, "result": [{"adam_id": "...", "title": "...", ...}]}
    <-  {"id": 2, "ok": false, "error": "ResolveError: ..."}
    <-  {"event": "progress", "job_id": 7, "fraction": 0.4, "bytes_done": 12, "bytes_total": 30}
    <-  {"event": "log", "line": "..."}

**Requests are dispatched concurrently, responses are not.** `expand`, `run_song` and
`render_filename` all become tasks, so a slow album expansion does not block a progress
line, and `run_song` for two jobs runs at the same time -- which is the point of
`AMD_RIP_CONCURRENCY`. Every write to stdout goes through one lock, because the progress
callbacks arrive on worker threads (the seam calls `on_progress` from a thread) while the
op handlers write from the event loop, and two interleaved `write()` calls would produce a
line neither side can parse -- which would look like a crash, not like a race.

**The wrapper is not driven from here.** The supervisor, its readiness gate and the 2FA
file are lifecycle and text handling, so the Go side owns them; what this worker owns is
everything that needs the client in-process. That split is why `run_song` can take a leaf
that the Go side re-expanded after a restart without this process knowing anything about
the job table.

Run it with the Go hub's own environment: `python3 tools/pyworker.py --config
<vendor>/config.toml`, with `hub/` importable (the image installs the package; a checkout
needs `PYTHONPATH=<repo>/hub`).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import threading
import traceback
from typing import Any

# One lock for stdout. See the module docstring: progress callbacks arrive on worker
# threads and op handlers write from the loop, and a line torn in half is a line neither
# side can parse.
_WRITE_LOCK = threading.Lock()


def _send(payload: dict) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    with _WRITE_LOCK:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _log(line: str) -> None:
    """A log line for the hub's log pane, never a response."""
    _send({"event": "log", "line": line})


def _error(exc: BaseException) -> str:
    """`TypeName: message`, the same shape the Python hub's own failures use.

    The traceback goes to stderr, where the container log will have it, and not into the
    message: a job row is read by a user, and a stack trace in one is noise that also hides
    the one line that says what happened.
    """
    traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
    return f"{type(exc).__name__}: {exc}"


class Host:
    """The lazy `RipperHost`, plus the project's working directory.

    Lazy because `start()` is expensive and the hub boots whether or not an account is
    configured: a hub whose worker cannot start must still answer `/api/health`, or the
    container's healthcheck turns a missing account into a restart loop.
    """

    def __init__(self, config_path: str) -> None:
        self._config_path = config_path
        self._host = None
        self._lock = asyncio.Lock()
        self._current_job: int | None = None

    async def _ensure(self):
        if self._host is not None and self._host.started:
            return self._host
        async with self._lock:
            if self._host is not None and self._host.started:
                return self._host
            from hub.ripper_host import RipperHost

            host = RipperHost(self._config_path, on_progress=self._on_progress)
            await host.start()
            self._host = host
            _log("the Apple client is ready")
            return host

    # -- progress -------------------------------------------------------- #

    def _on_progress(self, progress) -> None:
        """One reading, tagged with the job it belongs to.

        Called from a worker thread, so it only writes a line: the Go side owns the
        `mark` -- and owns refusing it, which matters, because a reading can arrive after
        the job finished and the store is the thing that knows.
        """
        job_id = self._current_job
        if job_id is None:
            return
        _send(
            {
                "event": "progress",
                "job_id": job_id,
                "fraction": progress.fraction,
                "bytes_done": progress.bytes_done,
                "bytes_total": progress.bytes_total,
            }
        )

    # -- ops ------------------------------------------------------------- #

    async def op_expand(self, args: dict) -> list[dict]:
        from hub.resolver import expand

        host = await self._ensure()
        leaves = await expand(
            args["url"],
            codec=args["codec"],
            language=args.get("language") or "",
            web_api=host.web_api,
        )
        return [dataclasses.asdict(leaf) for leaf in leaves]

    async def op_render_filename(self, args: dict) -> str:
        from hub.jobs import Leaf

        host = await self._ensure()
        return host.render_song_filename(
            Leaf(**args["leaf"]), track_number=int(args.get("track_number") or 1)
        )

    async def _run(self, args: dict, music_video: bool) -> None:
        from hub.jobs import Leaf

        host = await self._ensure()
        leaf = Leaf(**args["leaf"])
        self._current_job = int(args["job_id"])
        try:
            if music_video:
                await host.run_music_video(leaf, force=bool(args.get("force")))
            else:
                await host.run_song(leaf, force=bool(args.get("force")))
        finally:
            # Cleared here and not by the caller, so a reading from a sampler that
            # outlived its rip cannot be attributed to the next job.
            self._current_job = None

    async def op_run_song(self, args: dict) -> None:
        await self._run(args, music_video=False)

    async def op_run_music_video(self, args: dict) -> None:
        await self._run(args, music_video=True)

    async def op_region_language(self, args: dict) -> str:
        host = await self._ensure()
        return host.region_language or ""

    async def op_status(self, args: dict) -> dict:
        host = await self._ensure()
        return await host.wrapper_status()

    async def op_shutdown(self, args: dict) -> None:
        if self._host is not None:
            await self._host.close()


async def _dispatch(host: Host, request: dict, tasks: set[asyncio.Task]) -> None:
    request_id = request.get("id")
    op = request.get("op")
    method = getattr(host, f"op_{op}", None)
    if method is None:
        _send({"id": request_id, "ok": False, "error": f"ValueError: unknown op {op!r}"})
        return
    try:
        result = await method(request.get("args") or {})
    except BaseException as exc:  # noqa: BLE001 - every failure is a response, not a death
        _send({"id": request_id, "ok": False, "error": _error(exc)})
        return
    _send({"id": request_id, "ok": True, "result": result})
    if op == "shutdown":
        # Answered first, because the Go side is waiting for it; closing the loop is what
        # makes the process exit and is the point of the op.
        for task in list(tasks):
            task.cancel()


async def main(config_path: str) -> int:
    host = Host(config_path)
    _log("amd-hub python worker ready")
    loop = asyncio.get_running_loop()
    tasks: set[asyncio.Task] = set()

    # Reading stdin on the loop, not in a thread with a blocking read: the ops are
    # coroutines, so a thread would need `call_soon_threadsafe` for every line anyway,
    # and `connect_read_pipe` is the same thing without the hop.
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        line = await reader.readline()
        if not line:
            break
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except ValueError as exc:
            _send({"id": None, "ok": False, "error": f"ValueError: bad request line: {exc}"})
            continue
        task = loop.create_task(_dispatch(host, request, tasks))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--config",
        required=True,
        help="the AppleMusicDecrypt config.toml the seam reads (AMD_VENDOR_CONFIG).",
    )
    args = parser.parse_args()
    # `hub` must be importable. Said out loud rather than left to the traceback, because
    # the failure otherwise reads as a missing module in a container whose image does
    # have it -- one layer of `PYTHONPATH` between the two.
    if not os.environ.get("AMD_WORKER_QUIET"):
        sys.stderr.write(f"pyworker: hub importable from {os.environ.get('PYTHONPATH', '')!r}\n")
    try:
        return asyncio.run(main(args.config))
    except KeyboardInterrupt:  # pragma: no cover - SIGINT is the Go side's stop button
        return 0


if __name__ == "__main__":
    raise SystemExit(cli())
