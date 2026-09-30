"""Environment -> `Settings`.

Parsing is kept explicit rather than declarative: each value has a documented default and
a documented failure mode, and a misconfigured deployment must fail at startup with a
message naming the variable -- not at the first request, and not by silently falling back
to a different mode.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

DEFAULT_BIND = "0.0.0.0"  # Reachable from the LAN.
DEFAULT_PORT = 8080
# The wrapper is loopback-only and its port must never be published.
DEFAULT_WRAPPER_HOST = "127.0.0.1"
DEFAULT_WRAPPER_PORT = 12340
DEFAULT_WRAPPER_BINARY = Path("/usr/local/bin/wrapper-lite-qemu")
DEFAULT_WRAPPER_BASE_DIR = Path("/data/wrapper")
# Only job state is persisted, so the hub needs exactly one database file, and it
# lives on the hub-data volume.
DEFAULT_DB_PATH = Path("/data/hub.db")

#: How many tracks to rip at once, and why four. Measured here rather than assumed: a 41.8 MB
#: ALAC track takes 9.8 s, and 6.1 s of that is the wrapper answering `/lyrics`, the album
#: lookup and the codec check before a byte of audio moves; the audio then crosses at ~41 MB/s
#: in about a second. So ~85% of a track's wall clock is an API round-trip, and a serial queue
#: spent almost all of it waiting for a sibling that was not running. Four overlapping rips
#: recover most of that, and it is a number to put on a home connection without worrying the
#: Apple account -- upstream's own ceiling (`maxRunningTasks`, 128) is tuned for a TUI driven
#: by a person and is not a number to send from a LAN box.
DEFAULT_RIP_CONCURRENCY = 4

MIN_SESSION_SECRET_CHARS = 32

#: The minimum number of *distinct* characters an operator-supplied
#: `AMD_SESSION_SECRET` must contain. The length check alone would accept
#: `"a"*32`, which is guessable and makes the signed cookie offline-forgeable.
MIN_SECRET_DISTINCT_CHARS = 8

# A port outside this range cannot be bound, and the failure would otherwise surface as
# an OSError from the server at startup rather than as a named misconfiguration.
MIN_PORT = 1
MAX_PORT = 65535

ArtistScope = Literal["loose", "strict"]


class Settings(BaseModel):
    """Validated settings. Every field is supplied by `load_settings`."""

    password: str
    bind: str
    port: int
    library_roots: list[Path]
    wrapper_binary: Path
    wrapper_base_dir: Path
    wrapper_host: str
    wrapper_port: int
    #: How many tracks to rip at once. Upstream's `DownloadManager` has been built for
    #: concurrency all along -- `asyncio.Semaphore(it(Config).download.maxRunningTasks)`,
    #: 128 by default, which the TUI drives with `safely_create_task` -- and only the hub
    #: serialised. Measured here: 85% of a track's wall clock is the wrapper's metadata
    #: round-trip before a byte of audio moves, so a serial queue spends almost all of its
    #: time waiting for a sibling that is not running. Four is well under the 128 and is a
    #: number to put on a home connection without worrying about the account.
    rip_concurrency: int = DEFAULT_RIP_CONCURRENCY
    dedup_artist_scope: ArtistScope
    db_path: Path
    session_secret: bytes
    #: Where an idle-queue announcement is POSTed. Empty means silence, which is the
    #: default: the hub has always been the kind of tool that waits to be looked at,
    #: and an operator who has not pointed it at anything must find it unchanged.
    notify_webhook_url: str = ""


def _text(env: Mapping[str, str], key: str, default: str) -> str:
    """An unset, empty, or whitespace-only variable falls back to the default."""
    value = env.get(key)
    return default if value is None or not value.strip() else value.strip()


def _port(env: Mapping[str, str], key: str, default: int) -> int:
    raw = _text(env, key, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{key} must be an integer, got {raw!r}") from None
    if not MIN_PORT <= value <= MAX_PORT:
        raise RuntimeError(
            f"{key} must be between {MIN_PORT} and {MAX_PORT}, got {value}"
        )
    return value


def _paths(env: Mapping[str, str], key: str, default: tuple[Path, ...]) -> list[Path]:
    raw = _text(env, key, "")
    if not raw:
        return list(default)
    # A single root is the common case and a comma-separated list is the general one;
    # blanks are dropped so a trailing comma is not a path of "".
    roots = [Path(part.strip()) for part in raw.split(",") if part.strip()]
    if not roots:
        raise RuntimeError(f"{key} is set but contains no usable path")
    return roots


def _required_paths(env: Mapping[str, str], key: str, example: str) -> list[Path]:
    """The same parsing as `_paths`, with no fallback.

    A default for one of these is a host-specific path baked into the source, and it is
    wrong on every machine but the one it was written on. The message carries `example`
    so the operator can see the format rather than infer it.

    `example` is a *container-side* path, and the noun has to say so: an earlier wording
    said "host directory" while showing `/library`, and an operator who obeys the noun sets
    the host path here -- which resolves to nothing inside the container and scans an empty
    tree. The host side of the same mount is `AMD_LIBRARY_HOST`, set in `.env`.
    """
    roots = _paths(env, key, ())
    if not roots:
        raise RuntimeError(
            f"{key} is unset. Name every container-side directory that holds your music "
            f"library, comma-separated, e.g. {key}={example}. If you meant a directory on "
            f"this host, that is a different variable (see the deployment's .env)."
        )
    return roots


def _scope(env: Mapping[str, str]) -> ArtistScope:
    value = _text(env, "AMD_DEDUP_ARTIST_SCOPE", "loose")
    # Not a plain default lookup: an unrecognised value is a typo, and silently degrading
    # to "loose" would re-enable the false-skip mode `ARTIST_SCOPES` exists to warn about.
    if value == "loose":
        return "loose"
    if value == "strict":
        return "strict"
    raise RuntimeError(
        f"AMD_DEDUP_ARTIST_SCOPE must be 'loose' or 'strict', got {value!r}"
    )


def _concurrency(env: Mapping[str, str]) -> int:
    # `_text(..., "")` and the constant, rather than the number written twice: a default that
    # lives in both the field and the reader is two values to keep in step, and the one that
    # loses is the one nobody reads.
    raw = _text(env, "AMD_RIP_CONCURRENCY", "")
    if not raw:
        return DEFAULT_RIP_CONCURRENCY
    value = raw
    try:
        count = int(value)
    except ValueError:
        raise RuntimeError(
            f"AMD_RIP_CONCURRENCY must be a whole number, got {value!r}"
        ) from None
    # One is not an error -- it is the old behaviour, and someone may want it to compare --
    # but zero and negatives are, because a gather over an empty or negative list would
    # claim work it never started and leave the jobs `running` for ever.
    if count < 1:
        raise RuntimeError(
            f"AMD_RIP_CONCURRENCY must be at least 1, got {count}. A value of 1 is the "
            f"previous one-track-at-a-time behaviour."
        )
    return count


def _has_entropy(raw: str) -> bool:
    """Whether `raw` carries enough variety to be a session secret.

    A threshold test rather than an entropy *estimate*: the failure mode is a
    degenerate operator value -- `"a"*32`, or `s3cr3t` repeated -- which the length
    check cannot see and which makes the signed cookie offline-forgeable once an
    attacker guesses the degeneracy. Requiring 8 distinct characters blocks every
    low-variety value while accepting anything a human would actually type.
    """
    return len(set(raw)) >= MIN_SECRET_DISTINCT_CHARS


def _session_secret(env: Mapping[str, str]) -> bytes:
    raw = env.get("AMD_SESSION_SECRET", "").strip()
    if not raw:
        # Generated per process on purpose. A missing secret must not degrade into a
        # constant that ships in the image, and regenerating costs only the user's own
        # logged-in sessions across a restart. Set AMD_SESSION_SECRET to keep them.
        return secrets.token_bytes(MIN_SESSION_SECRET_CHARS)
    if len(raw) < MIN_SESSION_SECRET_CHARS:
        raise RuntimeError(
            f"AMD_SESSION_SECRET must be at least {MIN_SESSION_SECRET_CHARS} characters, "
            f"got {len(raw)}; a short secret makes the session cookie forgeable"
        )
    if not _has_entropy(raw):
        raise RuntimeError(
            f"AMD_SESSION_SECRET must use at least {MIN_SECRET_DISTINCT_CHARS} distinct "
            f"characters, got {len(set(raw))}; a low-variety secret is guessable and makes "
            f"the session cookie forgeable (leave AMD_SESSION_SECRET unset to get a random "
            f"one per process)"
        )
    return raw.encode()


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build `Settings` from `env`, or from the process environment when `env` is None.

    Raises `RuntimeError` -- not `ValidationError` -- because every failure here is an
    operator error to fix in the environment, and the message must name the variable.
    """
    source: Mapping[str, str] = os.environ if env is None else env

    # Single shared password, no default, never hardcoded.
    password = source.get("AMD_PASSWORD", "").strip()
    if not password:
        raise RuntimeError(
            "AMD_PASSWORD is unset or empty. The hub has one shared password and no "
            "default; set it in the environment (compose reads it from .env)."
        )

    # Both of these are required, and the order matters: a caller who has set neither
    # should be told about the password, which is the one it is more likely to have
    # meant. `test_the_password_is_still_checked_before_the_library_roots` pins this.
    library_roots = _required_paths(source, "AMD_LIBRARY_ROOTS", "/library")

    return Settings(
        password=password,
        bind=_text(source, "AMD_BIND", DEFAULT_BIND),
        port=_port(source, "AMD_PORT", DEFAULT_PORT),
        library_roots=library_roots,
        rip_concurrency=_concurrency(source),
        wrapper_binary=Path(
            _text(source, "AMD_WRAPPER_BINARY", str(DEFAULT_WRAPPER_BINARY))
        ),
        wrapper_base_dir=Path(
            _text(source, "AMD_WRAPPER_BASE_DIR", str(DEFAULT_WRAPPER_BASE_DIR))
        ),
        wrapper_host=_text(source, "AMD_WRAPPER_HOST", DEFAULT_WRAPPER_HOST),
        wrapper_port=_port(source, "AMD_WRAPPER_PORT", DEFAULT_WRAPPER_PORT),
        dedup_artist_scope=_scope(source),
        db_path=Path(_text(source, "AMD_DB_PATH", str(DEFAULT_DB_PATH))),
        session_secret=_session_secret(source),
        notify_webhook_url=_text(source, "AMD_NOTIFY_WEBHOOK_URL", ""),
    )
