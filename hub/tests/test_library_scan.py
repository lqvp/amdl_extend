"""Album discovery over multiple messy library roots (spec §7.3 Step 1, §8).

The scanner is the only part of dedup that touches the filesystem, and it is the part
that has to survive a library nobody curated: 212 artist directories on one drive, a
second drive laid out `[ALAC|Atmos/]artist/album/` with loose files and stray
subdirectories, and an external drive that may simply not be mounted when a download is
requested. Every test here is pinned to one of those observations, and the fixtures are
the §12.1 regression shapes.
"""

from __future__ import annotations

import time

import pytest

from hub.library_scan import _artist, _relpath, scan_roots


def test_finds_album_dirs_at_the_shallowest_level(make_library):
    scan = scan_roots(make_library)
    names = {a.relpath for a in scan.albums}
    assert "a/ALAC/鎖那/Hush a by little girl" in names
    assert "Nyarons" in names  # loose files directly under artist
    assert "a/TEMPLIME" in names  # dir with both files and subdirs


def test_an_album_dirs_keys_do_not_include_a_subdirectorys_tracks(make_library):
    # "TEMPLIME" is an album dir because it holds HIKO.flac; "TEMPLIME/Escapism"
    # is a separate album dir, and TEMPLIME/Escapism's keys are not merged in.
    scan = scan_roots(make_library)
    temp = next(a for a in scan.albums if a.relpath == "a/TEMPLIME")
    assert temp.track_keys == frozenset({"hiko"})
    # "t" is Escapism's only track, "x"/"y" are POP-AID's. None of them may be hoisted
    # into the parent's scope: a key that really belongs to another album makes a
    # download into TEMPLIME skip against POP-AID, and the other way round.
    assert "t" not in temp.track_keys
    assert "x" not in temp.track_keys


def test_the_walk_descends_past_an_album_dir_into_its_subdirectories(make_library):
    # The counterpart of the test above. "Not descending" is a statement about which keys
    # belong to a scope, not about the traversal: TEMPLIME/Escapism is its own album dir
    # and the walk has to reach it, or the scope for it silently disappears and a download
    # into Escapism runs again.
    scan = scan_roots(make_library)
    escapism = next(a for a in scan.albums if a.relpath == "a/TEMPLIME/Escapism")
    assert escapism.track_keys == frozenset({"t"})


def test_groups_same_named_album_dirs_across_roots(make_library):
    scan = scan_roots(make_library)
    hush = scan.by_name["hush a by little girl"]
    assert {a.relpath for a in hush} == {
        "a/ALAC/鎖那/Hush a by little girl",
        "b/new-dl/鎖那/Hush a by little girl",
    }


def test_album_name_index_keeps_leading_digits(make_library_extra):
    # A number glued to a word is not a track number, so "4pi" and "1st EP" are reachable
    # whichever way strip_track_prefix is set. That makes this a statement about
    # `normalize` -- §7.5 step 4 must not eat a name that merely starts with digits -- and
    # NOT a statement about the index's flag, which it cannot be. The flag is pinned by
    # test_album_name_index_keeps_a_number_a_separator_follows.
    scan = scan_roots(make_library_extra)
    assert "4pi" in scan.by_name
    assert "1st ep" in scan.by_name


def test_album_name_index_keeps_a_number_a_separator_follows(make_library_extra):
    # The assertion that can actually tell strip_track_prefix=False from True, i.e. the
    # one that catches a by_name index built with the wrong flag -- the most damaging
    # silent failure in the whole dedup path, since every album lookup would miss and
    # nothing would ever be skipped.
    #
    # No other name in the tree can. "4pi" and "1st EP" have no separator after the
    # digits, and a 4-digit year is out of `\d{1,3}`'s reach on purpose (§7.5), so all
    # three produce the same key under either flag and pass with it wrong. "4 - Leaves"
    # has the separator the pattern requires: with the flag on the index would answer to
    # "leaves", and Task 4 -- which must look up with the flag off -- would never find it.
    scan = scan_roots(make_library_extra)
    assert "4 - leaves" in scan.by_name
    assert "leaves" not in scan.by_name


def test_a_track_number_is_still_stripped_from_track_keys(make_library_extra):
    # The other half of the same flag: off for the directory name, on for the file. If
    # this ever came out as "01 t", every re-download of the same track would render a
    # new key and nothing would match.
    scan = scan_roots(make_library_extra)
    assert scan.by_name["4 - leaves"][0].track_keys == frozenset({"t"})


def test_album_name_index_keeps_single_and_deluxe_suffixes(make_library_extra):
    # spec §7.5: " - Single" and " [Deluxe]" are album identity, not decoration.
    scan = scan_roots(make_library_extra)
    assert "song - single" in scan.by_name
    assert "album [deluxe]" in scan.by_name


def test_an_album_name_that_normalizes_to_nothing_is_still_indexed(make_library):
    # D8. The real library has `ALAC/薄塩指数/!_`: a directory name with no alphanumeric
    # character in it, so `normalize` answers "".
    #
    # It stays in the index because the *album* side has no empty-key guard anywhere: the
    # plan's one guard is on the track title (Task 4 refuses to skip when the title key is
    # empty), so `by_name[""]` is reachable for a download whose album name is
    # punctuation-only, and dropping the key would make that download re-fetch an album
    # already on disk. Whether such a match *should* be trusted is Task 4's call -- two
    # punctuation-only album names being the same release is plausible, and a guard there
    # is a decision for the dedup layer, not for the scanner.
    #
    # An empty *title* is the other case and behaves differently by accident, not by
    # design: `""` in `track_keys` is findable for the same reason, and Task 4's title
    # guard is what stops it from matching everything. (Review Focus #2.)
    scan = scan_roots(make_library)
    assert "" in scan.by_name
    (album,) = scan.by_name[""]
    assert album.name == "!_"
    assert album.track_keys == frozenset({"t"})


def test_derives_artist_from_structure(make_library):
    scan = scan_roots(make_library)
    hush = scan.by_name["hush a by little girl"][0]
    assert hush.artist == "鎖那"
    nyarons = next(a for a in scan.albums if a.relpath == "Nyarons")
    assert nyarons.artist is None  # undeterminable, not a guess


def test_artist_of_a_depth_two_album_dir(make_library):
    # The wiring site, not the rule. `scan_roots` hands `_artist` the relpath split into
    # components, and slicing that wrong at the call site is invisible to every test of
    # `_artist` as a function: dropping components there only decides which element
    # `[-2]` picks, so the same artist comes out and the suite stays green while the
    # scanner reports none.
    #
    # This is the shape that matters. 1,069 album dirs in `downloads/` and 40 in the
    # external library are `<root>/<artist>/<album>`, and it is the shallowest one the
    # scanner can meet, so it is the only fixture entry that reaches it: everything else
    # in this tree sits at depth 3 or more behind the `a/` prefix.
    scan = scan_roots([make_library / "a"])
    album = next(a for a in scan.albums if a.relpath == "Artist/Album")
    assert album.artist == "Artist"
    # and the same directory one level down, where `a/` is the artist
    deeper = next(
        a for a in scan_roots(make_library).albums if a.relpath == "a/Artist/Album"
    )
    assert deeper.artist == "Artist"


def test_artist_of_a_codec_directory_that_is_not_below_a_codec_directory(make_library):
    # The one input the removed bucket rule got wrong, read off the scanner this time.
    # `Some/Atmos/Album` has a codec directory as its parent and a plain directory above
    # that, so the grandparent check could not have fired for it either way; the answer
    # comes from the plain parent rule and nothing else.
    scan = scan_roots([make_library / "a"])
    album = next(a for a in scan.albums if a.relpath == "Some/Atmos/Album")
    assert album.artist == "Atmos"


def test_artist_is_the_parent_directory_name(make_library):
    # §8: the parent is the artist, full stop. There is no codec-bucket exception -- a
    # bucket sits between the artist and the root, never between the artist and the album
    # -- so `ALAC/TEMPLIME/POP-AID` and `TEMPLIME/POP-AID` answer the same without one.
    scan = scan_roots(make_library)
    bucketed = next(a for a in scan.albums if a.relpath == "a/ALAC/TEMPLIME/POP-AID")
    plain = next(a for a in scan.albums if a.relpath == "a/TEMPLIME/POP-AID")
    assert bucketed.artist == plain.artist == "TEMPLIME"
    assert scan.by_name["escapism"][0].artist == "TEMPLIME"


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ((), None),  # the root itself: no parent below the root to name
        (("Nyarons",), None),  # directly in the root -- Music/Nyarons/A.flac
        (("TEMPLIME", "POP-AID"), "TEMPLIME"),
        (("ALAC", "TEMPLIME", "POP-AID"), "TEMPLIME"),  # a bucket above the artist
        (("ALAC", "EmoCosine", "らぶふぉーゆー - Single"), "EmoCosine"),
        # The last two are the rule with no codec-bucket exception in it. A version that
        # special-cases a bucket parent answers None to both, and answering None there is
        # no less of a guess than answering the bucket name: the real library has no such
        # directory, so the exception would only ever fire on inputs nobody has.
        (("ALAC", "Atmos", "Some Album"), "Atmos"),  # an album with no artist level
        (("Some", "Atmos", "Album"), "Atmos"),  # a bucket that is not below a bucket
    ],
)
def test_artist_is_the_parent_name_and_nothing_else(parts, expected):
    # Table-driven so the rule is pinned as a mapping, not through whatever happens to be
    # in the fixture tree. `parts` is the relpath below the root, album directory
    # included. Note that re-introducing the grandparent check alone cannot be caught by
    # any test: both of its branches return the parent, so it is a no-op, and the only
    # thing to pin is that no *other* rule crept in.
    assert _artist(parts) == expected


def test_an_album_with_no_artist_level_reports_the_codec_directory(tmp_path):
    # `ALAC/Atmos/Some Album/` has no artist level, so the parent really is the codec
    # directory and "Atmos" is what the rule answers. Pinned because it is the one input
    # the rule gets wrong, so nobody later assumes it is handled.
    album_dir = tmp_path / "lib" / "ALAC" / "Atmos" / "Some Album"
    album_dir.mkdir(parents=True)
    (album_dir / "01 t.m4a").write_bytes(b"")
    (album,) = scan_roots([tmp_path / "lib"]).albums
    assert album.artist == "Atmos"


def test_artist_of_a_dir_whose_parent_is_the_root_is_unknown(make_library):
    # The real shape is Music/Nyarons/A.flac: the parent is the library root, and
    # reporting the root's own name as the artist would put "Music" in the library UI
    # and make artist_scope="strict" compare against it. Unknown, not the root name.
    scan = scan_roots(make_library)
    nyarons = next(a for a in scan.albums if a.relpath == "Nyarons")
    assert nyarons.artist is None
    assert nyarons.name == "Nyarons"


def test_a_root_that_is_itself_an_album_dir_is_still_a_scope(tmp_path):
    # Loose files at the top of a library: the root is a directory that directly holds
    # audio, so it is an album dir like any other. Excluding it would make every one of
    # those files invisible to dedup, and the miss is silent -- the download simply runs
    # again. Its relpath is "." (the pathlib convention), it has no parent, so no artist,
    # and it has no album name, so it is not in `by_name`.
    root = tmp_path / "lib"
    root.mkdir()
    (root / "loose.m4a").write_bytes(b"")
    (root / "cover.jpg").write_bytes(b"")
    scan = scan_roots([root])
    (only,) = scan.albums
    assert only.relpath == "."
    assert only.artist is None
    assert only.track_keys == frozenset({"loose"})


def test_the_root_scope_is_not_indexed_as_an_album_name(tmp_path):
    # A mount point is not an album. `by_name` is keyed by album name, so the root's
    # basename in it would join the group of any real album with that name -- and one
    # drive has more than one basename ("Music", "HDD_Music"), so the key would depend on
    # how the path happened to be spelled. "" is not a name any directory can have.
    root = tmp_path / "lib"
    root.mkdir()
    (root / "loose.m4a").write_bytes(b"")
    # a real album that the mount-point name would otherwise collide with
    (root / "Music").mkdir()
    (root / "Music" / "01 t.m4a").write_bytes(b"")
    scan = scan_roots([root])
    root_scope = next(a for a in scan.albums if a.relpath == ".")
    music = next(a for a in scan.albums if a.relpath == "Music")
    assert root_scope.name == ""
    assert music.name == "Music"
    assert set(scan.by_name) == {"music"}
    assert [a.relpath for a in scan.by_name["music"]] == ["Music"]


def test_by_name_is_the_same_however_the_root_is_spelled(tmp_path):
    # The user's own configuration is the symlink /home/m/Music/HDD_Music ->
    # /run/media/.../Music, and §8.1 probes reachability from more than one call site, so
    # two spellings of one drive must produce one index. With the root's basename in
    # `by_name` this failed: the drive's own scope was keyed "music" one way and
    # "hdd_music" the other, and the two scans compared unequal.
    real = tmp_path / "1A5E05A75E057D2F" / "Music"
    (real / "Artist" / "Album").mkdir(parents=True)
    (real / "Artist" / "Album" / "01 t.m4a").write_bytes(b"")
    # makes the root a scope, as the real drive is
    (real / "loose.m4a").write_bytes(b"")
    link = tmp_path / "HDD_Music"
    link.symlink_to(real)

    direct = scan_roots([real])
    via_link = scan_roots([link])

    assert direct.albums == via_link.albums
    assert dict(direct.by_name) == dict(via_link.by_name)
    assert set(direct.by_name) == {"album"}
    # and neither spelling of the drive contributes a key
    for basename in (real.name, link.name):
        assert basename.casefold() not in direct.by_name


def test_is_audio_file_decides_what_is_a_track(make_library):
    # The brief makes `is_audio_file` mandatory, and every one of its boundaries needs a
    # test that a raw membership check would fail. None of these two files exist in the
    # real library -- every extension there is lowercase, and no file is named ".m4a" --
    # which is exactly why they have to be constructed:
    #   os.path.splitext(name)[1] in AUDIO_EXTS   -> drops "cover" (".FLAC" is not a member)
    #   name.lower().endswith(tuple(AUDIO_EXTS))   -> keeps ".m4a", which has no extension
    # A casefolding implementation passes both, which is fine: case-insensitivity is the
    # contract, the mechanism is not.
    scan = scan_roots(make_library)
    seven = scan.by_name["album seven"][0]
    assert seven.track_keys == frozenset({"real", "cover"})
    # the leading dot marks a hidden file, not an extension
    assert ".m4a" not in seven.track_keys


def test_part_files_are_not_tracks(make_library):
    # Review Focus #5: 160 .part files exist in the real library
    scan = scan_roots(make_library)
    six = scan.by_name["album six"][0]
    assert "real" in six.track_keys
    assert not any(".part" in k for k in six.track_keys)


def test_a_directory_of_only_part_files_is_not_an_album_dir(tmp_path):
    # The complement of the test above: excluding .part from the key set is not enough,
    # it must also not let an interrupted download's directory become an album scope.
    root = tmp_path / "lib" / "Interrupted" / "Album Seven"
    root.mkdir(parents=True)
    (root / "01 real.m4a.part").write_bytes(b"")
    (root / "cover.jpg").write_bytes(b"")
    scan = scan_roots([tmp_path / "lib"])
    assert scan.albums == ()


def test_empty_titles_are_representable_but_distinguishable(make_library):
    # Review Focus #2: normalize("") == "" must be findable so dedup can reject it
    scan = scan_roots(make_library)
    four = scan.by_name["album four"][0]
    assert "" in four.track_keys


def test_unreachable_root_is_marked_degraded(tmp_path, make_library):
    roots = [make_library / "a", tmp_path / "not-mounted"]
    scan = scan_roots(roots)
    assert scan.reachable == (True, False)
    assert scan.degraded == (tmp_path / "not-mounted",)
    assert len(scan.albums) > 0  # the good root still works (Review Focus #4)


def test_every_root_appears_in_order_with_its_own_index(make_library, tmp_path):
    # roots / reachable are positional, and root_index is the only thing that ties an
    # album dir back to its root. Losing the pairing makes the per-root degraded banner
    # impossible to attribute.
    roots = [make_library / "a", tmp_path / "gone", make_library / "b"]
    scan = scan_roots(roots)
    assert scan.roots == tuple(roots)
    assert scan.reachable == (True, False, True)
    assert scan.degraded == (tmp_path / "gone",)
    assert {a.root_index for a in scan.albums} == {0, 2}
    for album in scan.albums:
        assert (roots[album.root_index] / album.relpath).is_dir()


def test_root_that_is_a_symlink_is_accepted(tmp_path, make_library):
    # Review Focus #1: the real path is /home/m/Music/HDD_Music -> /run/media/...
    link = tmp_path / "HDD_Music"
    link.symlink_to(make_library / "a")
    scan = scan_roots([link])
    assert scan.reachable == (True,)
    assert len(scan.albums) > 0
    # and every relpath must be judged relative to the root the caller passed,
    # not to its resolved target
    assert all(not a.relpath.startswith("/") for a in scan.albums)


def test_a_walked_path_outside_the_root_raises_instead_of_finding_nothing():
    # The loud half of Review Focus #1. Resolving one side of the walk and not the other
    # is what makes every entry look like it lives outside the root; the symptom is a
    # scan that reports no albums and no error, so every download runs again. A root
    # configured with a redundant component, or a root normalized in one place and walked
    # in another, produces exactly this. _relpath refuses instead of returning "".
    with pytest.raises(ValueError, match="must both be used unresolved"):
        _relpath("/somewhere/else/Album", "/library/b", "/library/b/")


def test_a_symlinked_directory_inside_a_root_is_not_followed(tmp_path, make_library):
    # A symlinked *root* is followed (Review Focus #1 -- that is the user's own
    # HDD_Music). A symlinked directory *inside* a root is not: os.walk is called
    # without followlinks, so `Linked` is listed but never visited. That keeps a
    # per-request scan bounded -- a link back up to an ancestor would otherwise recurse
    # forever on every download request -- and it keeps every reported path inside the
    # root the user configured, which §11's file-serving check depends on. The real
    # library contains no symlinks at all, so nothing is lost here.
    real = tmp_path / "elsewhere" / "Real Album"
    real.mkdir(parents=True)
    (real / "t.m4a").write_bytes(b"")
    (make_library / "a" / "Linked").symlink_to(real)
    scan = scan_roots([make_library / "a"])
    relpaths = {a.relpath for a in scan.albums}
    assert "Linked" not in relpaths
    assert not any("elsewhere" in rp for rp in relpaths)
    # and the rest of the walk is unaffected by the link's presence
    assert "ALAC/鎖那/Hush a by little girl" in relpaths


def test_repeated_scans_of_one_tree_agree(tmp_path, make_library):
    # The filesystem is the only source of truth and there is no cache (§7.1.1), so
    # every request re-walks. Two scans must not differ, otherwise a skip depends on
    # which request happened to ask.
    first = scan_roots([make_library / "a"])
    second = scan_roots([make_library / "a"])
    assert first.albums == second.albums
    assert dict(first.by_name) == dict(second.by_name)


def test_a_later_change_is_visible_without_a_rescan_hook(tmp_path, make_library):
    # The direct consequence of "no cache": a directory created after the last scan is
    # found by the next one. If this ever needs a call to drop a cache, §7.1's
    # statelessness has been broken.
    root = make_library / "a"
    before = scan_roots([root])
    (root / "Brand New" / "01 t.m4a").parent.mkdir(parents=True)
    (root / "Brand New" / "01 t.m4a").write_bytes(b"")
    after = scan_roots([root])
    assert "brand new" not in before.by_name
    assert "brand new" in after.by_name


def test_no_roots_yields_an_empty_but_valid_scan():
    scan = scan_roots([])
    assert scan.roots == ()
    assert scan.reachable == ()
    assert scan.albums == ()
    assert dict(scan.by_name) == {}
    assert scan.degraded == ()


def test_a_root_that_is_a_file_is_degraded_not_fatal(tmp_path):
    # /library/b is a file, or a symlink dangling, or an unplugged drive: whatever it
    # is, one bad root must not take the scan down. The hub is single-user and the
    # second library is 341 GB, so "no dedup at all" must be visible, not silent.
    path = tmp_path / "not-a-dir"
    path.write_bytes(b"")
    scan = scan_roots([path])
    assert scan.reachable == (False,)
    assert scan.degraded == (path,)
    assert scan.albums == ()


def test_scan_is_fast_enough_to_run_per_request(make_library):
    # spec §7.1.1 measured 0.06 s for `os.walk` over 4,367 dirs / 10,184 files, and
    # 0.091 s for a full `scan_roots` of the same library, so the design re-scans per
    # request and keeps no cache. This is a per-file cost guard rather than a wall-clock
    # guard: the fixture is tiny, so the only thing it can catch is a scan that does
    # something superlinear or re-stats.
    roots = [make_library]
    scan_roots(roots)  # warm the page cache so the first run is not the outlier
    t0 = time.perf_counter()
    for _ in range(20):
        scan_roots(roots)
    per_scan_ms = (time.perf_counter() - t0) / 20 * 1000
    assert per_scan_ms < 50


def test_album_dirs_are_reported_in_a_stable_per_directory_order(make_library):
    # relpaths reach the user in skip_reason and in the library list, so two scans of one
    # unchanged tree have to come out in the same order. os.walk yields directories in
    # readdir order and NTFS does not sort, so this is a property of the scan.
    first = [a.relpath for a in scan_roots(make_library).albums]
    second = [a.relpath for a in scan_roots(make_library).albums]
    assert first == second
    # What is guaranteed is sorted *siblings*, not a sorted list of relpaths: a
    # directory's subtree stays contiguous, and children are visited in sorted order
    # within their parent.
    siblings: dict[str, list[str]] = {}
    for relpath in first:
        parent, _, base = relpath.rpartition("/")
        siblings.setdefault(parent, []).append(base)
    assert all(names == sorted(names) for names in siblings.values())
    # "a/A/CD 1" is nested inside "a/A" and "a/A B" is their sibling, so this is where
    # per-directory order and a global sort of the finished relpath strings disagree:
    # comparing "a/A " against "a/A/" puts the space (0x20) before the slash (0x2F), so a
    # global sort would report "a/A B" first. The fixture holds all three directories so
    # this cannot pass by coincidence.
    assert first.index("a/A/CD 1") < first.index("a/A B")
    assert sorted(first).index("a/A B") < sorted(first).index("a/A/CD 1")


def test_per_root_counts_each_root_separately(tmp_path):
    """Positional with `roots` and `reachable`, so an index means one thing everywhere.

    `tmp_path` rather than the `make_library` fixture, because the counts here are the
    assertion and a 21-album regression tree would make the expected numbers depend on a
    fixture this test is not about.
    """
    first = tmp_path / "a" / "artist" / "album"
    second = tmp_path / "b" / "artist" / "album2"
    third = tmp_path / "b" / "artist2" / "album3"
    for directory in (first, second, third):
        directory.mkdir(parents=True)
        (directory / "t.m4a").write_bytes(b"")

    scan = scan_roots([tmp_path / "a", tmp_path / "b"])

    assert scan.per_root() == (1, 2)
    assert len(scan.per_root()) == len(scan.roots) == len(scan.reachable)
    assert sum(scan.per_root()) == len(scan.albums)


def test_per_root_keeps_the_slot_of_a_root_that_could_not_be_read(tmp_path):
    """A rejected root keeps its index, exactly as `roots` and `reachable` do.

    If this one dropped its slot the counts would be positional-but-wrong, and the whole
    point of the field is that a caller can zip it against `roots` without having to
    re-derive which root is which. A count of `[1]` for two roots is the bug: it silently
    attributes the surviving root's album to whichever root the caller guessed.
    """
    good = tmp_path / "a" / "artist" / "album"
    good.mkdir(parents=True)
    (good / "t.m4a").write_bytes(b"")

    scan = scan_roots([tmp_path / "a", tmp_path / "does-not-exist"])

    assert scan.degraded == (tmp_path / "does-not-exist",)
    assert scan.per_root() == (1, 0)
    assert dict(zip(scan.roots, scan.per_root(), strict=True)) == {
        tmp_path / "a": 1,
        tmp_path / "does-not-exist": 0,
    }


def test_per_root_reports_zero_for_a_directory_that_is_there_and_holds_nothing(tmp_path):
    """The case this method exists for, and it is not `degraded`.

    An empty but *readable* root is the state a not-plugged-in drive lands in once Docker has
    autocreated the mount point. It reads as healthy, so `degraded_roots` is empty and the
    only thing that distinguishes it from an intentionally empty library is this count.
    Nothing on disk can settle which of the two it is, which is why the count is surfaced for
    a human rather than turned into a boolean here.
    """
    empty = tmp_path / "empty"
    empty.mkdir()

    scan = scan_roots([empty])

    assert scan.degraded == ()
    assert scan.reachable == (True,)
    assert scan.per_root() == (0,)
