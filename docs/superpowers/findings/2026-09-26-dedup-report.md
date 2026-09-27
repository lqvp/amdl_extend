# Task 4 report — Dedup: album-scoped title matching

- Commit: `9ddc3add3e6d70a455f81d4da756babb7027cea4` on `feat/phase-1-foundation`
- Branch: `feat/phase-1-foundation`, parent `5a8db48`
- Files: `hub/hub/dedup.py` (new), `hub/tests/test_dedup.py` (new), `hub/tests/conftest.py`
  (one fixture entry + its docstring note), `hub/spike/task4_real_library_check.py` (new,
  the read-only verification script)
- Status: **DONE_WITH_CONCERNS** — everything asked for is implemented and verified, but
  four brief assertions had to be corrected and one pre-existing test failure was traced
  to a stale `.pyc`. Both are detailed below.

---

## 1. What was implemented

### `hub/hub/dedup.py`

```python
ARTIST_SCOPES: frozenset[str]  # {"loose", "strict"}

@dataclass(frozen=True, slots=True)
class DuplicateHit:
    matched: tuple[str, ...]   # AlbumDir.relpath, sorted

def find_duplicate(
    scan: LibraryScan, *,
    album_name: str,
    track_title: str,
    artist_name: str | None,
    artist_scope: str = "loose",
) -> DuplicateHit | None
```

Five steps, exactly the brief's algorithm, with the album-side empty-key guard added:

1. `artist_scope` validated against `ARTIST_SCOPES`.
2. `title_key = normalize(track_title)`; `scope_key = album_key(album_name)`. If **either**
   is `""`, return `None`.
3. `candidates = scan.by_name.get(scope_key, ())`.
4. `strict` filters candidates to those whose `artist` is truthy and equals
   `normalize(artist_name)`.
5. `matched = tuple(sorted(c.relpath for c in candidates if title_key in c.track_keys))`;
   return `DuplicateHit(matched)` or `None`.

**Purity.** No filesystem access, no config, no clock, no network. `artist_scope` is a
keyword argument and is never read from settings inside. Pinned by
`test_find_duplicate_never_touches_the_filesystem`, which builds a `LibraryScan` by hand
over `/nonexistent/lib` — nothing in that test can have been satisfied by reading a disk.

**Album-side keying uses `library_scan.album_key()`, not a literal
`normalize(..., strip_track_prefix=False)`.** The brief wrote the literal; the function
already exists for exactly this reason (build/lookup drift is the most damaging silent
failure in the dedup path — every album lookup misses and nothing is ever skipped, with no
error anywhere). Calling the one function that Task 3 already designated makes the drift
structurally impossible rather than merely documented.

**Album names keep their discriminators.** `4pi`, `1st EP`, `4 - Leaves`, `Song - Single`,
`Album [Deluxe]` all key to themselves; the strip is off for the album side and on for the
title side, on both the index build and the lookup.

## 2. Decisions the brief left open

| # | Decision | Why |
|---|---|---|
| 1 | **`artist_scope` gets a default of `"loose"`** | The parent fixed this (decision 2). The brief's signature showed no default; a default makes the "omitted means loose" path testable and cannot break the Task 9 call site. `test_artist_scope_defaults_to_loose` pins it. |
| 2 | **An unrecognised `artist_scope` raises `ValueError`** | Closed set, not a truthy switch. Reading `"Loose"`/`"loose "` as `loose` skips tracks the user asked for when the typo was meant to be `strict`, and stops skipping tracks already on disk in the other direction. Both silent, neither recoverable. `Settings.dedup_artist_scope` is a `Literal["loose","strict"]` (config.py:44), so reaching this is a wiring bug, and raising points at the only place it can be fixed. Case is *not* accepted silently — a normalising layer belongs in config, not here. **Precedent: `config._scope` (config.py:96) already does exactly this, with the same reasoning in its comment** ("silently degrading to `loose` would re-enable the false-skip mode §7.4 is written to warn about"), raising `RuntimeError` there vs `ValueError` here. The principle is already established in this codebase; I matched it and used the more precise type for a bad argument. |
| 3 | **`DuplicateHit` has exactly one field** | The brief fixes the interface and Task 9 builds `skip_reason` from `matched`. No `skip_reason` formatter is added: that is Task 9's job and inventing it here would be an unused API. |
| 4 | **`matched` is sorted, and a hit that matched nothing is `None` not `DuplicateHit(())`** | `os.walk` order is filesystem-dependent and NTFS does not sort; the strings reach the user through `skip_reason`. An empty hit would be indistinguishable from a hit at call sites. |
| 5 | **`strict` drops candidates with `artist is None` and with an empty `artist_name`** | "`strict` means the artist vouches." 10 album directories in the real library have no determinable artist (the album dir is the root, or sits directly in it), and a resolver that could not read the artist hands over `None`. Treating either as a wildcard would make `strict` `loose` with extra steps. Pinned by `test_strict_scope_refuses_when_the_artist_is_unknown`. |
| 6 | **Both sides go through `normalize()` with the default flag for the artist** | The brief's step 3 says `normalize(artist_name)`. The candidate side is a directory basename and the query side is a tag value, so symmetry is what matters, and both use the same call. Stripping the track prefix on the *artist* side is also the better behaviour: an artist directory written `01 Artist` still answers to the tag `Artist`. |
| 7 | **`artist_scope` is validated *before* the empty-key checks** | An invalid argument is a programming error regardless of whether the keys are empty, and a test that passes only because it short-circuits on `""` would not notice a later refactor dropping the validation. |
| 8 | **The real-library verification lives in `hub/spike/`, not in the test suite** | It reads 341 GB of live user data. `spike/child_process_probe.py` (Task 5) set the precedent, and `pyproject.toml` already excludes `spike/` from the wheel. The numbers are in this report; the script is committed so they are reproducible. |

## 3. Four brief assertions that contradicted their own fixtures

Each was run as written first, failed, and the failure was diagnosed against the fixture
before anything was changed. In every case the brief's **comment/intent was right** and only
the `assert` line was inverted. The correction is recorded in the `test_dedup.py` module
docstring so it is durable, not just in this report.

### 3.1 `test_does_not_skip_same_title_in_a_different_album` → `test_a_hit_is_confined_to_the_album_that_was_asked_for`

The brief asserted `is None` for `album_name` in `("Album One", "Album Two", "Album Three")`
with `track_title="intro"`. Each of those three directories **contains `intro.m4a`**. No
correct implementation can return `None`; the assertion pins the opposite of §7.6's first
row ("別アルバムに同名トラックがある → **スキップしない**"), i.e. it would have required dedup to
*skip nothing ever*, which is also not the requirement.

Corrected to what "does not skip the same title in a *different* album" actually means: a
hit names *that* album and never a sibling sharing the title.

```python
for album in ("Album One", "Album Two", "Album Three"):
    hit = find_duplicate(scan, album_name=album, track_title="intro", ...)
    assert hit is not None
    assert set(hit.matched) == {f"a/{album}"}
for album in ("Album Four", "Album Six", "Nyarons"):   # hold no "intro"
    assert find_duplicate(scan, album_name=album, track_title="intro", ...) is None
```

Strengthened with `test_the_global_answer_is_wider_than_the_scoped_one`, which asserts the
contrast the scoping exists to draw: a global title search returns
`["a/Album One", "a/Album Three", "a/Album Two"]` — 3× the scoped answer, and 6× in the real
library.

### 3.2 `test_format_bucket_prefix_does_not_matter`

The brief asserted `"a/TEMPLIME/POP-AID" in hit.matched` for `track_title="x"`. The fixture
holds `a/ALAC/TEMPLIME/POP-AID/x.m4a` and `a/TEMPLIME/POP-AID/y.m4a` — `x` is in the
**bucketed** copy, so the correct answer is `("a/ALAC/TEMPLIME/POP-AID",)`. The brief's
assertion can only pass if the bucket prefix is ignored when matching **titles**, which is
the opposite of the test's name.

Corrected to query each copy by its own track, which is what actually demonstrates "the
bucket does not affect which directories are in scope":

```python
find_duplicate(scan, album_name="POP-AID", track_title="x", ...) -> {"a/ALAC/TEMPLIME/POP-AID"}
find_duplicate(scan, album_name="POP-AID", track_title="y", ...) -> {"a/TEMPLIME/POP-AID"}
{a.relpath for a in scan.by_name["pop-aid"]} == {both}   # both copies are in scope
```

### 3.3 `test_album_name_keeps_single_suffix_and_deluxe`

The brief asserted the *exact* name `"Album [Deluxe]"` returns `None`, from a tree that
contains `extra/Album [Deluxe]/t.m4a`. That assertion passes **only** when `[Deluxe]` is
stripped — i.e. it would have certified the very bug the test exists to catch, and that bug
would fuse every `<track> - Single` directory in the real library into one group.

Corrected to the discrimination the comment describes: the exact name is found, and the name
with its discriminator removed is a *different* album.

```python
for name in ("4pi", "1st EP", "4 - Leaves", "Song - Single", "Album [Deluxe]"):
    assert find_duplicate(scan, album_name=name, track_title="t", ...) is not None
for stripped in ("Album", "Song", "Leaves", "4"):
    assert find_duplicate(scan, album_name=stripped, track_title="t", ...) is None
```

### 3.4 `test_refuses_to_skip_on_an_empty_album_name` — a fixture that was never landed

The brief asserted `"" in scan_roots(make_library_extra).by_name`, but `make_library_extra`
held no punctuation-only album. Root cause: the uncommitted plan edit in this working tree
adds a conftest line `lib/extra/・・・/t.m4a  # punctuation-only album name -> "" key` which
was **never applied to `conftest.py`**.

Resolved in the direction of the plan rather than by moving the assertion: the fixture entry
was added, so the brief's assertion is satisfied verbatim and the test now covers both real
spellings of the shape at once — `a/!_` (how the library has it) and `extra/・・・` (how a
download would carry it). They land in one `""` group, which is the hazard: that group holds
real tracks keyed `t`, so a lookup for either spelling would skip a download that has not
happened. The guard therefore has to be on the **lookup**, not on the index.

## 4. A pre-existing test failure that was not mine

The first full run showed `1 failed, 85 passed`:
`test_library_scan.py::test_album_dirs_are_reported_in_a_stable_per_directory_order`.

**Cause: a stale `__pycache__`, not a code defect.** `hub/__pycache__/library_scan.cpython-313.pyc`
had mtime 00:41, the same second as the `library_scan.py` written by Task 3's final commit
(`5a8db48`). CPython validates a `.pyc` by `(source mtime, source size)`; a same-second edit
that preserves the file size passes validation, so the **old bytecode was loaded and
`dirnames.sort()` was silently absent**. Album directories came out in readdir order.

Confirmed by `inspect.getsource(_walk_root)` showing the sort present in the `.py` while
`scan_roots` behaved as if it were not, and by an inline hand-rolled walk over the identical
tree producing the correct sorted order in the same interpreter.

Fix: `rm -rf hub/__pycache__ tests/__pycache__` (a gitignored build artifact). No source
change; nothing to commit. All 86 tests pass on clean bytecode. **Worth knowing:** anyone
editing a `hub/` source file within the same second as a `pytest` run can silently test
stale code. Consider `PYTHONDONTWRITEBYTECODE=1` for a one-off confirmation run, or just
clearing the cache when a result contradicts the source.

## 5. Test command and verbatim output

```
cd /home/m/apple-dl_extend/hub && uv run pytest -v
```

```
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/apple-dl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/apple-dl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 86 items

tests/test_config.py::test_requires_password PASSED                      [  1%]
tests/test_config.py::test_requires_password_when_the_variable_is_absent PASSED [  2%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [  3%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [  4%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [  5%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [  6%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [  8%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [  9%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 10%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 11%]
tests/test_config.py::test_rejects_a_port_outside_the_bindable_range PASSED [ 12%]
tests/test_dedup.py::test_skips_when_same_album_exists_in_two_places PASSED [ 13%]
tests/test_dedup.py::test_loose_scope_catches_collab_fanout PASSED       [ 15%]
tests/test_dedup.py::test_strict_scope_requires_artist PASSED            [ 16%]
tests/test_dedup.py::test_strict_scope_answers_only_one_of_the_collab_placements PASSED [ 17%]
tests/test_dedup.py::test_strict_scope_refuses_when_the_artist_is_unknown PASSED [ 18%]
tests/test_dedup.py::test_a_hit_is_confined_to_the_album_that_was_asked_for PASSED [ 19%]
tests/test_dedup.py::test_the_global_answer_is_wider_than_the_scoped_one PASSED [ 20%]
tests/test_dedup.py::test_format_bucket_prefix_does_not_matter PASSED    [ 22%]
tests/test_dedup.py::test_album_name_keeps_single_suffix_and_deluxe PASSED [ 23%]
tests/test_dedup.py::test_refuses_to_skip_on_an_empty_album_name PASSED  [ 24%]
tests/test_dedup.py::test_refuses_to_skip_on_an_empty_title PASSED       [ 25%]
tests/test_dedup.py::test_unknown_album_returns_none PASSED              [ 26%]
tests/test_dedup.py::test_hit_paths_are_sorted_for_stable_display PASSED [ 27%]
tests/test_dedup.py::test_artist_scope_defaults_to_loose PASSED          [ 29%]
tests/test_dedup.py::test_an_unknown_artist_scope_is_rejected PASSED     [ 30%]
tests/test_dedup.py::test_find_duplicate_never_touches_the_filesystem PASSED [ 31%]
tests/test_dedup.py::test_duplicate_hit_is_immutable PASSED              [ 32%]
tests/test_dedup.py::test_scan_is_fast_enough_to_run_per_request PASSED  [ 33%]
tests/test_library_scan.py::test_finds_album_dirs_at_the_shallowest_level PASSED [ 34%]
tests/test_library_scan.py::test_an_album_dirs_keys_do_not_include_a_subdirectorys_tracks PASSED [ 36%]
tests/test_library_scan.py::test_the_walk_descends_past_an_album_dir_into_its_subdirectories PASSED [ 37%]
tests/test_library_scan.py::test_groups_same_named_album_dirs_across_roots PASSED [ 38%]
tests/test_library_scan.py::test_album_name_index_keeps_leading_digits PASSED [ 39%]
tests/test_library_scan.py::test_album_name_index_keeps_a_number_a_separator_follows PASSED [ 40%]
tests/test_library_scan.py::test_a_track_number_is_still_stripped_from_track_keys PASSED [ 41%]
tests/test_library_scan.py::test_album_name_index_keeps_single_and_deluxe_suffixes PASSED [ 43%]
tests/test_library_scan.py::test_an_album_name_that_normalizes_to_nothing_is_still_indexed PASSED [ 44%]
tests/test_library_scan.py::test_derives_artist_from_structure PASSED    [ 45%]
tests/test_library_scan.py::test_artist_of_a_depth_two_album_dir PASSED  [ 46%]
tests/test_library_scan.py::test_artist_of_a_codec_directory_that_is_not_below_a_codec_directory PASSED [ 47%]
tests/test_library_scan.py::test_artist_is_the_parent_directory_name PASSED [ 48%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts0-None] PASSED [ 50%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts1-None] PASSED [ 51%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts2-TEMPLIME] PASSED [ 52%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts3-TEMPLIME] PASSED [ 53%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts4-EmoCosine] PASSED [ 54%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts5-Atmos] PASSED [ 55%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts6-Atmos] PASSED [ 56%]
tests/test_library_scan.py::test_an_album_with_no_artist_level_reports_the_codec_directory PASSED [ 58%]
tests/test_library_scan.py::test_artist_of_a_dir_whose_parent_is_the_root_is_unknown PASSED [ 59%]
tests/test_library_scan.py::test_a_root_that_is_itself_an_album_dir_is_still_a_scope PASSED [ 60%]
tests/test_library_scan.py::test_the_root_scope_is_not_indexed_as_an_album_name PASSED [ 61%]
tests/test_library_scan.py::test_by_name_is_the_same_however_the_root_is_spelled PASSED [ 62%]
tests/test_library_scan.py::test_is_audio_file_decides_what_is_a_track PASSED [ 63%]
tests/test_library_scan.py::test_part_files_are_not_tracks PASSED        [ 65%]
tests/test_library_scan.py::test_a_directory_of_only_part_files_is_not_an_album_dir PASSED [ 66%]
tests/test_library_scan.py::test_empty_titles_are_representable_but_distinguishable PASSED [ 67%]
tests/test_library_scan.py::test_unreachable_root_is_marked_degraded PASSED [ 68%]
tests/test_library_scan.py::test_every_root_appears_in_order_with_its_own_index PASSED [ 69%]
tests/test_library_scan.py::test_root_that_is_a_symlink_is_accepted PASSED [ 70%]
tests/test_library_scan.py::test_a_walked_path_outside_the_root_raises_instead_of_finding_nothing PASSED [ 72%]
tests/test_library_scan.py::test_a_symlinked_directory_inside_a_root_is_not_followed PASSED [ 73%]
tests/test_library_scan.py::test_repeated_scans_of_one_tree_agree PASSED  [ 74%]
tests/test_library_scan.py::test_a_later_change_is_visible_without_a_rescan_hook PASSED [ 75%]
tests/test_library_scan.py::test_no_roots_yields_an_empty_but_valid_scan PASSED [ 76%]
tests/test_library_scan.py::test_a_root_that_is_a_file_is_degraded_not_fatal PASSED [ 77%]
tests/test_library_scan.py::test_scan_is_fast_enough_to_run_per_request PASSED [ 79%]
tests/test_library_scan.py::test_album_dirs_are_reported_in_a_stable_per_directory_order PASSED [ 80%]
tests/test_normalize.py::test_stem_of_drops_only_a_known_audio_extension PASSED [ 81%]
tests/test_normalize.py::test_stem_of_keeps_a_name_whose_dot_is_not_an_extension PASSED [ 82%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 83%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 84%]
tests/test_normalize.py::test_normalize_keeps_a_four_digit_year PASSED   [ 86%]
tests/test_normalize.py::test_normalize_folds_decomposed_and_composed_equally PASSED [ 87%]
tests/test_normalize.py::test_normalize_folds_fullwidth_to_halfwidth PASSED [ 88%]
tests/test_normalize.py::test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title PASSED [ 89%]
tests/test_normalize.py::test_normalize_does_not_swallow_a_title_leading_dot_run PASSED [ 90%]
tests/test_normalize.py::test_audio_exts_membership_table PASSED         [ 91%]
tests/test_normalize.py::test_normalize_keeps_a_separator_with_padding_and_stops_at_two_groups PASSED [ 93%]
tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping PASSED [ 94%]
tests/test_normalize.py::test_is_audio_file_is_case_insensitive PASSED   [ 95%]
tests/test_normalize.py::test_normalize_squeezes_whitespace PASSED       [ 96%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 97%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 98%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 86 passed in 0.16s ==============================
```

18 of the 86 are Task 4's; the other 68 (config, normalize, library_scan) are unchanged and
still pass.

Lint — the project is ruff-check clean but **not** ruff-format clean (4 pre-existing files
would be reformatted), so only the check gate was held:

```
$ uvx ruff@0.16.9 check hub/ tests/ spike/task4_real_library_check.py
All checks passed!
```

## 6. Real-library verification

```
cd /home/m/apple-dl_extend/hub && uv run python spike/task4_real_library_check.py
```

Roots: `/home/m/apple-dl_extend/AppleMusicDecrypt/downloads` and
`/run/media/m/1A5E05A75E057D2F/Music` (the user's symlink). Both reachable, no degradation.
**4,739 album dirs, 3,431 distinct album names, `scan_roots` in 0.123 s.**

### 6.1 How many of the 223 duplicate album-directory names are found

| | external drive only | both roots |
|---|---|---|
| duplicated album names (≥2 dirs share a name) | **223** | 1,220 |
| redundant directories | **238** | 1,307 |
| **found by an album-name lookup** | **222 / 223** | 1,214 / 1,220 |
| not askable (group holds only unusable track keys) | 1 | 5 |

The external-only figures reproduce §7.2.1's **223 names / 238 redundant dirs** exactly, so
the index and the lookup agree with the measurement the design rests on. The 1 miss is not a
miss: that group contains only `""` track keys, and refusing an empty key is the point. The
1 remaining "not found" in the both-roots column is an artifact of the *script* — it passes an
already-normalised index key as `track_title`, and 6 of 8,721 keys are not idempotent under a
second `normalize()` (see 6.4). The product passes a real title or a rendered filename.

Of the 1,220 both-roots duplicate groups: **134** have every copy under a distinct artist
(collab fan-out, 種別 B) and **1,086** have ≥2 copies under one artist (種別 A).
Per-copy detection: **2,511 / 4,739** album dirs are reachable as a duplicate under `loose`,
**2,510 / 4,739** under `strict` with the correct artist — i.e. `strict` loses almost nothing
overall and all of the loss is the collab fan-out.

### 6.2 `intro` still resolves to six separate scopes

```
'intro'    in 6 album dirs, 6 distinct album names
    ALAC/Kirara Magic/Magic Shop
    ALAC/Moe Shop/Moshi Moshi
    ALAC/Moe Shop/Pure Pure - EP
    ALAC/Snail's House/Sweety Sweety - EP
    ALAC/tofubeats/lost decade
    ALAC/サカナクション/sakanaction
    -> 6 per-album lookups, none crossed an album-name boundary
```

A lookup for one of those albums **never** returns a hit from any of the other five. The
script asserts this for every album holding the title, on all five affected titles:

| title | album dirs | distinct album names | leak |
|---|---|---|---|
| `intro` | 6 | **6** | none |
| `escapism` | 6 | 3 | none |
| `mu` | 6 | 3 | none |
| `yoake` | 6 | 3 | none |
| `""` | 15 | 7 | none |

`escapism`/`mu`/`yoake` are a better demonstration than `intro`: their 6 directories are only
**3** album names, because `Escapism - EP` and `POP-AID` each genuinely exist twice (under
`ALAC/TEMPLIME/`+`TEMPLIME/` and `ALAC/星宮とと/`). A lookup for `Escapism - EP` correctly
returns both of *its* copies and never touches `POP-AID` — a duplicate is supposed to return
more than one path. The invariant asserted is therefore **not** "returns exactly one
directory" but **"never crosses an album-name boundary"**, which is the property that makes
`skip_reason` adjudicable.

Also checked: an unrelated album (`Let me battle (feat. つぐ, わかばやし & みょみょ) - Single`)
asked for `intro` → no match.

### 6.3 The empty album key `""` is present, and a punctuation-only name is refused

```
'' in by_name : True
members (1):
    ALAC/薄塩指数/!_   (artist='薄塩指数', 11 track keys)
refused for '・・・' / '!' / '_' / '...' / '' with title '!_ (intro)' -> None
album dirs holding an unusable track key: 15
refused for '' / '...' / '・' / '01 ..m4a' -> None
```

`"" in by_name` is `True` and holds exactly one directory, `ALAC/薄塩指数/!_`, with **11 real
track keys**. A punctuation-only album name is refused for all of them — verified against the
actual titles (`!_ (intro)`, `おろかもね (feat. 重音テト)`, `かぶしきがいしゃにんげん! (feat.
重音テト)`, `すかすかですが__ (feat. 重音テト)`, `だめにんげんだ! (feat. 重音テト)`), not just
one. The symmetric title-side guard refuses `""`/`"..."`/`"・"`/`"01 ..m4a"` against all 15
album directories that hold an unusable track key.

Critically, the guard is a refusal and not a way of making an album invisible:
`【 呪文 】 - EP` (whose name *contains* no alnum but the directory name as a whole does) is
found, and asking for one of its real titles returns both placements —
`('new-dl/わらべ/【 呪文 】 - EP', 'わらべ/【 呪文 】 - EP')` — a correct 種別 A skip.

### 6.4 The measured 種別 B case, and the `loose` default

`らぶふぉーゆー - Single` exists three times, one per credited artist, with one track each:

```
ALAC/EmoCosine/らぶふぉーゆー - Single     artist='EmoCosine'
ALAC/ころねぽち/らぶふぉーゆー - Single      artist='ころねぽち'
ALAC/メガミノウタゲ/らぶふぉーゆー - Single   artist='メガミノウタゲ'

title='らぶふぉーゆー' artist='EmoCosine'  loose  -> all 3 dirs
title='らぶふぉーゆー' artist='EmoCosine'  strict -> 1 of 3
title='らぶふぉーゆー' artist='ころねぽち'   strict -> 1 of 3
title='らぶふぉーゆー' artist='メガミノウタゲ' strict -> 1 of 3
```

This is the parent's decision 2 measured and confirmed: `loose` finds all three placements,
`strict` finds one and would re-download the other two. The `loose` default is right, and
`test_strict_scope_answers_only_one_of_the_collab_placements` pins the 2/3 cost as a number
rather than in a comment.

### 6.5 Supporting measurements

- Titles shared by >1 album dir: **1,207 / 8,721 (13.8%)** on the external drive, matching
  §7.2.1's 1,206 / 8,721. Across both roots, 2,810 / 8,721 (32.2%).
- Worst offenders: `""` ×15, `hiko` ×9, `木漏れ日の待ち人` ×8, then several at ×6
  (`小さな星座`, `春死なん (feat. 可不)`, `死が僕らに恋してる`, `醜形恐怖症`, `永眠のすゝめ`).
  **A global title match would skip on 13.8% of the external library.** That is the number
  the album scoping avoids.
- `scan_roots` over both roots: **0.123 s**, so a per-request scan is affordable (§7.1.1
  measured 0.06 s of `os.walk`; the full scan including key building is 0.123 s here).
- 6 of 8,721 index keys are not idempotent under a second `normalize()` — all titles that
  genuinely begin with a digit pattern: `00 am` → `am`, `1-ch channel id` → `ch channel id`,
  `1 a.m. (feat. shinoだす。)` → `a.m. (feat. shinoだす。)`, etc. See the concern below.

## 7. Concerns for Task 9

1. **Pass a rendered filename, not a tag title.** `find_duplicate`'s docstring says the
   comparison basis is the filename (§7.5). Measured: 6 of 8,721 keys are not idempotent, so
   a caller that passes a tag title where a rendered filename belongs mis-keys those 6 and
   re-downloads. Misses in the safe direction and quantified at 0.07%, but worth wiring
   deliberately.

2. **One `normalize()` quirk, in Task 2's scope, not fixed here.** The track-prefix pattern
   eats `1-` from the real library's `1-ch channel id` and `1-ch music_ hom.02`, keying them
   `ch channel id` and `ch music_ hom.02`. Because the same transform is applied to both
   sides this cannot cause a false skip — only a different key than a human would expect — so
   I did not touch `normalize` (reviewed and closed, and consumed as-is by contract). Noted
   for the record.

3. **`loose`'s residual false-positive risk is real but bounded and visible.** Two genuinely
   different albums sharing a name *and* a track title would both be reported. Per §7.4 the
   mitigation is not perfect detection, it is that `skip_reason` always carries the matched
   real paths — so `DuplicateHit.matched` must reach `skip_reason` intact in Task 9
   (`f"duplicate:{'|'.join(hit.matched)}"` per the plan). Losing that field would remove the
   only thing that makes a `loose` skip adjudicable.

4. **The stale-`.pyc` hazard is environmental, not fixed in code.** See §4. If a later task
   reports a `hub/` test failing in a way the source contradicts, clear
   `hub/**/__pycache__` before debugging, or run with `PYTHONDONTWRITEBYTECODE=1`.

5. ~~**The uncommitted plan edit is still uncommitted.**~~ **RESOLVED between rounds.**
   `docs/superpowers/plans/2026-09-26-amd-hub-phase1.md` was committed twice after round 1:
   `47ea860 fix(plan,spec): un-invert three Task 4 assertions, record normalize's idempotence
   gap` and `49d90bb docs: pin Task 9 to pass a rendered filename, not a tag title`. The
   three inverted assertions are now correct in the plan (`Album [Deluxe]` is `is not None`,
   plus an explicit deluxe≠single assertion) and the "218 件" figure is gone. **The one
   residue from this round is m2:** the plan's Step 5 still prescribes
   `test_scan_is_fast_enough_to_run_per_request`, the duplicate this round replaced. It
   should be re-worded to prescribe the `find_duplicate` cost guard, since the plan is what
   a re-implementer reads.

---

## Fix round 1

Review outcome: 25 mutations, **21 caught, 4 survived**, plus one Critical test gap. The
module was confirmed correct — scope, guards, `artist_scope` semantics and purity are
unchanged, and `find_duplicate` itself is not modified in this round. This round is tests
and prose.

- Commit: `712246e3e1d413625fcdc4939304e7ae9a3976da` on `feat/phase-1-foundation`, parent
  `9ddc3ad`
- Files: `hub/hub/dedup.py` (docstrings only), `hub/tests/test_dedup.py`,
  `hub/tests/conftest.py` (one fixture entry + its docstring note)
- All runs below use `PYTHONDONTWRITEBYTECODE=1`, per the round-1 environment note.

### C1 (Critical) — `sorted()` on `matched` was unpinned

**Root cause, unchanged from the review:** no real walk of a `dirnames.sort()`ed tree
produces out-of-order candidates. On the §12.1 fixture `a/...` already precedes `b/...`,
so the natural candidate order *is* sorted and every fixture-based assertion is satisfied
identically with and without the call. `test_skips_when_same_album_exists_in_two_places`
compares with `set(...)` and cannot see order; `test_find_duplicate_never_touches_the_filesystem`
hand-builds a scan with a single album, so `sorted()` is a no-op on it.

This is the same failure mode as the `strip_track_prefix` fixture, and worse here:
`DuplicateHit.matched` is the evidence a human adjudicates a `loose` skip from, and its
docstring justifies the sort by exactly the condition the fixture cannot produce.

**Fix:** `test_matched_is_sorted_even_when_the_scan_order_is_not` — a hand-built scan over
`/nonexistent/lib` whose three candidates are in scrambled order.

- **Scrambled, not reversed.** The review's `z`/`a` pair would also pass under
  `reversed(candidates)`, so it pins "not a reversal" rather than "is a sort". Three
  candidates in the order `b, a, c` pin both: the expected value
  `("a/Album", "b/Album", "c/Album")` is neither the input order nor its reversal.
- **Both scopes asserted.** The sort happens *after* the candidate filter, so a mutant that
  moved it inside the `loose` branch would survive a `loose`-only test. The test asserts the
  same ordering under `strict`.
- `test_hit_paths_are_sorted_for_stable_display` now carries a comment saying it **cannot**
  catch this and pointing at the test that does, so the next reader does not rely on it.

### I1 (Important) — four surviving `_by_artist` mutants

Every surviving mutant shared one cause: **every fixture artist matches its tag
byte-for-byte**, so a candidate side that skipped `normalize()` still agreed with every
assertion. Two docstring claims were untested for the same reason.

**Fix, with two independent kills for the candidate-side `normalize()`:**

1. **Case folding, no fixture cost.** The library has 8 artist directories that are not
   already lowercase. `strict` with `artist_name="emocosine"` must match the directory
   `EmoCosine`; a raw comparison answers False.
2. **The real directory that is not its own key.** The library has exactly **1 of 349**
   artist directories whose `normalize()` differs from its raw name: `429 & nyankobrq`,
   whose key is `& nyankobrq`. Added to the fixture as `a/429 & nyankobrq/Named Album/t.m4a`
   and asserted both ways — a tag of `429 & nyankobrq` **and** a tag of `& nyankobrq` must
   find it, and a tag of `nyankobrq` must not. This is the review's suggested `01 Artist`
   case, using the one instance the user's data actually contains rather than a synthetic
   name.

**Empty-`artist_key` refusal:** covered on a hand-built scan with `artist="!"`, not on a
fixture. The real library has **no** artist directory whose name normalizes to `""`
(measured: 0 of 349), so a fixture entry would invent a shape — and `conftest.py` carries an
explicit contract that fixtures do not do that. The test says so.

**Unknown-artist drop (`c.artist and`):** already killed, and the test now explains *why* it
is load-bearing — it is what keeps `normalize(None)` from raising on the 10 album directories
whose artist is undeterminable. `test_strict_scope_refuses_when_the_artist_is_unknown` also
gained `""` and `"   "`, since `artist_name` is `str | None` and a blank field is a real
resolver outcome.

**Mutation results** (each applied to `hub/hub/dedup.py`, run, then restored from a copy):

| # | Mutation | Result |
|---|---|---|
| M1 | delete `sorted(` | **killed** — `test_matched_is_sorted_even_when_the_scan_order_is_not` |
| M2 | delete candidate-side `normalize()` | **killed** — 5 tests (was: survived) |
| M3 | delete the empty-`artist_key` refusal | **killed** — `..._refuses_an_artist_name_with_no_identifying_information` |
| M4 | fold unknown artist to `''`: `normalize(c.artist or "")` | **survives — equivalent by construction** (below) |
| M5 | drop the `c.artist` truthiness guard | **killed** — `TypeError` in `normalize.py:162` |
| M6 | body returns `()` | **killed** — 7 tests |
| M7 | **M3+M4 compound** ("strict silently degrades") | **killed** — the empty-artist test |

**M4 is genuinely equivalent, not an untested claim.** `normalize(c.artist or "")` differs
from `c.artist and normalize(c.artist)` in exactly one case: `artist is None` *and*
`artist_key == ""`. The `if not artist_key: return ()` guard makes `artist_key == ""`
unreachable at the comparison, so the two forms agree on every input. It is not pinnable
without removing a guard, and M7 — the compound, which is the state that actually matters —
is killed. I am recording this as equivalent rather than pretending to cover it.

### m1 — the 種別 attribution was wrong

`dedup.py` claimed 種別 B was "**223 of the 223** duplicated album names in the real
library". Measured on the external drive:

| | external only | both roots |
|---|---|---|
| duplicated album names | 223 | 1,220 |
| 種別 B (every copy under a distinct artist) | **164** | 134 |
| 種別 A (≥2 copies under one artist) | **59** | 1,086 |
| └ A-only / mixed groups | 53 / 6 | 1,049 / 37 |

The load-bearing claim — all of them are A or B, so `strict` systematically misses them and
`loose` must be the default — was and remains correct, and remains asserted as a number in
`test_loose_scope_catches_collab_fanout`. Only the attribution to B alone was wrong. The
docstring now states the measured split: `loose` catches all 223, `strict` finds the 59 and
misses all 164. `test_skips_when_same_album_exists_in_two_places` also had "the real library
has 233 of these" for 種別 A, which is neither 59 nor 238; it now says 59.

A secondary number was stale in the same direction: "1,206 of 8,721 titles" → **1,207**
(re-measured; §7.2.1 says 1,206, off by one since that measurement). Corrected in both
`dedup.py` and `test_dedup.py`.

### m2 — the cost guard now measures `find_duplicate`

`test_scan_is_fast_enough_to_run_per_request` was a weaker duplicate of
`test_library_scan.py::test_scan_is_fast_enough_to_run_per_request` — same name, same body,
minus the warm-up — and it timed `scan_roots`, not the function this task added. The brief
mandated it, so writing it was right; keeping it as the only cost guard was not, because
`find_duplicate` is called **once per leaf track** on every request and had no bound.

**Replaced with two tests:**

- `test_find_duplicate_is_cheap_enough_to_call_per_track` — 2,000 calls over the fixture
  scan against a hit with two candidates on the `strict` path (the longer of the two).
- `test_find_duplicate_issues_no_syscall_per_call` — poisons `os.walk`/`scandir`/`listdir`/
  `stat`/`lstat`/`open` and `Path.stat`/`exists`/`is_dir`/`is_file`/`iterdir`/`glob`/`rglob`/
  `open` and `builtins.open`, so **any** filesystem call raises whatever it costs. Uses
  `monkeypatch.context()` rather than the fixture: pytest's own teardown, `tmp_path` cleanup
  and cache provider all call `os.stat` and `Path.exists`, so a patch left installed past the
  end of the test body fails the whole *session* instead of the test — which is what the
  first attempt did.

**Budget chosen from measurement, not taste:**

| | measured |
|---|---|
| `find_duplicate`, real 4,739-album-dir library, 20,000 calls | **0.0069 ms** (min 0.00690, median 0.00693, max 0.00710) |
| budget set | **0.1 ms** (~14× headroom) |
| per-call `rglob` walk of the fixture (the mutation) | **0.42 ms** (fails the budget by 4×) |

A 1 ms budget would **not** have caught it: with the walk injected the suite ran in 1.04 s
instead of 0.20 s, so the regression is visible in aggregate but no assertion moved. The
budget is 0.1 ms so the timing test genuinely fires, and headroom is safe because the figure
is a mean over 2,000 calls rather than a single one.

### m3 — what actually enforces the scoping

The module docstring said the scoping "is expressed in the shape of the data
(`by_name` → candidates → title) rather than in a filter that could be dropped". A mutant
sourcing candidates from `scan.albums` filtered by `album_key(a.name) == scope_key` is
green — equally correct, equally fast enough. The container is not what enforces it.

Reworded to name the invariant that is load-bearing: **a directory is a candidate only when
the album name it carries equals the album name being downloaded, and the title is then
tested only against those.** `scan.by_name` is how that set is obtained in one dict lookup,
not what makes the answer right. The docstring adds that the tests pin the invariant through
the observable answer — a hit names directories of one album and never of another — rather
than through which container the candidates came from, which is the right level for both the
real property and the mutant.

### Test commands and verbatim output

```
$ cd /home/m/apple-dl_extend/hub && PYTHONDONTWRITEBYTECODE=1 uv run pytest -v
```

```
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/apple-dl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/apple-dl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 90 items

tests/test_config.py::test_requires_password PASSED                      [  1%]
tests/test_config.py::test_requires_password_when_the_variable_is_absent PASSED [  2%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [  3%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [  4%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [  5%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [  6%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [  7%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [  8%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 10%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 11%]
tests/test_config.py::test_rejects_a_port_outside_the_bindable_range PASSED [ 12%]
tests/test_dedup.py::test_skips_when_same_album_exists_in_two_places PASSED [ 13%]
tests/test_dedup.py::test_loose_scope_catches_collab_fanout PASSED       [ 14%]
tests/test_dedup.py::test_strict_scope_requires_artist PASSED            [ 15%]
tests/test_dedup.py::test_strict_scope_answers_only_one_of_the_collab_placements PASSED [ 16%]
tests/test_dedup.py::test_strict_scope_normalizes_the_artist_directory_too PASSED [ 17%]
tests/test_dedup.py::test_strict_scope_refuses_an_artist_name_with_no_identifying_information PASSED [ 18%]
tests/test_dedup.py::test_strict_scope_refuses_when_the_artist_is_unknown PASSED [ 20%]
tests/test_dedup.py::test_a_hit_is_confined_to_the_album_that_was_asked_for PASSED [ 21%]
tests/test_dedup.py::test_the_global_answer_is_wider_than_the_scoped_one PASSED [ 22%]
tests/test_dedup.py::test_format_bucket_prefix_does_not_matter PASSED    [ 23%]
tests/test_dedup.py::test_album_name_keeps_single_suffix_and_deluxe PASSED [ 24%]
tests/test_dedup.py::test_refuses_to_skip_on_an_empty_album_name PASSED  [ 25%]
tests/test_dedup.py::test_refuses_to_skip_on_an_empty_title PASSED       [ 26%]
tests/test_dedup.py::test_unknown_album_returns_none PASSED              [ 27%]
tests/test_dedup.py::test_hit_paths_are_sorted_for_stable_display PASSED [ 28%]
tests/test_dedup.py::test_matched_is_sorted_even_when_the_scan_order_is_not PASSED [ 30%]
tests/test_dedup.py::test_artist_scope_defaults_to_loose PASSED          [ 31%]
tests/test_dedup.py::test_an_unknown_artist_scope_is_rejected PASSED     [ 32%]
tests/test_dedup.py::test_find_duplicate_never_touches_the_filesystem PASSED [ 33%]
tests/test_dedup.py::test_duplicate_hit_is_immutable PASSED              [ 34%]
tests/test_dedup.py::test_find_duplicate_is_cheap_enough_to_call_per_track PASSED [ 35%]
tests/test_dedup.py::test_find_duplicate_issues_no_syscall_per_call PASSED [ 36%]
tests/test_library_scan.py::test_finds_album_dirs_at_the_shallowest_level PASSED [ 38%]
...
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 90 passed in 0.20s ==============================
```

86 → **90 passed**, 0 failed. 22 of the 90 are Task 4's (was 18). `test_config.py` (11),
`test_library_scan.py` (40) and `test_normalize.py` (17) are unchanged and still pass.

```
$ uvx ruff@0.16.9 check hub/ tests/ spike/task4_real_library_check.py
All checks passed!
```

### Both requested mutation checks, both sides

**Delete `sorted(`:**

```
$ # hub/dedup.py:  tuple(sorted(c.relpath ...))  ->  tuple(c.relpath ...)
$ PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
FAILED tests/test_dedup.py::test_matched_is_sorted_even_when_the_scan_order_is_not
1 failed, 89 passed in 0.21s
E         Use -v to get more diff
tests/test_dedup.py:335: AssertionError
```

Verbose, showing the kill:

```
tests/test_dedup.py::test_matched_is_sorted_even_when_the_scan_order_is_not FAILED [100%]
```

Restored, unmutated:

```
$ PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
........................................................................ [ 80%]
..................                                                       [100%]
90 passed in 0.19s
```

**Delete the candidate-side `normalize()` in `_by_artist`:**

```
$ # hub/dedup.py:  normalize(c.artist) == artist_key  ->  c.artist == artist_key
$ PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
FAILED tests/test_dedup.py::test_strict_scope_requires_artist - AssertionErro...
FAILED tests/test_dedup.py::test_strict_scope_answers_only_one_of_the_collab_placements
FAILED tests/test_dedup.py::test_strict_scope_normalizes_the_artist_directory_too
FAILED tests/test_dedup.py::test_strict_scope_refuses_an_artist_name_with_no_identifying_information
FAILED tests/test_dedup.py::test_matched_is_sorted_even_when_the_scan_order_is_not
5 failed, 85 passed in 0.26s
```

Restored, unmutated:

```
$ PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
........................................................................ [ 80%]
..................                                                       [100%]
90 passed in 0.24s
```

**Cost-guard result — a per-call filesystem walk injected into `find_duplicate`:**

```
$ # hub/dedup.py:  for _root in scan.roots: list(_root.rglob("*"))
$ PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
FAILED tests/test_dedup.py::test_find_duplicate_is_cheap_enough_to_call_per_track
FAILED tests/test_dedup.py::test_find_duplicate_issues_no_syscall_per_call - ...
2 failed, 88 passed in 1.15s
```

Both fire. Note the 1.15 s wall time against a 0.20 s baseline — that is the cost of the
mutation, and it is why the budget was tightened to 0.1 ms rather than left at 1.0 ms
(see the m2 table). Restored:

```
$ PYTHONDONTWRITEBYTECODE=1 uv run pytest -q
........................................................................ [ 80%]
..................                                                       [100%]
90 passed in 0.20s
```

### Real-library baseline re-confirmed, unchanged

```
$ PYTHONDONTWRITEBYTECODE=1 uv run python spike/task4_real_library_check.py
album dirs            : 4739
distinct album names  : 3431
scan_roots            : 0.119 s

[1] external only: duplicated album names (>=2 dirs sharing a name): 223
    redundant directories                             : 238
    found by an album-name lookup                     : 222/223
    not askable (only unusable track keys in the group): 1

[1] both roots: duplicated album names (>=2 dirs sharing a name): 1220
    redundant directories                             : 1307
    found by an album-name lookup                     : 1214/1220
    every copy under a distinct artist (collab)   : 134
    >=2 copies under one artist (種別 A)           : 1086
    per-copy detection, loose                      : 2511/4739
    per-copy detection, strict (artist matches)    : 2510/4739

[2] 'intro'    in 6 album dirs, 6 distinct album names
    -> 6 per-album lookups, none crossed an album-name boundary (6 scopes, 6 dirs)
    'escapism' in 6 album dirs, 3 distinct album names   -> none crossed
    'mu'       in 6 album dirs, 3 distinct album names   -> none crossed
    'yoake'    in 6 album dirs, 3 distinct album names   -> none crossed
    '<empty>'  in 15 album dirs, 7 distinct album names  -> none crossed
```

Identical to the round-1 baseline: 223 / 238 external-only, 222 found, `intro` 0 leaks out
of 6, the empty album key present. The new fixture entry (`a/429 & nyankobrq/Named Album`)
is in `tmp_path` and does not touch the real libraries.

### Concerns

1. **M4 is still green and will stay green** — `normalize(c.artist or "")` is equivalent to
   `c.artist and normalize(c.artist)` given the `if not artist_key` guard, on every input.
   Not a coverage gap to close; recorded so a future reviewer does not re-report it as one.
   The compound that matters (M7) is killed.
2. **`test_find_duplicate_issues_no_syscall_per_call` is sensitive to pytest internals.** It
   poisons `os`/`Path`/`builtins.open`, which pytest's own teardown uses; the
   `monkeypatch.context()` scope is what keeps that contained. Widening the patch or moving
   to the `monkeypatch` fixture will fail the *session*, not the test — the first attempt did
   exactly that and it is worth knowing before someone "simplifies" it.
3. **The 0.1 ms budget is 14× headroom** over a measured 0.0069 ms. Safe as a mean over
   2,000 calls, but a much slower or much busier runner than this machine would narrow it.
   The failure message prints the measured figure so a breach is diagnosable rather than a
   mystery.
4. **Still outstanding from round 1:** the plan doc
   (`docs/superpowers/plans/2026-09-26-amd-hub-phase1.md`) was committed between rounds and
   its three inverted assertions and the "218 件" figure are now fixed (`47ea860`). One
   residue from *this* round: its Step 5 still prescribes
   `test_scan_is_fast_enough_to_run_per_request`, the duplicate m2 replaced, and should be
   re-worded to prescribe the `find_duplicate` cost guard.
