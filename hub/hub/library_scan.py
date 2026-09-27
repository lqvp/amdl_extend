"""Album discovery across every configured library root.

The filesystem is the only source of truth and nothing here is cached: the 0.06 s is the
cost of `os.walk` alone, and a full `scan_roots` of the same 341 GB external library
measures 0.091 s (median of 5: walk 0.044, extension filter 0.005, `normalize` 0.015,
building 3,670 album scopes 0.038, `by_name` 0.009). So every request re-walks, a
directory the user moved outside the app is followed immediately, and there is no scan
cache to invalidate and no staleness window to reason about -- the only cache the design
allows is tag reads, and those belong to `library.py`, not here.

What a walk yields is deliberately *not* a model of the library. `dirPathFormat`
describes one of the two libraries on this machine and not the other, and neither
matches the shapes the downloader actually produces over time (loose files in an artist
directory, a subdirectory sitting next to a loose file, an album directory holding a
`.part` leftover). So the scan reports the one fact that is unambiguous -- **a directory
that directly holds at least one audio file is an album scope** -- and derives artist
from the structure as a best effort that is allowed to be `None`. Anything that needs
tags, or anything that would be a guess, is not answered here.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePath
from types import MappingProxyType

from hub.normalize import is_audio_file, normalize

# The `relpath` of the album scope that a root is, when the root holds audio directly.
# `Path.relative_to` answers "." for a path against itself, so `roots[i] / relpath` keeps
# naming the directory, and it is the one value that identifies a scope by position rather
# than by name -- see `scan_roots` for why that matters for `by_name`.
_ROOT_SCOPE = "."


@dataclass(frozen=True, slots=True)
class AlbumDir:
    """One album scope: a directory that directly holds at least one audio file.

    `relpath` is posix-form and relative to **the root exactly as the caller passed it**
    (`scan_roots` does not resolve anything). `root_index` is the position of that root
    in `LibraryScan.roots`, so an album dir can always be resolved back to an absolute
    path for display or for serving a file.
    """

    root_index: int
    relpath: str
    name: str
    artist: str | None
    track_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class LibraryScan:
    """The result of one walk of every root, plus what to warn the user about.

    `roots` and `reachable` are positional and always the same length, including for a
    root that could not be read. A rejected root keeps its slot so that `root_index`
    means the same thing to every `AlbumDir` in the scan, and so the UI can name the
    drive that is missing instead of silently deduping against a smaller library.
    """

    roots: tuple[Path, ...]
    reachable: tuple[bool, ...]
    albums: tuple[AlbumDir, ...]
    by_name: Mapping[str, tuple[AlbumDir, ...]]

    @property
    def degraded(self) -> tuple[Path, ...]:
        """The roots that could not be read, in the order the caller passed them.

        An unmounted external drive must be *loud*. `loose` dedup against the
        surviving roots still works, so the failure mode without this is a quiet
        re-download of everything that lived on the missing drive.
        """
        return tuple(
            root for root, ok in zip(self.roots, self.reachable, strict=True) if not ok
        )

    def per_root(self) -> tuple[int, ...]:
        """Album directories found under each root, positional like `roots` and `reachable`.

        **This exists because `degraded` cannot catch the failure it looks like it catches.**
        A root that is *unreadable* is reported, loudly, and that is the case `degraded`
        describes. A root that is **mounted and empty** is not: Docker's bind-mount
        autocreate makes a directory when the source is missing, so an absent drive
        becomes a present, readable, zero-album root that `reachable` reports as `True`.
        `library_scan` genuinely cannot tell an empty library from an absent drive --
        nothing on disk distinguishes them -- so the count has to be surfaced rather than
        inferred, and a *per-root* count is the only
        shape that can be: a total is equally consistent with both roots working and with one
        of them empty.

        Kept positional, so `roots[i]` and `reachable[i]` mean the same thing here as
        everywhere else, including for a root that was rejected.
        """
        counts = [0] * len(self.roots)
        for album in self.albums:
            counts[album.root_index] += 1
        return tuple(counts)


def album_key(name: str) -> str:
    """The `by_name` key for an album, and the key a lookup must use.

    `strip_track_prefix=False` is the whole point: album names keep their leading
    digits, because `4pi` and `1st EP` are real albums. Callers get this
    function rather than `normalize` so that building the index and looking it up
    cannot drift apart -- if they did, every lookup would miss and nothing would ever
    be skipped, with no error anywhere.
    """
    return normalize(name, strip_track_prefix=False)


def scan_roots(roots: Sequence[Path] | Path | str) -> LibraryScan:
    """Walk every root and return the album scopes found across all of them.

    A root is *reachable* when it is a directory. An unreachable one keeps its slot in
    `roots`/`reachable`, is listed in `degraded`, and contributes no albums; the other
    roots are unaffected. Nothing here raises for a bad root, because a single missing
    341 GB drive must not take down dedup for the 69 GB one next to it.

    Accepts `Sequence[Path]`, a single `Path`, or a single `str`, and the return type is
    the same either way: `roots` is a `tuple[Path, ...]` of `Path` objects, in the order
    given, with a slot kept for every input. `Settings.library_roots` is a `list[Path]`
    and the scheduler passes that, but one root is the common case while developing and
    `TypeError: 'PosixPath' object is not iterable` is a baffling way to learn that.
    """
    root_paths = _as_roots(roots)
    albums: list[AlbumDir] = []
    reachable: list[bool] = []
    grouped: dict[str, list[AlbumDir]] = {}

    for index, root in enumerate(root_paths):
        ok = root.is_dir()
        reachable.append(ok)
        if not ok:
            continue
        for album in _walk_root(index, root):
            albums.append(album)
            if album.relpath == _ROOT_SCOPE:
                # The root is a container that happens to hold audio, not an album, and
                # `by_name` is keyed by album *name*. Indexing it would put a mount-point
                # basename -- "Music", "my-music", a volume UUID -- into that index,
                # where it would silently join the group of a real album with that name and
                # be matched against as one. It is worse than a latent collision: the same
                # drive reached by two different spellings would produce two different
                # keys, and reachability is checked from more than one call site, so
                # nothing guarantees they spell the path the same way.
                #
                # Nothing is lost by leaving the scope out of the index. It keeps its
                # place in `albums`, and the only route to it before this exclusion ran
                # was a lookup by the mount point's own basename -- "Music",
                # "my-music" -- which is never the album name of a download, so no
                # re-request could have matched it. Those loose files were already
                # invisible to dedup; what the exclusion removes is a false-positive
                # route, not a working one.
                continue
            grouped.setdefault(album_key(album.name), []).append(album)

    return LibraryScan(
        roots=root_paths,
        reachable=tuple(reachable),
        albums=tuple(albums),
        # A frozen dataclass holding a plain dict is not frozen in any way that matters,
        # and this one is handed to the API layer and to `dedup.py`, both of which have no
        # business inserting into a scan result.
        by_name=MappingProxyType(
            {key: tuple(members) for key, members in grouped.items()}
        ),
    )


def _as_roots(roots: Sequence[Path] | Path | str) -> tuple[Path, ...]:
    """One path becomes a one-element sequence; `str` is accepted for the same reason.

    Coerced through `Path` so that `roots` is always `Path` objects even if the caller
    passed strings, which keeps `roots[i] == <what the caller holds>` true for the
    `degraded` comparison. `Path` also drops a trailing separator, so
    `_walk_root`'s prefix arithmetic does not have to special-case one -- except for
    the filesystem root itself, which it does.
    """
    if isinstance(roots, (str, os.PathLike)):
        return (Path(roots),)
    return tuple(Path(root) for root in roots)


def _walk_root(root_index: int, root: Path) -> Iterator[AlbumDir]:
    """Yield the album scopes under one root, in a stable order.

    **`root` is used as given and is never resolved.** The user's own
    path is typically a user-managed symlink into `/run/media/<volume-UUID>/`, so `relpath`
    has to be cut against the string that was passed in. Resolving the root but not the
    paths `os.walk` yields (or the reverse) makes every entry look like it lives outside
    the root, and the whole scan then quietly finds nothing -- no exception, no album
    dir, every download running again.

    `os.walk` is used rather than a manual `os.listdir` recursion because it hands back
    the directory's non-directory entries without a second syscall pass, which is what
    keeps this at the measured cost. The two are equivalent for the album-dir test
    unless a *directory* is named `something.m4a`.

    The walk descends through album directories rather than stopping at them, and that is
    not a contradiction of "an album dir is a scope": the real library has
    `TEMPLIME/HIKO.flac` and `TEMPLIME/Escapism/` side by side, so `TEMPLIME` and
    `TEMPLIME/Escapism` are two separate scopes. Not descending would apply to building
    the *key set* -- which never merges a child's keys into its parent -- and not to the
    traversal.
    """
    root_str = os.fspath(root)
    # `Path` has already dropped any trailing separator, but "/" (the filesystem root)
    # is all separator, and doubling it would make nothing look like a descendant.
    prefix = root_str if root_str.endswith(os.sep) else root_str + os.sep

    for dirpath, dirnames, filenames in os.walk(root_str):
        # readdir order is filesystem-dependent -- NTFS does not sort -- and relpaths
        # are shown to the user in skip_reason, so two scans of one unchanged tree have
        # to come out in the same order. Per-directory order, not a global sort of
        # relpaths: a directory's subtree stays contiguous, but "A B/z" and "A/a" order
        # differently under the two rules and only the first is meaningful here.
        dirnames.sort()

        audio = [name for name in filenames if is_audio_file(name)]
        if not audio:
            continue
        relpath = _relpath(dirpath, root_str, prefix)
        # Basenames only, and `.part` is already gone because `is_audio_file` rejects it:
        # 160 of those are the real library's leftovers from interrupted downloads, and
        # counting one as an existing track wrongly skips a re-request.
        # A key of "" is *kept* -- `normalize` reports an unusable title rather than
        # hiding it, so `dedup.find_duplicate` can find it and refuse to skip on it.
        # Only an empty *set* means "not an album dir", and an audio file always
        # contributes a key, so the test is exact.
        track_keys = frozenset(normalize(name) for name in audio)
        parts = () if relpath == _ROOT_SCOPE else tuple(relpath.split("/"))
        # A root that holds audio directly is still an album scope -- the real library has
        # `Hatsuboshi Gakuen & Kotone Fujita - Yellow Big Bang!.m4a` sitting in it, and
        # leaving those files out would make them invisible to dedup, silently.
        #
        # Its name is "" rather than the root's own basename. No directory on any
        # filesystem can have an empty name, so "" cannot be the name of a real album; a
        # basename taken from the caller's spelling of the path can (the same drive is
        # "Music" at one path and "my-music" at another), which would make `scan.albums`
        # depend on how the root was configured. `scan_roots` keeps the root scope out of
        # `by_name` for the same reason.
        name = parts[-1] if parts else ""
        yield AlbumDir(
            root_index=root_index,
            relpath=relpath,
            name=name,
            artist=_artist(parts),
            track_keys=track_keys,
        )


def _relpath(dirpath: str, root_str: str, prefix: str) -> str:
    """`dirpath` relative to the root string the caller passed, in posix form.

    A lexical slice rather than `Path.relative_to`, deliberately: it cannot resolve a
    symlink even by accident, and `relative_to` on a resolved root is exactly the
    mistake this module must not make. `_ROOT_SCOPE` is what `os.walk` yields for the
    root itself, and it is also `Path.relative_to` self's own answer, so
    `root / relpath` still names the directory.
    """
    if dirpath == root_str:
        return _ROOT_SCOPE
    if not dirpath.startswith(prefix):
        # Unreachable: `os.walk` only ever joins names onto the root it was handed. If
        # this fires, someone resolved one side of the walk and not the other, and the
        # alternative to raising here is a scan that reports an empty library.
        raise ValueError(
            f"{dirpath!r} is not below the scanned root {root_str!r}; the root and the "
            f"walked paths must both be used unresolved"
        )
    return PurePath(dirpath[len(prefix) :]).as_posix()


def _artist(parts: Sequence[str]) -> str | None:
    """The artist for an album scope: the parent directory's name, or None.

    `parts` is the relative path split into components, album directory included, so
    `parts[-1]` is the album and `parts[-2]` is its parent. That is the whole rule, and it
    is the shape `dirPathFormat` produces: 3,660 of the 3,670 album directories in the real
    external library and all 1,069 in `downloads/` are `artist/album/`, and
    `ALAC/Atmos/TEMPLIME/POP-AID` still answers TEMPLIME because a format bucket sits
    between the artist and the root, never between the artist and the album.

    `None` means there is no parent below the root to read a name from: the album
    directory *is* the root, or it sits directly in it. `Music/Nyarons/A.flac` is the
    second case, and there are 9 more like it in the real library. Reporting the root's
    own name instead would put "Music" in the library UI and make `strict` matching
    compare against it, and `strict` already treats `None` as "cannot vouch for this".

    `ALAC/Atmos/Some Album/` -- an album with no artist level, so the parent is a codec
    directory -- is answered as `"Atmos"`. That is a known wrong answer, accepted because
    the real library contains no such directory and a `None` there would be no less of a
    guess. It is the reason the rule has no codec-bucket exception: an exception that only
    fires on inputs that do not exist costs a reader more than it saves.
    """
    if len(parts) < 2:
        return None
    return parts[-2]
