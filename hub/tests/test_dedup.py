"""Album-scoped title matching for download dedup.

The whole point of this module is the *scope*. The real library holds `intro`,
`escapism`, `mu` and `yoake` in six different albums each, and a normalized title is
shared by 2+ album directories for 1,207 of 8,721 titles, so a title match that is not
confined to the album would skip real downloads. Every test below is written so that it
can tell "scoped to the album" from "matched the title somewhere in the library", and the
empty-key tests are the other half: a key with no identifying information must never
produce a skip, because "" equals every other "".

**Three assertions written for this contradicted the fixtures they were given, and are
corrected here.** Each is noted at its test. In every case the stated intent was
right and only its `assert` line was inverted:
`test_a_hit_is_confined_to_the_album_that_was_asked_for` (asked for `None` from an album
that does hold the track), `test_format_bucket_prefix_does_not_matter` (asked for the
*other* copy's relpath under a lookup for the first copy's track), and
`test_album_name_keeps_single_suffix_and_deluxe` (asked for the *exact* name to miss,
which is precisely the suffix-stripping bug the test exists to catch).

A fourth, `test_refuses_to_skip_on_an_empty_album_name`, asserted `"" in by_name` for
`make_library_extra` while the conftest fixture line adding `extra/・・・` had not been
landed. The fixture entry is added here rather than the assertion moved, so the test now
covers both real spellings of the shape: `a/!_` as the library has it, `extra/・・・` as a
download would carry it.
"""

from __future__ import annotations

import builtins
import os
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from hub.dedup import DuplicateHit, find_duplicate
from hub.library_scan import AlbumDir, LibraryScan, scan_roots


def test_skips_when_same_album_exists_in_two_places(make_library):
    # 種別 A — the same release filed in two places. The external drive has 59 duplicated
    # album names of this shape (out of 223), and `strict` catches it too, because both
    # placements sit under the same artist directory.
    hit = find_duplicate(scan_roots(make_library), album_name="Hush a by little girl",
                         track_title="track", artist_name="鎖那", artist_scope="loose")
    assert hit is not None
    assert set(hit.matched) == {"a/ALAC/鎖那/Hush a by little girl",
                                "b/new-dl/鎖那/Hush a by little girl"}


def test_loose_scope_catches_collab_fanout(make_library):
    # 種別 B — same release filed under each credited artist. Apple Music distributes one
    # release to every credited artist's folder, so the same track file exists 3 times
    # and re-downloading it is pure waste. This is the larger of the two shapes -- 164 of
    # the 223 duplicated album names on the external drive -- and the one `strict` cannot
    # see at all, which is why `loose` is the default.
    hit = find_duplicate(scan_roots(make_library), album_name="らぶふぉーゆー - Single",
                         track_title="t", artist_name="EmoCosine", artist_scope="loose")
    assert hit is not None and len(hit.matched) == 2


def test_strict_scope_requires_artist(make_library):
    scan = scan_roots(make_library)
    assert find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                          artist_scope="strict", artist_name="EmoCosine") is not None
    # an artist that filed no copy of this album must not match
    assert find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                          artist_scope="strict", artist_name="Unrelated Artist") is None


def test_strict_scope_answers_only_one_of_the_collab_placements(make_library):
    # The other half of the `strict` case, stated as a number because it is the reason
    # `strict` is not the default: it finds 種別 A and systematically misses 種別 B, so every
    # collab after the first credited artist gets downloaded again.
    scan = scan_roots(make_library)
    loose = find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                           artist_name="EmoCosine", artist_scope="loose")
    strict = find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                            artist_name="EmoCosine", artist_scope="strict")
    assert loose is not None and strict is not None
    assert len(loose.matched) == 2
    assert strict.matched == ("a/ALAC/EmoCosine/らぶふぉーゆー - Single",)


def test_strict_scope_normalizes_the_artist_directory_too(make_library):
    # The two strings `strict` compares come from different places -- one is a tag value
    # the resolver read, the other a directory basename read off the disk -- so the
    # comparison is only well defined because *both* go through the same `normalize()`.
    # Every other artist in the fixture matches its tag byte-for-byte, so this is the only
    # test that can tell a normalized candidate side from a raw one; deleting the
    # `normalize()` on the candidate leaves the rest of this file green.
    scan = scan_roots(make_library)
    # Case folding. "EmoCosine" on disk, the tag spelled "emocosine".
    assert find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                          artist_name="emocosine", artist_scope="strict") is not None
    # The real library holds exactly one artist directory of 349 whose name is not its own
    # comparison key: `429 & nyankobrq`, whose `normalize()` is `& nyankobrq`. A tag
    # carrying either spelling must find it, which is also the docstring's claim that the
    # track-prefix strip is correct to leave on for this side.
    named = next(a for a in scan.albums if a.relpath == "a/429 & nyankobrq/Named Album")
    assert (named.artist, named.name) == ("429 & nyankobrq", "Named Album")
    for tag in ("429 & nyankobrq", "& nyankobrq"):
        hit = find_duplicate(scan, album_name="Named Album", track_title="t",
                             artist_name=tag, artist_scope="strict")
        assert hit is not None, tag
        assert hit.matched == ("a/429 & nyankobrq/Named Album",), tag
    # And the two spellings are the same artist, not two: a tag that names neither
    # spelling is a different artist and must not match.
    assert find_duplicate(scan, album_name="Named Album", track_title="t",
                          artist_name="nyankobrq", artist_scope="strict") is None


def test_strict_scope_refuses_an_artist_name_with_no_identifying_information():
    # `normalize` answers "" for a name with no alphanumeric character, and "" == "" -- so
    # a strict lookup carrying such an artist would match *every* candidate whose artist
    # directory is equally unusable, which is a false skip rather than the re-download the
    # refusal costs. This function has no "cannot decide" branch of its own, so the refusal
    # lives here.
    #
    # The candidate is built by hand rather than added to the fixture: the real library has
    # **no** artist directory whose name normalizes to "" (measured: 0 of 349), so a fixture
    # entry would invent a shape. `conftest` does not do that, and neither does this.
    unusable = AlbumDir(root_index=0, relpath="!/Album", name="Album", artist="!",
                        track_keys=frozenset({"track"}))
    scan = LibraryScan(
        roots=(Path("/nonexistent/lib"),),
        reachable=(True,),
        albums=(unusable,),
        by_name=MappingProxyType({"album": (unusable,)}),
    )
    assert find_duplicate(scan, album_name="Album", track_title="track",
                          artist_name=None) is not None  # `loose` ignores the artist
    for bad in ("・・・", "!", "...", ""):
        assert find_duplicate(scan, album_name="Album", track_title="track",
                              artist_name=bad, artist_scope="strict") is None, bad
    # A usable artist name still works, so the refusal is not a blanket rejection.
    assert find_duplicate(scan, album_name="Album", track_title="track",
                          artist_name="", artist_scope="strict") is None
    real = AlbumDir(root_index=0, relpath="A/Album", name="Album", artist="A",
                    track_keys=frozenset({"track"}))
    usable = LibraryScan(
        roots=(Path("/nonexistent/lib"),), reachable=(True,), albums=(real,),
        by_name=MappingProxyType({"album": (real,)}),
    )
    assert find_duplicate(usable, album_name="Album", track_title="track",
                          artist_name="A", artist_scope="strict") is not None


def test_strict_scope_refuses_when_the_artist_is_unknown(make_library):
    # "strict" means the artist has to vouch for the match. An album directory that is
    # the root, or sits directly in it, has no determinable artist -- the real library
    # holds 10 such -- and a resolver that could not read the artist hands over None.
    # Neither may be treated as a wildcard, or "strict" is only "loose" with extra steps.
    #
    # `artist is None` is also what makes the truthiness guard load-bearing rather than
    # decorative: without it `normalize(None)` raises TypeError on this exact call, so a
    # candidate side that dropped the guard fails here rather than matching too much.
    scan = scan_roots(make_library)
    assert "nyarons" in scan.by_name
    assert scan.by_name["nyarons"][0].artist is None
    assert find_duplicate(scan, album_name="Nyarons", track_title="a",
                          artist_name="Nyarons", artist_scope="loose") is not None
    assert find_duplicate(scan, album_name="Nyarons", track_title="a",
                          artist_name="Nyarons", artist_scope="strict") is None
    assert find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                          artist_name=None, artist_scope="strict") is None
    # `artist_name` is `str | None`; an empty string is what a resolver with a blank field
    # hands over, and it must be refused on the same grounds as None rather than compared.
    for blank in ("", "   "):
        assert find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                              artist_name=blank, artist_scope="strict") is None, blank


def test_a_hit_is_confined_to_the_album_that_was_asked_for(make_library):
    # 'intro' exists in 6 albums in the real library; this is the core requirement.
    # Each of the three fixture albums really does hold "intro", so the
    # claim is that a hit names *that* album and never a sibling sharing the title -- not
    # that the answer is None. (An earlier version asserted None here, which no correct
    # implementation can satisfy: the album looked up is the album holding the track.
    # Asserting None would have pinned the opposite of the requirement.)
    scan = scan_roots(make_library)
    for album in ("Album One", "Album Two", "Album Three"):
        hit = find_duplicate(scan, album_name=album, track_title="intro",
                             artist_name="x", artist_scope="loose")
        assert hit is not None, album
        assert set(hit.matched) == {f"a/{album}"}, album
    # An album that does not hold "intro" never borrows one that does. "Album Four" holds
    # only unusable titles and "Album Six" holds one real track, so neither can answer.
    for album in ("Album Four", "Album Six", "Nyarons"):
        assert find_duplicate(scan, album_name=album, track_title="intro",
                              artist_name="x", artist_scope="loose") is None, album


def test_the_global_answer_is_wider_than_the_scoped_one(make_library):
    # The contrast the scoping exists to draw, asserted rather than left in a comment: a
    # title search over the whole library is what `intro` would wrongly produce, and it is
    # 3x the scoped answer here and 6x in the real library.
    scan = scan_roots(make_library)
    assert sorted(a.relpath for a in scan.albums if "intro" in a.track_keys) == [
        "a/Album One",
        "a/Album Three",
        "a/Album Two",
    ]


def test_format_bucket_prefix_does_not_matter(make_library):
    # The same artist/album filed twice, once under the ALAC bucket and once not.
    # Discovery is by album basename, so the bucket cannot change which
    # directories are in scope. Each copy holds a *different* track though -- `x` in the
    # bucketed one, `y` in the plain one -- so a lookup for one title sees exactly one
    # copy. (An earlier version asked for "a/TEMPLIME/POP-AID" from a lookup for "x", which
    # is the
    # *other* copy's track; that assertion could only pass if the bucket prefix were
    # ignored when matching titles, which is the opposite of the test's name.)
    scan = scan_roots(make_library)
    bucketed = find_duplicate(scan, album_name="POP-AID", track_title="x",
                              artist_name="TEMPLIME", artist_scope="loose")
    assert bucketed is not None
    assert set(bucketed.matched) == {"a/ALAC/TEMPLIME/POP-AID"}
    plain = find_duplicate(scan, album_name="POP-AID", track_title="y",
                           artist_name="TEMPLIME", artist_scope="loose")
    assert plain is not None
    assert set(plain.matched) == {"a/TEMPLIME/POP-AID"}
    # Both copies are in scope for the album whichever title is asked about.
    assert {a.relpath for a in scan.by_name["pop-aid"]} == {
        "a/ALAC/TEMPLIME/POP-AID",
        "a/TEMPLIME/POP-AID",
    }


def test_album_name_keeps_single_suffix_and_deluxe(make_library_extra):
    # " - Single", " [Deluxe]" and a leading number are all part of album
    # identity. Every exact name is found ...
    scan = scan_roots(make_library_extra)
    for name in ("4pi", "1st EP", "4 - Leaves", "Song - Single", "Album [Deluxe]"):
        hit = find_duplicate(scan, album_name=name, track_title="t",
                             artist_name=None, artist_scope="loose")
        assert hit is not None, name
        assert hit.matched == (f"extra/{name}",), name
    # ... and the same name with its discriminator removed is a *different* album, which
    # is what "keeps" means. Without this half the test would pass against an
    # implementation that stripped the suffix -- the bug it exists to catch, and the one
    # that would fuse every "<track> - Single" in the real library into one group. (The
    # brief asserted the *exact* name "Album [Deluxe]" misses, which is the inverted
    # claim: it passes only when "[Deluxe]" is stripped.)
    for stripped in ("Album", "Song", "Leaves", "4"):
        assert find_duplicate(scan, album_name=stripped, track_title="t",
                              artist_name=None, artist_scope="loose") is None, stripped


def test_refuses_to_skip_on_an_empty_album_name(make_library_extra, make_library):
    # by_name[""] is real and reachable: the real library has ALAC/薄塩指数/!_, whose name
    # holds no alphanumeric character, and the scanner indexes that key rather than hiding
    # it precisely so this decision can be made here.
    #
    # Both fixtures are rooted at the same `tmp_path / "lib"`, so one scan sees both
    # spellings of the shape at once: `a/!_` is the library side and `extra/・・・` the name
    # a download would carry. They land in one group -- which is the hazard, since the group
    # holds real tracks keyed "t" and a lookup for either spelling would skip a download
    # that has not happened yet. So the guard has to be on the *lookup*, not on the index.
    scan = scan_roots(make_library_extra)
    assert make_library == make_library_extra  # one shared root, so one scan covers both
    assert "" in scan.by_name
    assert {a.relpath for a in scan.by_name[""]} == {"a/!_", "extra/・・・"}
    assert {a.track_keys for a in scan.by_name[""]} == {frozenset({"t"})}
    for punctuation in ("・・・", "!", "_", "...", ""):
        assert find_duplicate(scan, album_name=punctuation, track_title="t",
                              artist_name=None, artist_scope="loose") is None, punctuation


def test_refuses_to_skip_on_an_empty_title(make_library):
    # Must not treat "" as matching every untitled track. "Album Four"
    # holds "..m4a" and "01 ..m4a", both of which normalize to "", and the real library
    # holds 6 such tracks spread over different albums, so a "" title would
    # otherwise skip against any of them.
    scan = scan_roots(make_library)
    assert scan.by_name["album four"][0].track_keys == frozenset({""})
    for title in ("", "...", "・", "01 ..m4a"):
        assert find_duplicate(scan, album_name="Album Four", track_title=title,
                              artist_name=None, artist_scope="loose") is None, title
    # The album that does hold the unusable track is still skipped for a real title, so
    # the guard is a refusal and not a way of turning the album invisible.
    assert find_duplicate(scan, album_name="Album Six", track_title="real",
                          artist_name=None, artist_scope="loose") is not None


def test_unknown_album_returns_none(make_library):
    assert find_duplicate(scan_roots(make_library), album_name="Never Downloaded",
                          track_title="t", artist_name=None,
                          artist_scope="loose") is None


def test_hit_paths_are_sorted_for_stable_display(make_library):
    # `os.walk` order is filesystem-dependent and NTFS does not sort, so two scans of one
    # unchanged tree have to produce one answer, and these strings reach the user through
    # `skip_reason`.
    # NOTE this test cannot catch a missing `sorted()`. On this tree `os.walk` with
    # `dirnames.sort()` already yields `a/...` before `b/...`, so the natural candidate
    # order is already sorted and the assertion holds with and without the call. The mutant
    # is killed by test_matched_is_sorted_even_when_the_scan_order_is_not below, which is
    # the only kind of input that can tell the two apart.
    hit = find_duplicate(scan_roots(make_library), album_name="Hush a by little girl",
                         track_title="track", artist_name=None, artist_scope="loose")
    assert list(hit.matched) == sorted(hit.matched)
    assert list(hit.matched) == ["a/ALAC/鎖那/Hush a by little girl",
                                 "b/new-dl/鎖那/Hush a by little girl"]


def test_matched_is_sorted_even_when_the_scan_order_is_not():
    # The one test that pins `sorted()` on `matched`. Every other assertion in this file is
    # satisfied identically with and without the call, because no real walk of a
    # `dirnames.sort()`ed tree produces out-of-order candidates -- so the sort is proved on
    # a scan built to be wrong, which is the only way to see it.
    #
    # This is the `strip_track_prefix` fixture failure mode again, and worse: `matched` is
    # the evidence a human adjudicates a `loose` skip from, so an unstable order is a
    # display defect on the only field the user is given -- and `DuplicateHit`'s docstring
    # justifies the sort by exactly the condition no fixture can produce.
    #
    # Scrambled rather than reversed, so the assertion tells a sort apart from a reversal
    # of the input: `a` and `z` alone would also pass under `reversed()`.
    scrambled = tuple(
        AlbumDir(root_index=0, relpath=relpath, name="Album", artist="A",
                 track_keys=frozenset({"track"}))
        for relpath in ("b/Album", "a/Album", "c/Album")
    )
    scan = LibraryScan(
        roots=(Path("/nonexistent/lib"),),
        reachable=(True,),
        albums=scrambled,
        by_name=MappingProxyType({"album": scrambled}),
    )
    hit = find_duplicate(scan, album_name="Album", track_title="track", artist_name=None)
    assert hit is not None
    assert hit.matched == ("a/Album", "b/Album", "c/Album")
    # `strict` sorts too. The sort happens after the candidate filter, so it must not be
    # reachable only on the `loose` path.
    strict = find_duplicate(scan, album_name="Album", track_title="track",
                            artist_name="A", artist_scope="strict")
    assert strict is not None
    assert strict.matched == ("a/Album", "b/Album", "c/Album")


def test_artist_scope_defaults_to_loose(make_library):
    # The default, pinned so it cannot be flipped without a failing test. `loose` is the
    # default because it catches 種別 A *and* 種別 B; `strict` misses every collab after the
    # first credited artist, which is most of the 223 duplicated album names.
    scan = scan_roots(make_library)
    implicit = find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                              artist_name="EmoCosine")
    loose = find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                           artist_name="EmoCosine", artist_scope="loose")
    strict = find_duplicate(scan, album_name="らぶふぉーゆー - Single", track_title="t",
                            artist_name="EmoCosine", artist_scope="strict")
    assert implicit == loose
    assert implicit != strict


def test_an_unknown_artist_scope_is_rejected(make_library):
    # A closed set, not a truthy switch. Reading "loose"/"Loose"/"loose " as `loose` would
    # skip tracks the user asked for whenever the typo was meant to be `strict`, and would
    # stop skipping tracks that are already on disk in the other direction. Both are silent
    # and neither is recoverable, so the value names its own error.
    for bad in ("Loose", "STRICT", "loose ", "", "album"):
        with pytest.raises(ValueError, match="artist_scope"):
            find_duplicate(scan_roots(make_library), album_name="POP-AID",
                           track_title="x", artist_name=None, artist_scope=bad)


def test_find_duplicate_never_touches_the_filesystem():
    # The scan is hand-built over a root that does not exist, so nothing here can have been
    # satisfied by reading a disk. This is the property the scheduler relies on when it
    # takes one scan per request and calls this once per track, and it is what lets the
    # match be exercised with values no fixture on disk could produce.
    album = AlbumDir(root_index=0, relpath="artist/album", name="Album",
                     artist="Artist", track_keys=frozenset({"track"}))
    scan = LibraryScan(
        roots=(Path("/nonexistent/lib"),),
        reachable=(True,),
        albums=(album,),
        by_name=MappingProxyType({"album": (album,)}),
    )
    # A rendered download filename, not a tag: the extension and the track number are
    # stripped here, while the album side keeps its own name verbatim.
    hit = find_duplicate(scan, album_name="Album", track_title="1-01 Track.m4a",
                         artist_name="Artist")
    # Both forms, because both are part of the answer now. `matched` is the bare relpath and
    # is unchanged; `resolved` is the same directory with its root, which is the form that
    # resolves and the form `skip_reason` carries.
    assert hit == DuplicateHit(
        matched=("artist/album",),
        resolved=("/nonexistent/lib/artist/album",),
    )
    assert find_duplicate(scan, album_name="Other", track_title="1-01 Track.m4a",
                          artist_name="Artist") is None


def test_resolved_is_sorted_so_two_scans_of_one_tree_give_one_string(tmp_path):
    """Sorting is load-bearing for `skip_reason` and nothing else enforces it.

    `os.walk` order is filesystem-dependent -- NTFS does not sort at all -- and
    `skip_reason` is a *string* that is stored, compared in tests, and shown to a user. Two
    scans of one unchanged tree have to produce the same string, which means the sort cannot
    be dropped.

    **This test cannot catch a missing `sorted()` on this tree**, and that is worth saying
    because it is the same trap as `test_hit_paths_are_sorted_for_stable_display`: with
    `dirnames.sort()` already in `scan_roots` and roots named `a` and `b`, the natural
    candidate order happens to be sorted. So this asserts the *property* of the result --
    it is sorted, and it is a permutation of the unsorted input -- rather than the call,
    which is what makes it survive the mutation where the existing tests do not.
    """
    first = tmp_path / "a"
    second = tmp_path / "b"
    for root in (first, second):
        (root / "artist/album").mkdir(parents=True)
        (root / "artist/album/t.m4a").write_bytes(b"")

    hit = find_duplicate(scan_roots([first, second]), album_name="album",
                         track_title="t.m4a", artist_name="artist")

    assert hit is not None
    assert list(hit.resolved) == sorted(hit.resolved), "resolved must be sorted"
    assert set(hit.resolved) == {str(first / "artist/album"), str(second / "artist/album")}
    # Reversing the roots reverses the scan order but must not the answer.
    reversed_hit = find_duplicate(scan_roots([second, first]), album_name="album",
                                  track_title="t.m4a", artist_name="artist")
    assert reversed_hit is not None
    assert set(reversed_hit.resolved) == set(hit.resolved)
    assert sorted(reversed_hit.resolved) == sorted(hit.resolved)
    assert list(reversed_hit.resolved) == list(hit.resolved), (
        "the same library under a different root order must give the same string, or two "
        "scans of one tree disagree depending on which root the caller listed first"
    )


def test_duplicate_hit_is_immutable():
    # It is handed to the jobs layer and rendered into `skip_reason`; a mutable field
    # would let a caller edit the evidence for a decision that has already been made.
    hit = DuplicateHit(matched=("a/b",), resolved=("/lib/a/b",))
    with pytest.raises((AttributeError, TypeError)):
        hit.matched = ("c/d",)  # type: ignore[misc]
    assert hit.matched == ("a/b",)
    with pytest.raises((AttributeError, TypeError)):
        hit.resolved = ("/other/c/d",)  # type: ignore[misc]
    assert hit.resolved == ("/lib/a/b",)


def test_resolved_may_not_be_defaulted_away():
    """`resolved: tuple[str, ...] = ()` passes the entire suite, and must not.

    A default is the only thing standing between a caller and a `DuplicateHit` whose
    `resolved` is empty -- which `app.py::_skip_reason` would then join into a `skip_reason`
    naming nothing, and the queue page would render an empty path list. That is the exact
    defect round 1 removed, reachable again by one character of convenience, and the
    docstring's reasoning about it is not a guard: a docstring does not raise.

    A test rather than a comment, because this is a *shape* requirement and shape is the one
    thing a type annotation does not enforce at runtime.
    """
    with pytest.raises(TypeError, match="resolved"):
        DuplicateHit(("a/b",))  # type: ignore[call-arg]
    # And the message names the field, rather than only "missing 1 required positional
    # argument" -- which is the difference between a caller knowing what to do and guessing.
    with pytest.raises(TypeError) as excinfo:
        DuplicateHit(matched=("a/b",))  # type: ignore[call-arg]
    assert "resolved" in str(excinfo.value)
    # The keyword form is the one callers actually use, and it works.
    assert DuplicateHit(matched=("a/b",), resolved=("/lib/a/b",)).resolved == ("/lib/a/b",)


def test_find_duplicate_is_cheap_enough_to_call_per_track(make_library):
    # The cost guard for *this* function. The measurement that settles
    # that a scan per request is affordable is in `library_scan`'s module docstring, and
    # `scan_roots` has its own budget in
    # `test_library_scan.py`; `find_duplicate` is called once per leaf track on top of that
    # scan and had no bound, so nothing would have complained if it started doing real work.
    #
    # A budget, and a deliberately tight one. Measured: 0.0069 ms per call against the real
    # 4,739-album-dir library, so 0.1 ms is ~14x headroom, while a per-call directory walk
    # of this fixture measures 0.42 ms and so fails it by 4x. A 1 ms budget would have let
    # that walk through -- the aggregate suite time goes 0.20 s -> 1.04 s but no assertion
    # moves. Headroom is safe because this is a mean over 2,000 calls, not a single one.
    # test_find_duplicate_issues_no_syscall_per_call is the exact detector for the same
    # mutation; this is what stops the pure comparison itself from creeping.
    scan = scan_roots(make_library)
    album = next(a for a in scan.albums if a.relpath == "a/ALAC/鎖那/Hush a by little girl")
    # A hit with two candidates, so the sample covers the filter-and-sort path rather than
    # an early miss. `strict` because it is the longer of the two code paths.
    def call() -> DuplicateHit | None:
        return find_duplicate(scan, album_name=album.name, track_title="track",
                              artist_name="鎖那", artist_scope="strict")

    assert call() is not None  # warm up, so the first call's cost is not the sample
    calls = 2000
    t0 = time.perf_counter()
    for _ in range(calls):
        call()
    per_call_ms = (time.perf_counter() - t0) / calls * 1000
    assert per_call_ms < 0.1, f"{per_call_ms:.4f} ms per call (budget 0.1 ms)"


def test_find_duplicate_issues_no_syscall_per_call(make_library, monkeypatch):
    # What makes the budget above meaningful, and what a timing assertion cannot do: prove
    # the per-call cost is a few dict and string operations rather than a directory read.
    # Any filesystem call raises, so adding one -- `os.walk`, a `stat`, a `Path.exists`
    # re-check, a `listdir` -- fails the test whatever it costs.
    scan = scan_roots(make_library)

    def poisoned(*args, **kwargs):
        raise AssertionError("find_duplicate touched the filesystem")

    # `monkeypatch.context()` rather than the fixture: pytest's own teardown, tmp_path
    # cleanup and cache provider all call `os.stat` and `Path.exists`, so a patch left
    # installed past the end of the body fails the session instead of the test. Only the
    # calls below run under the poison.
    with monkeypatch.context() as patch:
        for name in ("walk", "scandir", "listdir", "stat", "lstat", "open"):
            patch.setattr(os, name, poisoned)
        for name in ("stat", "exists", "is_dir", "is_file", "iterdir", "glob", "rglob",
                     "open"):
            patch.setattr(Path, name, poisoned)
        patch.setattr(builtins, "open", poisoned)

        hit = find_duplicate(scan, album_name="Hush a by little girl", track_title="track",
                             artist_name="鎖那", artist_scope="strict")
        assert hit is not None
        # 種別 A survives `strict`: both placements sit under the same artist directory, so
        # the artist check keeps both. That is the whole reason `strict` is not useless --
        # it is only 種別 B it cannot see.
        assert hit.matched == ("a/ALAC/鎖那/Hush a by little girl",
                               "b/new-dl/鎖那/Hush a by little girl")
        # ... and the miss paths too, which a guard placed after the candidates are fetched
        # would skip past.
        assert find_duplicate(scan, album_name="Hush a by little girl", track_title="absent",
                              artist_name="鎖那", artist_scope="strict") is None
        assert find_duplicate(scan, album_name="Never Downloaded", track_title="track",
                              artist_name="鎖那", artist_scope="loose") is None
    # Still correct with the poison removed, so the assertions above are about
    # `find_duplicate` and not about the scan that was taken before it.
    assert find_duplicate(scan, album_name="Hush a by little girl", track_title="track",
                          artist_name="鎖那", artist_scope="strict") is not None


# --------------------------------------------------------------------------- #
# The two forms, and the reason there are two
# --------------------------------------------------------------------------- #
def test_resolved_paths_name_a_directory_that_exists_under_each_root(tmp_path):
    """The defect this field exists to fix, on a real two-root tree.

    `find_duplicate` pools candidates out of `scan.by_name`, which spans every configured
    root, so one album name filed under both roots produces two matched entries -- and the
    bare relpaths of `matched` each resolve under *one* root and not the other. On the real
    two-root library every single sampled hit was like this (493 of 493), which is what makes
    it worth a field rather than a note: the user is promised they can adjudicate a `loose`
    skip by opening the paths, and there was nothing to open.

    The assertion is about the filesystem, not about the string: every entry in `resolved`
    has to be a directory that exists, which is the property that was missing and the one
    `skip_reason` inherits.
    """
    first = tmp_path / "library-a"
    second = tmp_path / "library-b"
    for root in (first, second):
        (root / "9Lana/Let me battle - Single").mkdir(parents=True)
        (root / "9Lana/Let me battle - Single/1-01 track.m4a").write_bytes(b"")

    hit = find_duplicate(scan_roots([first, second]), album_name="Let me battle - Single",
                         track_title="1-01 track.m4a", artist_name="9Lana")

    assert hit is not None
    # `matched` is unchanged: bare relpaths, sorted, one entry per matched directory.
    assert hit.matched == ("9Lana/Let me battle - Single", "9Lana/Let me battle - Single")
    # Two matched directories, one distinct string. The bare form cannot say which is which,
    # and the string it does have resolves under *both* roots, so it does not identify a
    # directory. Asserted as a count so `resolved` cannot be quietly dropped without this
    # going red.
    assert len(set(hit.matched)) == 1 < len(hit.resolved)
    # `resolved` keeps both, each with the root it was found under, and every entry opens.
    assert hit.resolved == (
        f"{first}/9Lana/Let me battle - Single",
        f"{second}/9Lana/Let me battle - Single",
    )
    for entry in hit.resolved:
        assert Path(entry).is_dir(), f"{entry} does not resolve"


def test_resolved_collapses_a_root_that_is_itself_an_album_scope(tmp_path):
    """`relpath` is "." for a root holding audio directly, and `str()` must not keep it.

    The real library does this 10 times -- a stray `.m4a` sitting in a root rather than in a
    directory under it. `Path("/lib") / "."` is `PosixPath("/lib")`, so the entry is the root
    and not a trailing `/.`; the point is that the string still has to be something a caller
    can hand straight to `os.path.isdir`.

    Hand-built rather than scanned, and that is not laziness: a root-scope album has an empty
    `name`, so `album_key` folds it to `""`, so it never enters `by_name` and
    `find_duplicate` can never return it as a candidate (see
    `test_a_root_scope_album_is_never_a_match_candidate` below, which pins that). This test is
    therefore about the *expression*, and the one after it is about the scan.
    """
    root = tmp_path / "loose-root"
    root.mkdir()
    album = AlbumDir(root_index=0, relpath=".", name="Loose Root", artist=None,
                     track_keys=frozenset({"stray track"}))
    scan = LibraryScan(
        roots=(root,),
        reachable=(True,),
        albums=(album,),
        by_name=MappingProxyType({"loose root": (album,)}),
    )

    hit = find_duplicate(scan, album_name="Loose Root", track_title="stray track.m4a",
                         artist_name=None)

    assert hit is not None
    assert hit.matched == (".",)
    assert hit.resolved == (str(root),)
    assert Path(hit.resolved[0]).is_dir()


def test_a_root_scope_album_is_never_a_match_candidate(tmp_path):
    """The guard that makes the test above unreachable through a real scan.

    A root holding audio directly produces an album with `name == ""`, and `album_key("")` is
    `""`, so it is kept out of `by_name` entirely. `find_duplicate` refuses an empty scope key
    for the same reason it refuses an empty title key: `"" == ""` would match every unusable
    name in the library at once, and a re-download is recoverable while a false skip is not.

    So `resolved` can never contain a "."-relpath in practice, and the case the expression
    above handles is defensive rather than live. Worth saying out loud, because "it cannot
    happen" is the kind of claim that quietly stops being true when the index changes.
    """
    root = tmp_path / "loose-root"
    root.mkdir()
    (root / "stray track.m4a").write_bytes(b"")

    scan = scan_roots([root])

    assert scan.by_name == {}, "an empty album name must not be indexed"
    assert find_duplicate(scan, album_name=root.name, track_title="stray track.m4a",
                          artist_name=None) is None
