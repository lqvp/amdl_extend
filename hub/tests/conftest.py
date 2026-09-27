"""Session-wide pytest fixtures.

`from hub... import ...` resolves because `pyproject.toml` sets
`[tool.pytest.ini_options] pythonpath = ["."]` and rootdir is the project root.

The library trees below are the **regression fixtures**: every shape in
them was observed in the real 341 GB external library, so the numbers the design rests
on (`intro` in 6 albums, 160 `.part` files, 223 duplicated album-dir names) only stay
true if the scanner keeps reproducing these shapes. `tests/test_dedup.py`
depends on the same two trees, so they live here rather than in one test module.

A few entries are **contract probes** rather than claims about the library: they pin
behaviour the real library does not currently exercise, so a future refactor cannot
quietly get away with something. Each is marked where it is created, and the list is:

- `a/Album Seven/COVER.FLAC` and `a/Album Seven/.m4a` — `is_audio_file`'s two
  boundaries, a casefolded extension and a leading dot that is not an extension. Every
  extension in the real library is lowercase and no file there is named `.m4a`.
- `a/!_/01 t.m4a` — an album name with no alphanumeric character in it, which
  `normalize` answers `""` for. The real library has `ALAC/薄塩指数/!_`, so this one *is*
  observed, but only one instance of it, and the observable claim is the empty key.
  `extra/・・・` is the same shape from the other side of the comparison — the name a
  *download* would carry — and the empty album key has to be refused for both.
- `a/Artist/Album/x.m4a` — the depth-2 `<root>/<artist>/<album>` shape, reachable only by
  scanning `a/` as a root. Real (`downloads/` is entirely this shape) but not reachable
  through the fixture root, which prefixes everything with `a/`.
- `a/Some/Atmos/Album/t.m4a` — a codec directory that is not below a codec directory,
  the one input the artist rule is known to answer wrongly. No real library produces it.
- `a/A`, `a/A/CD 1`, `a/A B` — the ordering discrimination for per-directory order, which
  needs a nested album beside a sibling. The library does nest albums, so the shape is
  real even though these three names are not.
- `a/429 & nyankobrq/Named Album/t.m4a` — an artist directory whose name is not its own
  comparison key. Observed: the real library holds `429 & nyankobrq` as one of 349 artist
  directories, and it is the only one `normalize()` changes.
- `extra/4 - Leaves/01 t.m4a` — the only album name on which `strip_track_prefix` makes a
  difference. `extra/4pi` and `extra/1st EP` cannot tell the flag apart, so without this
  one the project's highest-consequence parameter is untested.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _touch(path: Path) -> Path:
    """Create an empty file and its parents.

    Contents are irrelevant: the scan is `stat`-only and never reads a tag, so
    a zero-byte file exercises exactly the same code path as a real 30 MB `.m4a` and
    keeps the suite fast.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.fixture
def make_library(tmp_path: Path) -> Path:
    """The regression tree, rooted at `tmp_path / "lib"`.

    The root is the directory that holds the two library shapes, so relpaths are
    `a/...`, `b/...`, `Nyarons/...` and no assertion has to know where pytest put
    `tmp_path`. The suite scans it both whole (`scan_roots(make_library)`) and as one of
    several roots (`scan_roots([make_library / "a", ...])`), which is what the degraded
    and multi-root tests need.

    Note `Nyarons` sits at the top of the tree rather than under `a/`: the real
    observation is `Music/Nyarons/A.flac` ("artist 直下の散在ファイル"), i.e. an
    artist directory *at the root of the library*, and that placement is what makes its
    artist genuinely undeterminable. Under `a/` the parent would be a grouping
    directory and `artist` would be a guess.
    """
    root = tmp_path / "lib"
    # -- 種別 A: one release filed in two places (ALAC/ and new-dl/) -------------------
    _touch(root / "a/ALAC/鎖那/Hush a by little girl/01 track.m4a")
    _touch(root / "b/new-dl/鎖那/Hush a by little girl/01 track.m4a")
    # -- 種別 B: collab fan-out, the same release under each credited artist ----------
    _touch(root / "a/ALAC/EmoCosine/らぶふぉーゆー - Single/t.m4a")
    _touch(root / "a/ALAC/ころねぽち/らぶふぉーゆー - Single/t.m4a")
    # -- format bucket present vs absent for the same artist/album -------------------
    _touch(root / "a/ALAC/TEMPLIME/POP-AID/x.m4a")
    _touch(root / "a/TEMPLIME/POP-AID/y.m4a")
    # -- a track title shared by three different albums: must never skip --------------
    _touch(root / "a/Album One/intro.m4a")
    _touch(root / "a/Album Two/intro.m4a")
    _touch(root / "a/Album Three/intro.m4a")
    # -- loose files directly in an artist directory at the top of the library --------
    _touch(root / "Nyarons/A.flac")
    # -- a subdirectory sharing a parent with a loose audio file ----------------------
    _touch(root / "a/TEMPLIME/Escapism/t.m4a")
    _touch(root / "a/TEMPLIME/HIKO.flac")
    # -- unusable titles: empty, and a bare track number ---------------------------
    _touch(root / "a/Album Four/..m4a")
    _touch(root / "a/Album Four/01 ..m4a")
    # -- an interrupted download must not count as an existing track ------------------
    _touch(root / "a/Album Five/01 real.m4a.part")
    # -- the same album with one finished file, so the dir is a real album dir --------
    _touch(root / "a/Album Six/01 real.m4a")
    # -- contract probe, not an observed shape: is_audio_file's two boundaries. Every
    # extension in the real library is lowercase, so nothing there pins these. A raw
    # `os.path.splitext(name)[1] in AUDIO_EXTS` drops "cover" (".FLAC" is not a member)
    # and an `endswith` test wrongly keeps ".m4a", which has no extension at all.
    _touch(root / "a/Album Seven/01 real.m4a")
    _touch(root / "a/Album Seven/COVER.FLAC")
    _touch(root / "a/Album Seven/.m4a")
    # -- observed shape, and the only reason `by_name` can hold an empty key: the real
    # library has `ALAC/薄塩指数/!_`, a directory whose name normalizes to "" because it
    # has no alphanumeric character. See D8 on why it is indexed rather than filtered.
    _touch(root / "a/!_/01 t.m4a")
    # -- the downloader's own shape, `artist/album/`, one level above where the rest of
    # this tree sits. 1,069 album dirs in `downloads/` and 40 in the external library are
    # exactly `<root>/<artist>/<album>`, and no other entry here can reach that shape
    # through the scanner: with the fixture root in front, they all sit at depth >= 3.
    # Scanned with `a/` as the root, these are the depth-2 cases.
    _touch(root / "a/Artist/Album/x.m4a")
    # -- a codec directory that is *not* below a codec directory. The old rule answered
    # None to this shape; the rule now in force answers with the parent's name. Present
    # at tree level so the answer is read off the scanner and not only off `_artist`.
    _touch(root / "a/Some/Atmos/Album/t.m4a")
    # -- observed ordering case. "A" holds an album of its own and an album directory
    # nested inside it, and "A B" is their sibling: the pair that tells per-directory
    # order apart from a global sort of the finished relpath strings, because comparing
    # "a/A " against "a/A/" puts the space first.
    #
    # The nested album is named "CD 1", not "a", on purpose. Naming it after its own
    # parent would make `by_name` group a directory with its own child -- a shape no real
    # library produces, and `dedup.py` groups by album name, so it would inherit a duplicate
    # group that cannot occur in the user's data. Sharing the track key "x" between the
    # two is the *real* shape instead: the same title in two different albums, which is
    # what `intro` does six times over.
    _touch(root / "a/A/x.m4a")
    _touch(root / "a/A/CD 1/x.m4a")
    _touch(root / "a/A B/z.m4a")
    # -- an artist directory whose name is not its own comparison key. The real library
    # has exactly one of 349: `429 & nyankobrq`, whose `normalize()` is `& nyankobrq`. It is
    # here because `strict` compares a *tag* value against a *directory basename*, and that
    # comparison is only well defined if both sides go through the same `normalize()`. Every
    # other artist in this tree matches its tag byte-for-byte, so without this one a
    # candidate side that skipped normalization would still agree with every test.
    _touch(root / "a/429 & nyankobrq/Named Album/t.m4a")
    return root


@pytest.fixture
def make_library_extra(tmp_path: Path) -> Path:
    """The album-name-identity tree, rooted at the same `tmp_path / "lib"`.

    Split out from `make_library` because album *identity* cannot be expressed there:
    every album name in the `make_library` tree is free of leading digits, but the real
    library has albums called `4pi` and `1st EP`, and it requires that a directory
    name keeps its number while a track filename loses it. That is exactly what
    `normalize(..., strip_track_prefix=False)` exists for, and it is the one flag in the
    project where a mismatch is silent and total: `dedup.py` looks albums up with the flag
    off, so an index built with the flag on makes *every* lookup miss and nothing is
    ever skipped.

    ` - Single` and ` [Deluxe]` are here because both are must-keep parts of
    an album name; both are two words wide, so a stray `.rstrip(" -[]")` in a later task
    would show up here immediately.
    """
    root = tmp_path / "lib"
    # A 1-to-3 digit number that a separator follows. This is the *only* shape on which
    # strip_track_prefix makes a difference, so it is the only one that can tell the flag
    # apart. The two shapes that look like they would work do not: "4pi" and "1st EP" have
    # no separator, and a 4-digit year is out of `\d{1,3}`'s reach by design, so
    # all three produce the same key under either flag and would pass with it wrong.
    # "4 - Leaves" is the shape of a real numbered series (Gesu no Kiwami Otome's
    # "1 - LIAR", "2 - Polaris", ...).
    _touch(root / "extra/4pi/01 t.m4a")
    _touch(root / "extra/1st EP/01 t.m4a")
    _touch(root / "extra/4 - Leaves/01 t.m4a")
    _touch(root / "extra/Song - Single/t.m4a")
    _touch(root / "extra/Album [Deluxe]/t.m4a")
    # The second instance of the shape `a/!_` already provides: an album name with no
    # alphanumeric character in it, which `normalize` answers `""` for. The real library
    # has `ALAC/薄塩指数/!_` -- the library side of the comparison, and the one that made
    # `by_name[""]` reachable at all. This one is the *download* side: the name an album
    # would have to be given for the lookup to land on that key. Both halves are the
    # observed shape, the real library contains both, and `dedup.py`'s job is to refuse the
    # lookup for either. It belongs in this tree rather than only under `a/` so that the
    # album-identity fixture can answer `"" in by_name` on its own.
    _touch(root / "extra/・・・/t.m4a")
    return root
