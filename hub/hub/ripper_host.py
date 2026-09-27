"""The one place in the hub that is allowed to import `AppleMusicDecrypt/src`.

**The boundary is the point of this module.** `AppleMusicDecrypt/` is a separate
upstream clone, intended to become a git submodule and to be upgraded from
upstream. Every `import src.*` that lives outside this file couples the hub to a
tree nobody here controls, and an upstream refactor then breaks the whole hub
instead of one file. `tests/test_ripper_host.py` walks the AST of every module in
`hub/hub/` and fails on any other importer, so the constraint is enforced rather
than documented.

**There are two exempt files, and each is exempt for a stated reason.**
`hub/vendor.py` is the other one: it is the thin pass-through to upstream's URL
parser, which is needed by `hub/resolver.py` and is the narrowest thing that can
hold it -- one upstream name, against this file's seven. Splitting them is not
about relaxing the boundary; it is so that the boundary reads as "the seam boots
the client, the vendor module lends the parser" rather than "one file does two
unrelated jobs".

Three things about the upstream client drive the shape of this file, and all
three are load-bearing.

**The vendor path.** `AppleMusicDecrypt/` is a top-level package directory named
`src`, so nothing resolves it until that directory is on `sys.path`. It is
derived from this file's own location -- `parents[2] / "AppleMusicDecrypt"` --
and never from `Path.cwd()`, because the hub's working directory is `/app` in
the container and `hub/` in a dev checkout, and both are wrong.

**The working directory.** Three upstream paths are relative to the process CWD
and the client never chdirs into its own tree:

- `Config.load_from_config()` defaults to the literal `"config.toml"`
  (`src/config.py`), and `ConfigCreator.create` calls it with no argument;
- `EMBEDDED_TEMPLATE_PATH = Path("assets/prefetch_template.json")` (`src/decrypt.py`),
  which is what removes the wrapper `/key` round-trip entirely when it loads;
- `download.dirPathFormat`, default `"downloads/{album_artist}/{album}"`, which
  is where files actually land.

So `start()` chdirs to the vendor root and **keeps it there** for the ripper's
lifetime, restoring the previous directory in `close()`. This is a process-global
change and it is not free: the hub must not use a relative path of its own
while a `RipperHost` is started. Every path in `hub.config.Settings` is absolute
by default, and `AMD_LIBRARY_ROOTS` / `AMD_DB_PATH` / `AMD_WRAPPER_BASE_DIR` are
operator-supplied absolute mount points, so that holds in practice -- but it is a
constraint on the rest of the hub, not a detail of this file.

Chdir-ing only for the duration of `start()` would not work, and it is worth
knowing why: `src/flags.py` has `language: str = it(Config).region.language` as a
dataclass *field default*, and `src/api.py` / `src/wrapper.py` evaluate
`it(Config)` inside `@retry(...)` decorator arguments. Both happen when the
module is imported, so `it(Config)` -- and therefore `open("config.toml")` --
fires at import time, and the FairPlay template and the download directory are
read at rip time. Import-time and rip-time need the same CWD, so it has to
persist.

**The creart registration order.** `main.py` registers seven creators in a fixed
order and this registers six of them. The one it drops is `TaskTreeCreator`,
which exists only to drive the TUI queue: `src/rip.py` resolves `it(Measurer)`
and the loggers, and never `TaskTree`, and the hub renders its own queue. The
order of the remaining six is not a style preference -- each has to be
registered before the *next import* resolves it, so `LoggerCreator` precedes
`src.config` (`src/flags.py`), and `ConfigCreator` precedes `src.api` and
`src.wrapper` (their `@retry` decorators).

`creart` keeps its instances in a process-global cache, so a second host in the
same process shares this one `Config`, `WebAPI` and `WrapperClient`. That is
what makes a second `start()` cheap and why it must be a no-op rather than a
second registration: `add_creator` raises `ValueError` on a duplicate target.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tomllib
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from hub.jobs import Leaf, Progress

# `hub/hub/ripper_host.py` -> parents[2] is the repository root, which is where
# the `AppleMusicDecrypt/` checkout lives. In the container that root is `/app`
# and the vendor tree is `/app/AppleMusicDecrypt`.
_VENDOR_ROOT = Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"

# The sections `src.config.Config` requires. It declares `region`, `instance`,
# `localInstance` and `download` without defaults, so a config missing one is a
# pydantic `ValidationError` from deep inside `create()` -- which is a worse
# report than naming the file and the section here.
_REQUIRED_CONFIG_SECTIONS = ("region", "instance", "localInstance", "download", "metadata")

# creart's creators live in a process-global list, so this is a process-global
# fact and belongs at module scope rather than on an instance.
_creators_registered = False

# creart caches its instances in a module-level dict that nothing can evict, so
# closing the process-global WrapperClient is a one-way door for the whole process.
# This is module scope for the same reason: a *second* `RipperHost` in the same
# process would otherwise get the closed client back and fail later as an httpx
# "client has been closed" from inside `_request`'s retry loop. Per-instance state
# cannot see that, which is exactly the bug.
_global_wrapper_closed = False

# The one started host, if any. `os.chdir` is process-global, so two hosts cannot
# both believe they own it: the second would capture the vendor root as its "where
# I found the process", and the first `close()` would then restore the caller's
# directory out from under the second, whose own `close()` would put the process
# into `AppleMusicDecrypt/` and leave it there. That was reachable, and it is the
# one way this module could strand the process in the vendor tree with no route
# back, so `start()` refuses a concurrent second host instead. Task 8/9's contract
# is one host per process anyway; this makes that contract enforced rather than
# assumed, and keeps the comment above honest.
_active_host = None

# The creart-resolved `WrapperClient`, kept so `close()` can tell "the process-global
# client" from a stand-in. A test that substitutes its own wrapper must not be able to
# set the one-way-door flag by closing it.
_global_wrapper = None


class _TaskSink:
    """Keeps the upstream `Task` for the duration of a rip, so its outcome survives.

    **Upstream unregisters the task before `rip_song` returns.** `DownloadManager.unregister_task`
    runs in the method's `finally` and deletes the `adam_id -> Task` entry, so by the time the
    await returns there is nothing left to read -- and the `Task` is the *only* place the
    outcome is recorded, because `rip_song` does not raise on failure (see `run_song`).

    So `install()` wraps the two methods on the ripper's own `download_manager`. The wrapper
    keeps its own copy keyed by `adam_id`, which is unaffected by the unregistration.

    A dict rather than a context manager because `run_song` is the only holder of the
    `Task` and a per-`adam_id` key is what upstream's own table is keyed by, so the two cannot
    disagree about which task belongs to which track. `end()` **pops**, so a second rip of the
    same track reads its own task and not the previous one's -- which is the bug a
    "keep the last one" implementation would have after a retried download.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, object] = {}

    def install(self, ripper) -> None:
        """Wrap the ripper's task registration. Idempotent per manager.

        **Only `register_task` is wrapped.** `unregister_task` is left alone: upstream's
        deletion is what makes the outcome unreadable, and the sink keeps its own copy keyed
        the same way, so intercepting the removal would only add a second thing to get right.
        A manager is marked rather than the sink, so a *different* manager -- a ripper replaced
        after `start()`, which `_attach` in the tests does -- still gets wrapped.
        """
        manager = getattr(ripper, "download_manager", None)
        if manager is None or getattr(manager, "_amd_hub_sink", False):
            return
        original_register = manager.register_task
        sink = self

        async def register(task):
            sink._tasks[task.adamId] = task
            return await original_register(task)

        manager.register_task = register
        manager._amd_hub_sink = True

    def begin(self, adam_id: str) -> None:
        """Clear any task left from a previous run of this track."""
        self._tasks.pop(adam_id, None)

    def end(self, adam_id: str):
        """The task this rip left behind, or `None`. Removes it either way."""
        return self._tasks.pop(adam_id, None)


def _status_value(name: str):
    """`src.task.Status[name]`, or the string itself when the name is not one of them.

    Exists so a test can say `outcome = "FAILED"` in a language the boundary allows, without
    the *production* path having to care. The fallback to the raw string is deliberate: a
    status this build does not have compares unequal to `DONE` and to `ALREADY_EXIST`, so it
    lands in the "did not finish" branch -- the safe direction, and the one a new upstream
    status gets by default rather than by being noticed.
    """
    from src.task import Status

    try:
        return Status[name]
    except KeyError:
        return name


class RipperHostError(RuntimeError):
    """Anything that went wrong in the seam.

    `RuntimeError` and not a bespoke base, because the hub's own startup path
    (`hub.config.load_settings`) already raises `RuntimeError` for operator
    errors, and a caller that handles one and not the other is a bug in every
    caller. The message is the diagnostic: upstream messages are carried through
    verbatim rather than replaced with a generic "rip failed".
    """


def _require_vendor_root() -> Path:
    """Return the `AppleMusicDecrypt` checkout, or explain precisely what is wrong.

    A bare `ModuleNotFoundError: No module named 'src'` three frames deep, from
    inside a `retry` decorator, tells the reader nothing about which directory is
    missing or how to get it.
    """
    root = _VENDOR_ROOT
    if not (root / "src").is_dir():
        raise RipperHostError(
            f"the AppleMusicDecrypt checkout is not at {root}. This module derives "
            f"that path from its own location ({Path(__file__).resolve()}) and "
            f"expects <repo root>/AppleMusicDecrypt/src to exist. Clone "
            f"WorldObservationLog/AppleMusicDecrypt, or mount it there in the "
            f"container; the hub cannot download anything without it."
        )
    return root


def _ensure_vendor_on_path(root: Path) -> None:
    """Put the vendor root on `sys.path` so `import src.*` resolves.

    Inserted rather than appended: a different `src` package already on the path
    would otherwise win, and the failure would look like a pydantic error about
    the wrong `Config`.
    """
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)


# Task 8's `parse_apple_music_url` used to live here. It moved to `hub/vendor.py`,
# because this module is a process-lifecycle class -- it holds the working directory
# and runs subprocesses -- and it does not own pure URL parsing. `vendor.py` is the
# second of the two files allowed to import the upstream tree, and it is the narrower
# of the two: it reaches one name, where this one reaches seven. See its docstring for
# why the parse must not be duplicated into the resolver.


def _resolve_config(config_path: Path, vendor_root: Path) -> Path:
    """Validate the config the upstream loader is going to read, and return it.

    `ConfigCreator.create` calls `Config.load_from_config()` with its default
    argument, the literal relative string `"config.toml"`. There is no seam in
    upstream to pass a path through, and no creart hook to override one, so the
    only file the client will ever read is `<CWD>/config.toml` -- and the CWD is
    the vendor root. Hence the location check, which is a consequence rather than
    a preference, and an error message that says so: accepting a config from
    anywhere else would mean silently reading a *different* file than the one the
    caller named.
    """
    path = Path(config_path)
    if not path.is_file():
        raise RipperHostError(
            f"no AppleMusicDecrypt config at {path}. The client reads "
            f"`<vendor root>/config.toml`; copy config.example.toml there, or run "
            f"`python scripts/migrate_config.py` to bring an old one up to date."
        )

    expected = vendor_root / "config.toml"
    if path.resolve() != expected.resolve():
        raise RipperHostError(
            f"config_path must be {expected}, not {path}. AppleMusicDecrypt's "
            f"Config.load_from_config() opens the relative path \"config.toml\" "
            f"with no way to override it, and this seam sets the working "
            f"directory to the vendor root so that the relative download "
            f"directory and assets/prefetch_template.json keep resolving. Any "
            f"other file would be ignored rather than used."
        )

    try:
        with path.open("rb") as handle:
            parsed = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RipperHostError(f"{path} could not be read as TOML: {exc}") from exc

    missing = [name for name in _REQUIRED_CONFIG_SECTIONS if name not in parsed]
    if missing:
        raise RipperHostError(
            f"{path} is missing the section(s) {', '.join(missing)}, which "
            f"AppleMusicDecrypt's Config requires. Regenerate it from "
            f"config.example.toml or run `python scripts/migrate_config.py`."
        )
    return path


def _register_creators() -> None:
    """Register the six creart creators, in the order `main.py` registers them.

    Each import is separate and each `add_creator` immediately follows it,
    because the *next* import needs the previous creator: `src.config` is
    imported before `src.api` and `src.wrapper` because those two evaluate
    `it(Config)` in `@retry` decorator arguments while the class body runs, and
    `src.logger` is imported first because `src.wrapper` does the same with
    `it(GlobalLogger)`.

    `TaskTreeCreator` is deliberately absent. `src/rip.py` *does* reference
    `it(TaskTree)` -- four times, at `rip_song`, `rip_album`, `rip_artist` and
    `rip_playlist` -- and `src/mv.py` three more. What matters is that every one
    of those sites is wrapped in `try: ... except Exception: pass`, because
    upstream made the TUI an optional attachment rather than a hard dependency of
    the ripping pipeline. That is the durable reason six is correct and the one
    to rely on: the hub is not a TUI host, the hub renders its own queue, and
    upstream has arranged for the absence of a task tree to be a no-op rather
    than an error. It is also why the count could drop to six without a
    behaviour change, and why it will not break if upstream adds a seventh.

    Idempotent by flag rather than by probing creart: `add_creator` raises
    `ValueError` on a duplicate target, so a second call would turn a harmless
    repeat into a crash.
    """
    global _creators_registered
    if _creators_registered:
        return

    from creart import add_creator
    from src.logger import LoggerCreator
    add_creator(LoggerCreator)
    from src.config import ConfigCreator
    add_creator(ConfigCreator)
    from src.api import APICreator
    add_creator(APICreator)
    from src.wrapper import WrapperCreator
    add_creator(WrapperCreator)
    from src.decrypt import DecryptorCreator
    add_creator(DecryptorCreator)
    from src.measurer import MeasurerCreator
    add_creator(MeasurerCreator)

    _creators_registered = True


class RipperHost:
    """A started-upstream client, and the `Leaf` -> ripper translation.

    One instance per hub process. `start()` is idempotent and `close()` restores
    the working directory `start()` changed, so the host has to outlive every rip
    rather than be built per job.
    """

    def __init__(self, config_path: Path, *, on_progress: Callable | None = None) -> None:
        """`config_path` is `<vendor root>/config.toml`; `on_progress` is optional.

        **The optional progress callback is keyword-only and defaults to `None`, which is
        what keeps every existing call and every existing test working.** It is called from
        inside upstream's transfer loop, in whatever thread that loop runs in, with a
        `hub.app.Progress`; `hub.app` hops to its event loop and writes the row. A caller that
        passes `None` -- which is every caller that does not want progress -- gets no
        behaviour change at all, not even a `None` check somewhere hot.
        """
        # Resolved here rather than in `start()`: `start()` chdirs, so a relative
        # path would be interpreted against the vendor root instead of against
        # the directory the caller named.
        self._config_path = Path(config_path).resolve()
        # Not validated here on purpose: a callable check would reject a Mock in a test, and
        # the only thing that matters is whether it is called. A non-callable truthy value
        # fails at the first progress tick, in a message that names this argument.
        self._on_progress = on_progress
        # Where the `Task` each rip left behind is kept, for `run_song` to read the outcome
        # from. See `_TaskSink` for why the outcome has to be read at all.
        self._tasks = _TaskSink()
        self._origin_cwd: Path | None = None
        self._started = False
        # Rips currently awaiting upstream. `close()` refuses while this is
        # non-zero: restoring the CWD under a running rip would leave that rip
        # resolving `EMBEDDED_TEMPLATE_PATH` and `dirPathFormat` against the
        # hub's directory, which fails *silently* for reads.
        self._in_flight = 0
        # Set by `start()`; the tests substitute fakes for the rippers.
        self._ripper = None
        self._mv_ripper = None
        self._wrapper = None
        # The two things Task 9 needs from the *client* rather than from hub config: the
        # catalogue client its resolver is written against, and the language the client's own
        # `config.toml` asks for. Both would otherwise have to be invented on the hub side,
        # and a hub-side constant that disagrees with `region.language` is a hub that
        # silently requests the wrong storefront language.
        self._web_api = None
        # Not tracked per instance: see `_global_wrapper_closed`, which is module
        # scope because the client it describes is process-global.

    @property
    def started(self) -> bool:
        return self._started

    @property
    def web_api(self):
        """The upstream ``WebAPI`` instance, or `None` before `start()`.

        `hub.resolver.expand` takes a `web_api` by injection precisely so it never
        constructs one (and so it stays off the network under test). Task 9 therefore needs
        the client the seam already built rather than a second copy of it -- constructing a
        second one here would be a second `httpx` client, a second set of retry decorators
        and a second source of truth for which storefront credentials are in play.

        `None` rather than raising, because "the client is not up yet" is a state the hub
        routes around (the API answers 503 and the UI retries) and not an error to report.
        """
        return self._web_api

    @property
    def region_language(self) -> str | None:
        """``region.language`` from the client's own config, or `None` before `start()`.

        The language every catalogue lookup is made in. It is *not* a hub setting: the
        library the hub reads was written with whatever this client was configured with, and
        a `POST /api/jobs` that defaulted to some other language would queue tracks whose
        titles came back in a form the dedup comparison has never seen.

        `None` means "ask the caller to say", and the API turns that into a 400 rather than
        into a guess.
        """
        if not self._started:
            return None
        from creart import it
        from src.config import Config

        return it(Config).region.language

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """Put the vendor tree on `sys.path`, register the creators, build the rippers.

        Declared `async` for symmetry with the pipeline it feeds -- and so a
        future warm-up (`Decryptor.warm_prefetch`, say) has somewhere to await.
        Nothing here awaits today, because every upstream singleton is created
        synchronously by `creart.create`.

        A failure here is fatal for the process and cannot be retried in-process:
        creart exposes no way to un-register a creator, so a half-finished
        registration would make the next attempt raise `ValueError` on a
        duplicate target instead of the real error. The message says so rather
        than letting the retry's `ValueError` be the thing a reader sees.
        """
        if self._started:
            return

        # Checked before the chdir, so a refusal leaves the process where it was.
        # `start()` has no `await` before the chdir and none until it is done, so
        # check-then-claim cannot interleave with another task on the event loop.
        global _active_host
        if _active_host is not None and _active_host is not self:
            raise RipperHostError(
                f"another RipperHost is already started in this process "
                f"(id={id(_active_host):#x}). The working directory it holds is "
                f"process-global, so two hosts cannot both restore it: a second "
                f"one would record the vendor root as where it found the process "
                f"and leave the process inside AppleMusicDecrypt/ when it closed. "
                f"Use one host per process, or close the other first."
            )

        root = _require_vendor_root()
        # Kept, not discarded. `os.chdir(root)` is what makes `<root>/config.toml`
        # *be* the `config.toml` that `load_from_config()` opens, so the file that
        # was validated above and the file the client will read are provably the
        # same one. Assigning it to a local makes that relationship explicit
        # instead of leaving it implied by the chdir on the next line.
        config = _resolve_config(self._config_path, root)
        _ensure_vendor_on_path(root)

        # Before the first `import src.*`: see the module docstring for why the
        # import-time `it(Config)` and the rip-time relative paths need the same
        # CWD.
        self._origin_cwd = Path.cwd()
        os.chdir(root)
        try:
            # The whole config story rests on this identity, so it is checked
            # rather than assumed: `load_from_config()` opens the relative
            # `"config.toml"`, so after the chdir the file it opens is
            # `<CWD>/config.toml` -- and that has to be the file just validated.
            if (Path.cwd() / "config.toml").resolve() != config.resolve():
                raise RipperHostError(
                    f"{config} was validated, but <CWD>/config.toml after the chdir "
                    f"to {root} is a different file; refusing to start rather than "
                    f"letting the client read an unvalidated config"
                )

            _register_creators()

            from creart import it
            from src.api import WebAPI
            from src.config import Config
            from src.decrypt import Decryptor
            from src.measurer import Measurer
            from src.mv import MVRipper
            from src.rip import Ripper
            from src.wrapper import WrapperClient

            # Resolved here, eagerly, so that a config that is missing or
            # unreadable fails in `start()` with a message that names it -- and
            # not later, inside the first job, as a traceback through
            # `load_from_config`.
            it(Config)
            if _global_wrapper_closed:
                raise RipperHostError(
                    "this process already closed creart's process-global "
                    "WrapperClient (via RipperHost.close()), and creart cannot "
                    "evict or re-create it, so a new host would hold an "
                    "aclose()d client and every wrapper call would fail as an "
                    "httpx 'client has been closed' from inside the retry loop. "
                    "Restart the hub process instead of starting a second host."
                )
            global _global_wrapper
            self._wrapper = it(WrapperClient)
            _global_wrapper = self._wrapper
            self._web_api = it(WebAPI)
            it(Decryptor)
            it(Measurer)

            self._ripper = Ripper()
            self._mv_ripper = MVRipper()
            self._tasks.install(self._ripper)
        except Exception as exc:
            self._restore_cwd()
            self._ripper = None
            self._mv_ripper = None
            self._wrapper = None
            self._web_api = None
            if isinstance(exc, RipperHostError):
                raise
            raise RipperHostError(
                f"could not start the AppleMusicDecrypt client from {root}: "
                f"{type(exc).__name__}: {exc}. This is fatal for the process -- "
                f"creart cannot un-register a creator, so restarting the hub is "
                f"the only way to retry."
            ) from exc

        # Claimed only on success, so a failed start leaves the slot free for a
        # retry that has not been poisoned by the half-finished attempt.
        _active_host = self
        self._started = True

    async def close(self) -> None:
        """Close the wrapper client and give the process its working directory back.

        **Refuses while a rip is in flight.** `os.chdir` is process-global and the
        working directory is what `EMBEDDED_TEMPLATE_PATH` (`src/decrypt.py`) and
        `download.dirPathFormat` resolve against, so restoring it under a running
        rip would silently point that rip at the hub's directories -- a lost
        FairPlay template and a download into the wrong tree, with no error
        anywhere. Refusing is the honest answer: the caller asked to tear down
        something still in use, and only it knows whether to wait or cancel.

        A caller that genuinely wants the teardown to proceed awaits the
        outstanding `run_song` / `run_music_video` first. That is what Task 8/9's
        shutdown path has to do, and it is the one piece of ordering this API
        cannot do on its own.

        The wrapper client is creart's process-global instance, so closing it is
        correct rather than merely tidy -- it owns an `httpx.AsyncClient` and
        leaving it open leaks a socket and a pool on every shutdown -- but it is
        also why a later `start()` in the same process is refused: creart caches
        instances and cannot evict one, so the restart would otherwise inherit an
        `aclose()`d client.
        """
        if not self._started:
            return
        if self._in_flight:
            raise RipperHostError(
                f"refusing to close: {self._in_flight} rip(s) still in flight. "
                f"Closing would move the working directory back while they run, "
                f"and upstream resolves assets/prefetch_template.json and the "
                f"download directory against it -- which fails silently. Await "
                f"the outstanding run_song/run_music_video calls, or cancel them, "
                f"then close."
            )

        wrapper, self._wrapper = self._wrapper, None
        self._ripper = None
        self._mv_ripper = None
        self._web_api = None

        failure: Exception | None = None
        try:
            if wrapper is not None:
                await wrapper.close()
                # Set only on success: a `close()` that raised may have left the
                # client usable, and claiming otherwise would refuse a restart
                # that would in fact work.
                global _global_wrapper_closed
                if _global_wrapper is not None and wrapper is _global_wrapper:
                    _global_wrapper_closed = True
        except Exception as exc:  # noqa: BLE001 - reported below, never swallowed
            failure = exc
        finally:
            # Released here and not after the `finally`, so a `close()` that
            # raised still frees the one-host slot: the CWD is restored either
            # way, and a host that gave its directory back is not "still started".
            global _active_host
            if _active_host is self:
                _active_host = None
            self._restore_cwd()
            self._started = False

        if failure is not None:
            raise RipperHostError(f"closing the wrapper client failed: {failure}") from failure

    def _restore_cwd(self) -> None:
        origin, self._origin_cwd = self._origin_cwd, None
        if origin is not None:
            os.chdir(origin)

    @contextmanager
    def _in_flight_rip(self):
        """Count a rip for as long as it awaits upstream, so `close()` can wait it out.

        A plain counter, not a lock: rips legitimately run concurrently, and the
        only question `close()` asks is "is the count zero yet". It also releases
        on `BaseException`, so a cancelled rip does not leave the host permanently
        un-closeable.
        """
        self._in_flight += 1
        try:
            yield
        finally:
            self._in_flight -= 1

    # ------------------------------------------------------------------ #
    # Translation
    # ------------------------------------------------------------------ #
    def _require_ripper(self):
        if not self._started or self._ripper is None or self._mv_ripper is None:
            raise RipperHostError(
                "this RipperHost is not started; await start() before ripping"
            )
        return self._ripper

    def _require_wrapper(self):
        if not self._started or self._wrapper is None:
            raise RipperHostError(
                "this RipperHost is not started; await start() before asking the "
                "wrapper for its status"
            )
        return self._wrapper

    async def run_song(self, leaf: Leaf, *, force: bool) -> None:
        """Rip one track, and raise if it did not finish.

        **Upstream's `rip_song` reports failure by *returning*.** Its whole body is one
        `try`, and the failure arms set `task.update_status(Status.FAILED)` and
        `task.error = e` without re-raising -- which is the right design for a TUI, where the
        row is the report and a raised exception would take down the caller. For the hub it
        means `await rip_song(...)` returning normally says *nothing* about whether a file was
        written, and a caller that treats a return as success marks every failed download
        `done`. That is not hypothetical: `spike/task9_contract_check.py` hit it, and the
        queue said `done` for a track whose only error was a `ValidationError` from the
        catalogue.

        So the outcome is read off the `Task` -- the same object upstream marks -- after the
        call. `_TaskSink` is how: `register_task` is intercepted at start-up so the `Task` is
        kept for the duration of the call, and `unregister_task`'s removal does not hide it
        from us. Everything upstream's `Status` says about the outcome is honoured, including
        `ALREADY_EXIST`, which is a *success* by another name and must not become an error.

        `force` is upstream's `Flags.force_save`, i.e. "re-download even though a file for
        this metadata already exists". The hub decides it per leaf (§7.3), at execution time,
        and it is not part of the queue's dedup key.

        No `parent_done` is passed, and deliberately: `rip_song` calls it purely to release a
        parent that is waiting on its children, and the hub's job scheduler owns that
        bookkeeping itself. Passing a handler would satisfy the wrong waiter and leave the
        hub's parent job waiting on a callback nothing else will ever call.
        """
        ripper = self._require_ripper()
        if leaf.is_music_video:
            raise RipperHostError(
                f"adam_id={leaf.adam_id} is a music video, so run_song() would take "
                f"the FairPlay path where it needs Widevine; call run_music_video()"
            )

        from src.flags import Flags
        from src.url import Song, URLType

        # Construction is inside the guard, so a malformed leaf surfaces as a
        # `RipperHostError` like every other seam failure. A bare
        # pydantic `ValidationError` would be more informative about *which*
        # field, but a caller written as `except RipperHostError` -- which
        # `RipperHostError`'s own docstring invites -- would miss it, and the
        # cause is chained either way.
        with self._in_flight_rip():
            try:
                url = Song(
                    url=leaf.url, storefront=leaf.storefront, id=leaf.adam_id, type=URLType.Song
                )
                # Installed per call rather than only in `start()`, because the ripper can be
                # replaced after `start()` -- `_attach` in the tests does exactly that, and a
                # sink wrapped around the *previous* ripper would find nothing. `install` is
                # idempotent, so the common path is a flag check.
                self._tasks.install(ripper)
                self._tasks.begin(leaf.adam_id)
                try:
                    async with self._with_progress(leaf, lambda: ripper.rip_song(
                        url, leaf.codec, Flags(force_save=force, language=leaf.language)
                    )):
                        pass
                finally:
                    task = self._tasks.end(leaf.adam_id)
            except Exception as exc:
                raise RipperHostError(
                    f"rip_song failed for adam_id={leaf.adam_id}: {exc}"
                ) from exc
            self._raise_unless_finished(leaf, task)


    def _raise_unless_finished(self, leaf: Leaf, task) -> None:
        """Turn a `Task` that reports failure into a `RipperHostError`, or return.

        `task is None` means the task was never registered -- which upstream also treats as a
        non-event, since `rip_song` returns early for an `adam_id` already in flight. So the
        honest answer is "I did not see it fail", and the hub's §7.3 dedup check, which runs
        *before* this, is what catches a track that is already on disk.

        `ALREADY_EXIST` is a success, and that is the case where guessing wrong is worst: it is
        upstream's answer for "the file was already there", which is exactly what the user
        asked for with `force=False`, and turning it into an error would fill the queue with
        failures for downloads that did the right thing.
        """
        if task is None:
            return

        if task.status in (_status_value("DONE"), _status_value("ALREADY_EXIST")):
            return
        detail = task.error or f"it ended as {task.status}"
        raise RipperHostError(
            f"rip_song did not finish for adam_id={leaf.adam_id}: it ended as "
            f"{task.status} and reported {detail}. Note that the client's rip_song() "
            f"reports failure by returning rather than by raising, so the queue's status "
            f"comes from the task it left behind rather than from an exception."
        )

    async def run_music_video(self, leaf: Leaf, *, force: bool) -> None:
        """Rip one music video, which is Widevine-only and has no codec to choose.

        There is no `Leaf.is_music_video` assertion here, deliberately and
        asymmetrically: `run_song` guards the *silent* failure (a wrong
        decryption path, reported as an error from the wrong subsystem), whereas
        this direction fails immediately and visibly on the first `/webplayback`
        call, which is an acceptable report of a caller's mistake.

        **`force` is accepted and ignored, and that is upstream's doing, not an
        oversight here.** `MVRipper.rip` is `async def rip(self, url, flags=None)`
        (`src/mv.py:89`) and the body never reads `flags` -- `grep -n flags
        src/mv.py` returns that one signature line. The music-video path has no
        "already on disk" check to override: `rip_song` consults
        `check_song_exists` under `if not flags.force_save`, whereas
        `MVRipper.rip` goes straight from the manifest to the HLS fetch, so it
        re-downloads unconditionally and `force_save=False` cannot make it skip.

        The parameter is kept rather than dropped because the brief pins this
        signature and Task 8/9 are written against it. A caller must therefore
        **not** rely on `force=False` to avoid a music-video re-download -- there
        is no "don't re-download" behaviour to ask for on this path.
        `test_run_music_video_ignores_force` pins that, against upstream's own
        signature, so the two `force` behaviours cannot drift together.
        """
        self._require_ripper()

        from src.flags import Flags
        from src.url import MusicVideo, URLType

        with self._in_flight_rip():
            try:
                url = MusicVideo(
                    url=leaf.url, storefront=leaf.storefront, id=leaf.adam_id,
                    type=URLType.MusicVideo,
                )
                # `Flags` is built and passed so the call matches upstream's
                # signature and so a release that starts reading it needs no
                # change here. Today it is inert; see above.
                async with self._with_progress(leaf, lambda: self._mv_ripper.rip(
                    url, Flags(force_save=force, language=leaf.language)
                )):
                    pass
            except Exception as exc:
                raise RipperHostError(
                    f"run_music_video failed for adam_id={leaf.adam_id}: {exc}"
                ) from exc

    # ------------------------------------------------------------------ #
    # Progress
    # ------------------------------------------------------------------ #
    #: How often the progress task samples, in seconds. A tenth of a second, because the
    #: consumer is a browser painting a bar and the cost on this side is one SQLite write --
    #: so the throttle lives *here*, next to the loop, rather than in `hub.app` where it would
    #: be one more thing to get right in the wrong place. 10 Hz is well under what a person
    #: can see and far above what a network panel would call excessive.
    PROGRESS_INTERVAL = 0.1

    @asynccontextmanager
    async def _with_progress(self, leaf: Leaf, call):
        """Await `call()`, reporting this track's byte count while it runs.

        **Upstream has no progress event to subscribe to**, so this polls. `Ripper` keeps a
        `Task` per `adamId` in `download_manager`, and that `Task` carries `decrypted_bytes`
        and `m3u8Info` -- so the count is read from the object upstream is already updating,
        not inferred from the file on disk (which would be a second, lagging and partially
        written answer). A poll and a real event differ in latency and in nothing else.

        The total is `m3u8Info.range_length` when upstream knows it, and `None` when it does
        not. `None` is the honest answer and the UI renders it as an indeterminate bar; a
        fabricated 0 would render as a bar stuck at zero, which reads as a hang.

        Nothing runs when `on_progress` is `None`, so the poll loop costs a `None` check and
        no task -- the common case for every caller that does not want progress.
        """
        if self._on_progress is None:
            yield await call()
            return

        stop = asyncio.Event()

        async def sample() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.PROGRESS_INTERVAL)
                    return
                except TimeoutError:
                    pass
                reading = self._read_progress(leaf)
                if reading is not None:
                    self._on_progress(reading)

        task = asyncio.create_task(sample())
        try:
            yield await call()
        finally:
            stop.set()
            # Awaited rather than dropped: a detached task would report one more reading
            # after `run_song` returned, i.e. after the job was marked `done`, and the last
            # thing on the stream would be a `running` frame.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _read_progress(self, leaf: Leaf):
        """One `Progress` for `leaf`, or `None` if the count is not readable.

        `None` rather than a zeroed `Progress`: a task that has not been registered yet --
        `rip_song` registers it after a metadata round-trip, so the first second of a rip
        has no `Task` at all -- is not progress of zero, and writing zero would move a
        progress bar backwards the moment the task appeared.

        **A music video always reads as `None`, and that is a known gap rather than a
        transient state.** The two paths differ structurally: `MVRipper.rip` (`src/mv.py`)
        does not go through `Ripper.download_manager` at all, so it registers no `Task` and
        keeps no `decrypted_bytes` to read. There is nothing for this to poll -- the
        alternative would be inferring progress from the size of the output file, which is
        the partially-written-file answer this method exists to avoid.

        So the queue's progress column is simply blank for a video, from start to end, and
        `hub/web/templates/job_row.html` renders that as "no progress" rather than as a bar
        at zero. The two sibling gaps on the same path are in `run_music_video` and
        `_raise_unless_finished`'s sibling: MVs are never deduplicated (§2's non-goal, since
        they live in one flat `mv.saveDir` with no album scope to compare), and `force` is
        inert for them because `Flags.force_save` is not read on the Widevine path. Closing
        the progress gap means reading something out of `MVRipper`, which is an upstream
        change and not a hub one; it is recorded here rather than in the report alone so it
        is found by whoever reads this method next.
        """
        ripper = self._ripper
        manager = getattr(ripper, "download_manager", None)
        if manager is None:
            return None
        task = manager.get_task(leaf.adam_id)
        if task is None:
            return None

        done = int(getattr(task, "decrypted_bytes", 0) or 0)
        m3u8 = getattr(task, "m3u8Info", None)
        total_raw = getattr(m3u8, "range_length", None) if m3u8 is not None else None
        total = int(total_raw) if isinstance(total_raw, int) and total_raw > 0 else None
        fraction = (done / total) if total else None
        return Progress(bytes_done=done, bytes_total=total, fraction=fraction)

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def render_song_filename(self, leaf: Leaf, *, track_number: int = 1) -> str:
        """The file name `rip_song` would write for `leaf`, extension included.

        **This is the value §7.3's duplicate check must be given, and `leaf.title` is
        not.** `normalize` is not idempotent: it strips up to two leading numeric groups, so
        ``1-01 1 a.m. (feat. …).m4a`` keys to ``1 a.m. (feat. …)`` and normalizing *that*
        again drops the leading ``1 ``. Six of the 8,721 real library keys are not fixed
        points, so a check fed a tag title compares against a key the library does not hold
        and re-downloads a file that is already there -- silently, and in the safe direction,
        which is exactly why nothing would ever report it.

        So the candidate is rendered the way the ripper will render it, and rendered
        **once**: `get_song_name_and_dir_path(codec, metadata)` with the client's own
        `songNameFormat` and `get_suffix(codec, atmosConventToM4a)`, the exact pair
        `check_song_exists` uses (`src/utils.py:211`). Anything else -- re-implementing the
        format, hardcoding the default, or normalizing the title here and passing the result
        -- is a second source of truth for what a file is called, and this codebase has
        already had one of those (`hub/vendor.py`'s docstring is about the URL parser).

        **Why it lives here and not in the API layer.** It needs `it(Config)` and
        `src.utils`, so it is unreachable from `api/jobs.py` by construction -- that is what
        the boundary test in `tests/test_ripper_host.py` enforces, and this method is the
        reason the boundary has an answer instead of a workaround.

        `track_number` is the one input the hub cannot know. `rip_song` reads it from the
        catalogue; a `Leaf` has no track number (spec §6's `job` table has no column for one,
        and `title` is documented "log display only"), so it defaults to 1. That is not a
        guess about the *title*: the default `songNameFormat` is ``{disk}-{tracknum:02d}
        {title}``, and `normalize` folds that numeric prefix away, so the rendered key is the
        same for every track number in the album. It is an approximation of the file name,
        not of the comparison.

        `Leaf` is frozen and carries no disc number either, so this is single-disc. A
        multi-disc album's second disc renders with a different `disk` and therefore a
        different prefix -- which again normalizes to the same key.
        """
        self._require_ripper()

        from creart import it
        from src.config import Config
        from src.metadata import SongMetadata
        from src.utils import get_song_name_and_dir_path, get_suffix

        # `album_artist` is not on a `Leaf`, and the default `dirPathFormat` interpolates it.
        # `artist_name` is what the resolver read off `attributes.artistName` -- the same
        # field `SongMetadata.parse_from_song_data` puts in `artist` -- so it is the closest
        # honest value, and the *directory* half is not used here at all: §7.5's comparison
        # basis is the file name on one side and the album directory name on the other, and
        # the directory this render produces is never compared.
        metadata = SongMetadata(
            song_id=leaf.adam_id,
            title=leaf.title,
            artist=leaf.artist_name,
            album_artist=leaf.artist_name,
            album=leaf.album_name,
            tracknum=track_number,
            disk=1,
            track_total={1: track_number},
            disk_total=1,
        )
        song_name, _dir_path = get_song_name_and_dir_path(leaf.codec, metadata)
        return song_name + get_suffix(leaf.codec, it(Config).download.atmosConventToM4a)

    async def wrapper_status(self) -> dict:
        """The wrapper's `GET /status` payload, e.g. ``{"regions": [...]}``.

        The upstream `status` is `@alru_cache`d and never expires on its own, so
        the cache is dropped first: a readiness gate that answered from a
        successful probe taken before the wrapper died is worse than no gate,
        because it reports the one thing it exists to detect as healthy.
        """
        wrapper = self._require_wrapper()
        invalidate = getattr(wrapper.status, "cache_invalidate", None)
        if callable(invalidate):
            invalidate()
        try:
            return await wrapper.status()
        except Exception as exc:
            raise RipperHostError(
                f"wrapper status unavailable at {wrapper.base_url}: {exc}"
            ) from exc
