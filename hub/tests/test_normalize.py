"""`normalize()` is the only thing keeping a downloaded track from matching itself, so its
boundaries are pinned here rather than left to the dedup call site (spec §7.5)."""

from hub.normalize import (
    AUDIO_EXTS,
    FORMAT_BUCKETS,
    is_audio_file,
    normalize,
    stem_of,
)


def test_stem_of_drops_only_a_known_audio_extension():
    assert stem_of("1-01 Caribbean Blue.m4a") == "1-01 Caribbean Blue"
    assert stem_of("Song. Pt. 2.m4a") == "Song. Pt. 2"
    assert stem_of("no-extension") == "no-extension"
    assert stem_of("01. Title.M4A") == "01. Title"  # the suffix match is case-insensitive


def test_stem_of_keeps_a_name_whose_dot_is_not_an_extension():
    # `playlistSongNameFormat` renders "{index:02d}. {artist} - {title}". Under
    # PurePath.stem semantics "01. Artist - Title" collapses to "01", which would key every
    # playlist-downloaded track to a bare index and let two tracks sharing an index inside
    # one album scope falsely match. Only a known audio extension is a suffix.
    assert stem_of("01. Artist - Title") == "01. Artist - Title"
    assert stem_of("1. Title") == "1. Title"
    assert stem_of("1. Title.m4a") == "1. Title"


def test_normalize_strips_leading_track_numbers():
    assert normalize("1-01 Title") == "title"
    assert normalize("01 Title") == "title"
    assert normalize("1. Title") == "title"
    # the real playlistSongNameFormat render, not a synthetic name
    assert normalize("01. Artist - Title") == "artist - title"
    # max 2 numeric groups; a third stays
    assert normalize("1-01-02 Title") == "02 title"


def test_normalize_can_keep_track_numbers_for_album_names():
    # album dirs are matched with strip_track_prefix=False (spec §7.5). "4pi" and "1st EP"
    # have no separator after the leading digit, so they are unchanged either way and
    # document intent without discriminating -- the literals below are what prove the
    # flag is wired up at all.
    assert normalize("4pi", strip_track_prefix=False) == "4pi"
    assert normalize("1st EP", strip_track_prefix=False) == "1st ep"
    # These two *are* flag-sensitive. Deleting the `if strip_track_prefix:` guard from
    # `normalize` must fail here; see the mutation check in the report.
    assert normalize("01 Title", strip_track_prefix=False) == "01 title"
    assert normalize("01 Title", strip_track_prefix=True) == "title"


def test_normalize_keeps_a_four_digit_year():
    # `\d{1,3}` cannot span four digits, so the year survives. That is deliberate
    # (§7.5 step 4): it makes the rendered "01-1979 - Song" match "1979 - Song" instead of
    # collapsing both onto an unrelated "Song" and falsely skipping.
    assert normalize("1979 - Song.m4a") == "1979 - song"
    assert normalize("01-1979 - Song") == "1979 - song"
    # A 3-digit group with a separator is still a track number, so widening the cap to
    # \d{1,4} would eat the year and break the pair above.
    assert normalize("197 Title") == "title"


def test_normalize_folds_decomposed_and_composed_equally():
    # Written as escapes on purpose. Typed as literal text the two forms render
    # identically, so an editor normalising the file would silently leave
    # composed-vs-composed -- a tautology that asserts nothing, which is the defect class
    # the plan flags as F3. The guard fails loudly if that ever happens again.
    composed = "Caf\u00e9"      # e-acute as one codepoint
    decomposed = "Cafe\u0301"   # "e" + combining acute
    assert composed != decomposed
    assert normalize(composed) == normalize(decomposed)


def test_normalize_folds_fullwidth_to_halfwidth():
    # NFKC, not merely NFC: a Japanese library routinely holds both "ＡＢＣ Title" and
    # "ABC Title" for what is one track, and the question being asked is whether two names
    # denote the same title. NFC leaves the fullwidth form fullwidth, so it cannot answer.
    assert normalize("\uff21\uff22\uff23 Title") == "abc title"
    assert normalize("\uff21\uff22\uff23 Title") == normalize("ABC Title")


def test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title():
    # NFKC folds the fullwidth solidus "／" to "/". If "/" were structural, "01. A／B.m4a"
    # would collapse to "b" and collide with "01. B.m4a" and "01. C／B.m4a" -- three
    # distinct tracks, one key. "／" is ordinary Japanese typography for a two-part title.
    # Latent in the current library (0 occurrences) but the same false-skip class §7.3
    # exists to prevent, so it is pinned rather than left to chance.
    #
    # The key is "a/b", not "a／b": NFKC runs first (round 2) and folds U+FF0F to "/".
    # That is correct -- the point is that the solidus *survives into the key* instead of
    # being read as a separator that truncates the name to "b".
    assert normalize("01. A／B.m4a") == "a/b"
    assert normalize("01. A／B.m4a") != normalize("01. B.m4a")
    assert normalize("01. C／B.m4a") != normalize("01. B.m4a")
    # "/" is not structural here either, whatever its provenance.
    assert stem_of("AC/DC - Back in Black") == "AC/DC - Back in Black"


def test_normalize_does_not_swallow_a_title_leading_dot_run():
    # NFKC folds "…" to "...", so a greedy `[\s._-]+` separator run eats the title's own
    # leading dots. This is live: "13. …to mo da ti _.m4a" really exists in the library
    # and keyed to "to mo da ti _" before the separator was tightened. One separator
    # character, optionally padded, cannot eat two adjacent dots.
    assert normalize("13. …to mo da ti _.m4a") == "...to mo da ti _"
    assert normalize("04. ...And Then") != normalize("And Then")
    assert normalize("13. …to mo da ti _.m4a") != normalize("13. to mo da ti _")


def test_audio_exts_membership_table():
    # The table in the AUDIO_EXTS comment, executable. Task 3's scan walks untrusted
    # names with this predicate, so every row is a decision rather than an example.
    for name in (
        "Song.m4a",                    # known extension
        "Song.M4A",                    # matched case-insensitively
        "album/01. Track.flac",        # a path to an audio file
        "01. A／B.m4a",         # "/" is not a separator, even after NFKC
    ):
        assert is_audio_file(name), name
    for name in (
        "01. Artist - Title",          # no extension; the dot is part of the name
        "1. Title",                    # same
        ".m4a",                        # a leading dot is not an extension
        ".hidden",
        "cover.jpg",                   # known non-audio extension
        "download.part",
        "no-extension",
    ):
        assert not is_audio_file(name), name
    # The leading-dot row is a decision, not an accident: `stem_of` agrees with it, so the
    # two never disagree about whether a name has an extension.
    assert stem_of(".m4a") == ".m4a"
    assert not is_audio_file(".m4a")


def test_normalize_keeps_a_separator_with_padding_and_stops_at_two_groups():
    # The cases the separator rewrite had to keep handling, per §7.5 step 4.
    assert normalize("1-01 Title") == "title"
    assert normalize("01 Title") == "title"
    assert normalize("1. Title") == "title"
    assert normalize("01. Artist - Title") == "artist - title"
    assert normalize("1 - 01 - Title") == "title"      # separator with padding
    assert normalize("1-01-02 Title") == "02 title"   # max 2 groups, third stays
    assert normalize("1979 - Song") == "1979 - song"  # 4-digit year survives


def test_normalize_folds_fullwidth_structure_before_stripping():
    # Order matters: NFKC has to run before the two strip steps, or structure written in
    # fullwidth is invisible to them. "Song．Ｍ４Ａ" otherwise keeps ".m4a" inside the key,
    # and the fullwidth index in "０１．Artist - Title" is never recognised as a track
    # number. Both are false misses -- the track fails to match its own file.
    assert normalize("Song．Ｍ４Ａ") == "song"
    assert normalize("０１．Artist - Title") == normalize("01. Artist - Title")
    # A fullwidth album name is unaffected by the reorder: "4pi"/"1st EP" are ASCII, and
    # NFKC leaves ASCII untouched.
    assert normalize("4pi", strip_track_prefix=False) == "4pi"


def test_is_audio_file_is_case_insensitive():
    # Task 3's scan walks untrusted names with this predicate. A bare `ext in AUDIO_EXTS`
    # is False for ".M4A"; every extension in the real library is lowercase, so this is
    # latent rather than live, but the helper is what keeps it from becoming live.
    # The full case table lives in test_audio_exts_membership_table.
    assert is_audio_file("Song.m4a")
    assert is_audio_file("Song.M4A")
    assert is_audio_file("album/01. Artist - Title.FLAC")
    assert not is_audio_file("cover.jpg")
    assert not is_audio_file("download.part")
    assert not is_audio_file("no-extension")
    assert not is_audio_file("01. Artist - Title")


def test_normalize_squeezes_whitespace():
    assert normalize("a   b") == normalize("a b")
    assert normalize("  Title  ") == "title"


def test_normalize_returns_empty_for_unusable_titles():
    # Review Focus #2: these must be detectable, not silently match everything.
    # An empty return means "unusable title" -- it is a value that equals every other
    # empty title, so the *caller* must refuse to skip on it. See `normalize`'s docstring.
    assert normalize("") == ""
    assert normalize("...") == ""
    assert normalize("1-01 ") == ""
    assert normalize("1-01 ...") == ""


def test_audio_exts_cover_the_real_library():
    # measured on this operator's external library
    assert {".m4a", ".flac", ".mp4"} <= AUDIO_EXTS
    assert ".jpg" not in AUDIO_EXTS and ".lrc" not in AUDIO_EXTS and ".part" not in AUDIO_EXTS


def test_format_buckets_are_casefolded():
    # §8 decides artist/album by matching a grandparent directory against these, against
    # `normalize()`d path components, so they are stored casefolded to be comparable.
    # Note this is not the same test as AUDIO_EXTS: these name directories, not files.
    assert FORMAT_BUCKETS == frozenset({"alac", "atmos"})
