# Task 3 report — library scan, album discovery over multiple messy roots

Status: **DONE_WITH_CONCERNS** (the code is complete and green; the concerns are three
corrections to the brief, which the brief itself got wrong against its own fixture, and
one number in the design that is about `os.walk` rather than about a scan)

Branch: `feat/phase-1-foundation`
Spec: §7.3 Step 1, §8, §7.1.1, §12.1

---

## What I implemented

| File | Change | Contents |
|---|---|---|
| `hub/hub/library_scan.py` | new, 280 lines | `AlbumDir`, `LibraryScan` (+ `degraded`), `album_key()`, `scan_roots()`, and the private `_as_roots` / `_walk_root` / `_relpath` / `_artist` / `_is_bucket` |
| `hub/tests/test_library_scan.py` | new, 27 tests | every Review Focus hazard the task owns, plus the decisions below |
| `hub/tests/conftest.py` | modified | `make_library` (§12.1 regression tree), `make_library_extra` (album-name identity) |

The produced interface is exactly what the brief specifies — `AlbumDir(root_index,
relpath, name, artist, track_keys)`, `LibraryScan(roots, reachable, albums, by_name)`
with `degraded`, `scan_roots(roots) -> LibraryScan` — plus one additive export,
`album_key()`, explained under Decisions D6.

The five hazards from the plan's Review Focus, and where each is pinned:

| # | Hazard | Test |
|---|---|---|
| 1 | symlinked root | `test_root_that_is_a_symlink_is_accepted`, `test_a_walked_path_outside_the_root_raises_instead_of_finding_nothing` |
| 2 | empty / punctuation-only titles must stay *findable* | `test_empty_titles_are_representable_but_distinguishable` (and a mutation test, below) |
| 3 | `loose` false skip | not this task (Task 4) |
| 4 | unmounted external drive | `test_unreachable_root_is_marked_degraded`, `test_a_root_that_is_a_file_is_degraded_not_fatal`, `test_every_root_appears_in_order_with_its_own_index` |
| 5 | `.part` leftovers | `test_part_files_are_not_tracks`, `test_a_directory_of_only_part_files_is_not_an_album_dir` |
| — | `intro` × 6 in six albums stays six scopes | `test_does_not_descend_into_an_album_dir` / `test_walk_reaches_a_subdirectory_of_an_album_dir` (album scope + separate `Escapism` scope) |

---

## The three corrections to the brief

These are the concerns. In all three cases the brief's **fixture tree** and the brief's
**test assertion** contradict each other, and the fixture is the one with a second
consumer — Task 4's tests are written against these exact relpaths — so the fixture was
kept and the assertion corrected.

### C1. `test_does_not_descend_into_an_album_dir` asserted a key that is not in that scope

The brief's tree puts `x.m4a` in `lib/a/ALAC/TEMPLIME/POP-AID/` and `lib/a/TEMPLIME/POP-AID/`.
**Neither is directly in `a/TEMPLIME/`.** So the brief's

```python
assert "x" in temp.track_keys and "hiko" in temp.track_keys
```

cannot hold for any implementation. `a/TEMPLIME` directly holds only `HIKO.flac`, so
its key set is `{"hiko"}`.

Kept the assertion's *intent* and made it stronger, because it is a better test than the
original:

```python
assert temp.track_keys == frozenset({"hiko"})
assert "t" not in temp.track_keys     # Escapism's only track
assert "x" not in temp.track_keys     # POP-AID's track
```

The original only checked that `Escapism`'s key was not hoisted. This also pins that
`POP-AID`'s key is not hoisted, which is the same hazard one level down. `x.m4a` cannot
be moved: Task 4's `test_format_bucket_prefix_does_not_matter` looks up `POP-AID` with
`track_title="x"`.

### C2. `make_library` must return the tree, not a factory

The brief specifies the fixtures as `make_library(tmp_path)` but writes every test body as
`scan_roots(make_library)`. A pytest fixture cannot take `tmp_path` as an argument, and a
fixture that returned a *callable* would make every one of those test bodies
`scan_roots(make_library())`. The test bodies are the authority: the fixtures now build
the tree and return the root `Path`, which makes all seven brief-specified call sites
work verbatim.

### C3. `Nyarons` moved from `lib/a/Nyarons/A.flac` to `lib/Nyarons/A.flac`

The brief's tree has `Nyarons` under `a/`, and its test asserts that album dir's artist
is `None` ("undeterminable, not a guess"). Under `a/` the parent is a grouping directory
and the specified derivation ("else the parent basename is treated as the artist") returns
`"a"`, so the test contradicts the rule. No derivation can satisfy both that fixture and
the rule, and I checked the alternatives against the real data before choosing:

- **`artist = None` whenever the parent is the root** → `Music/Nyarons/A.flac` reports
  `artist=None` ✓, and this is what the brief's assertion and §8 step 3 both describe.
- **A depth rule instead** (`len(parts) >= 3` or `== 2` to mean "artist") → **rejected**:
  the real external library has **3,359 of its 3,670 album dirs at depth 2**, all of the
  form `<artist>/<album>` (`Nyarons/Chika`, `TEMPLIME/Escapism`, `r-906/Blütenstand`, …),
  and all 1,069 album dirs of `downloads/` are at depth 2 as `artist/album/`. Any depth
  rule zeroes out the artist for the whole library. Depth is *not* the signal; whether
  the parent is the root is.

So the rule is the one the parent specified, with the one precondition it left implicit:
**the root itself is never an artist.** Placing `Nyarons` at the top of the tree — which
is what the real library actually looks like (§12.1's own table reads `Nyarons/A.flac`
（artist dir 直下）; on disk it is `Music/Nyarons/A.flac`, verified) — makes the brief's
assertion true under the specified derivation instead of contradicting it. No Task 4 test
references `a/Nyarons`.

---

## Decisions the brief left open

**D1. `relpath` is a lexical prefix slice, and it raises rather than guessing.**
`_relpath` cuts `dirpath` against the root *string the caller passed* and never calls
`resolve()`, `realpath`, or `Path.relative_to` on anything. If a path ever fails to start
with that prefix it raises `ValueError` naming both values. That branch is unreachable
from `os.walk` (it only ever joins names onto the root it was handed), which is exactly
why it is worth having: the mistake it guards against is *someone normalizing the root in
one place and walking it in another*, and its symptom would otherwise be a scan that
reports zero albums with no error — every download silently re-running. Loud beats
silent; `test_a_walked_path_outside_the_root_raises_instead_of_finding_nothing` pins it.

Note the harmless version of the mistake: resolving the root *and* walking the resolved
string is internally consistent and produces identical relpaths. Only resolving one side
breaks. The suite catches both one-sided variants (see mutation M5/M6) and cannot
distinguish the two-sided one, because there is nothing to distinguish.

**D2. `os.walk`'s own `filenames` instead of a second `os.listdir` per directory.** The
brief says "whose immediate `os.listdir` contains at least one audio file". `os.walk`
already returns that directory's non-directory entries from the same `scandir` that
produced `dirnames`, so re-listing would double the syscalls for an identical answer. The
two differ only if a *directory* is named `something.m4a`. Measured: 0.049 s with the
`os.walk` filter, and the design's 0.06 s target does not survive a second listdir pass
on a cold cache.

**D3. `dirnames.sort()` per directory, not a global sort of relpaths.** NTFS does not sort
readdir, and relpaths reach the user in `skip_reason` and in the library list, so two
scans of an unchanged tree have to agree. This is per-directory order, which is what makes
a subtree contiguous; a global sort of the finished relpath strings would order
`"A B/z"` before `"A/a"` in a way that splits nothing useful. Measured cost: **0.000 s**
(0.044 s median with and without).

**D4. A root that is itself an album dir is a scope, with `relpath == "."`.** This is real:
`Music/Hatsuboshi Gakuen & Kotone Fujita – Yellow Big Bang!.m4a` sits directly in the
root. Excluding the root would make every such file invisible to dedup, silently.
`"."` is also what `Path.relative_to` returns for the root against itself, so
`roots[i] / relpath` still names the directory. Its `name` is the root's own directory
name and its `artist` is `None`.

**D5. The `artist` derivation, in the specified order, with the root excluded.** (§8)

```
len(parts) < 2                    -> None   # the parent is the root
grandparent ∈ FORMAT_BUCKETS     -> parent # ALAC/Atmos group by codec, not by artist
parent ∈ FORMAT_BUCKETS          -> None   # no artist level at all; the level above is the root
otherwise                        -> parent # dirPathFormat's artist/album/
```

The third line is mine and is the only narrowing of the specified order. It is not
observed — the real library has **zero** album dirs with a bucket parent — so it is there
to keep the deduction sound rather than because a measurement demanded it. The
grandparent check is implemented literally even though it returns the same value as the
fallback, because the order is the spec's and the reason the parent is safe to trust is
worth having written down where the code is.

**D6. `album_key()` is exported.** `by_name` is keyed with
`normalize(basename, strip_track_prefix=False)`, and Task 4 has to look up with the same
flag. The parent's own note calls that flag "the highest-consequence parameter in the
project" — a mismatch makes every album lookup miss and nothing is ever skipped, with no
error anywhere. Exposing one function that is the single definition of the key means the
index and the lookup cannot drift. Task 4's signature is unchanged; it may call
`album_key(album_name)` instead of repeating the call.

**D7. A symlinked directory *inside* a root is not followed** (`os.walk` default). The
symlinked *root* is followed, which is the user's case and Review Focus #1. Not following
inner links keeps a per-request walk bounded (a link back to an ancestor would otherwise
recurse forever on every download request) and keeps every reported path inside the root
the user configured, which §11's `realpath`-under-root file-serving check depends on.
The real library contains **no symlinks at all**, so nothing is lost. Pinned by
`test_a_symlinked_directory_inside_a_root_is_not_followed`.

**D8. `by_name` is a `MappingProxyType`, and an empty key is not filtered out.** A frozen
dataclass holding a plain dict is not frozen in any way that matters, and this object is
handed to the API layer and to Task 4. Separately, an album dir whose name normalizes to
`""` *is* indexed under `""`, and the real library has one: `ALAC/薄塩指数/!_`. It is
reachable, so the empty-key question is an album-side one, and no guard answers it yet —
see "Task 4 follow-up" in Fix round 2.

**D9. `.part` gets no second check**, per the parent's decision and because `AUDIO_EXTS`
already excludes it. Verified: 160 `.part` files on disk, **0** appear in any
`track_keys`; and a directory containing *only* a `.part` is not an album dir at all.

---

## Test command and verbatim output

```
$ cd hub && uv run pytest -v
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 54 items

tests/test_config.py::test_requires_password PASSED                      [  1%]
tests/test_config.py::test_requires_password_when_the_variable_is_absent PASSED [  3%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [  5%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [  7%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [  9%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [ 11%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [ 12%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [ 14%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 16%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 18%]
tests/test_config.py::test_rejects_a_port_outside_the_bindable_range PASSED [ 20%]
tests/test_library_scan.py::test_finds_album_dirs_at_the_shallowest_level PASSED [ 22%]
tests/test_library_scan.py::test_does_not_descend_into_an_album_dir PASSED [ 24%]
tests/test_library_scan.py::test_walk_reaches_a_subdirectory_of_an_album_dir PASSED [ 25%]
tests/test_library_scan.py::test_groups_same_named_album_dirs_across_roots PASSED [ 27%]
tests/test_library_scan.py::test_album_name_index_keeps_leading_digits PASSED [ 29%]
tests/test_library_scan.py::test_album_name_index_keeps_a_number_a_separator_follows PASSED [ 31%]
tests/test_library_scan.py::test_a_track_number_is_still_stripped_from_track_keys PASSED [ 33%]
tests/test_library_scan.py::test_album_name_index_keeps_single_and_deluxe_suffixes PASSED [ 35%]
tests/test_library_scan.py::test_derives_artist_from_structure PASSED    [ 37%]
tests/test_library_scan.py::test_artist_looks_past_a_format_bucket PASSED [ 38%]
tests/test_library_scan.py::test_artist_of_a_dir_whose_parent_is_the_root_is_unknown PASSED [ 40%]
tests/test_library_scan.py::test_a_root_that_is_itself_an_album_dir_is_still_a_scope PASSED [ 42%]
tests/test_library_scan.py::test_part_files_are_not_tracks PASSED        [ 44%]
tests/test_library_scan.py::test_a_directory_of_only_part_files_is_not_an_album_dir PASSED [ 46%]
tests/test_library_scan.py::test_empty_titles_are_representable_but_distinguishable PASSED [ 48%]
tests/test_library_scan.py::test_unreachable_root_is_marked_degraded PASSED [ 50%]
tests/test_library_scan.py::test_every_root_appears_in_order_with_its_own_index PASSED [ 51%]
tests/test_library_scan.py::test_root_that_is_a_symlink_is_accepted PASSED [ 53%]
tests/test_library_scan.py::test_a_walked_path_outside_the_root_raises_instead_of_finding_nothing PASSED [ 55%]
tests/test_library_scan.py::test_a_symlinked_directory_inside_a_root_is_not_followed PASSED [ 57%]
tests/test_library_scan.py::test_repeated_scans_of_one_tree_agree PASSED [ 59%]
tests/test_library_scan.py::test_a_later_change_is_visible_without_a_rescan_hook PASSED [ 61%]
tests/test_library_scan.py::test_no_roots_yields_an_empty_but_valid_scan PASSED [ 62%]
tests/test_library_scan.py::test_a_root_that_is_a_file_is_degraded_not_fatal PASSED [ 64%]
tests/test_library_scan.py::test_scan_is_fast_enough_to_run_per_request PASSED [ 66%]
tests/test_library_scan.py::test_album_dirs_are_reported_in_a_stable_order PASSED [ 68%]
tests/test_normalize.py::test_stem_of_drops_only_a_known_audio_extension PASSED [ 70%]
tests/test_normalize.py::test_stem_of_keeps_a_name_whose_dot_is_not_an_extension PASSED [ 72%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 74%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 75%]
tests/test_normalize.py::test_normalize_keeps_a_four_digit_year PASSED   [ 77%]
tests/test_normalize.py::test_normalize_folds_decomposed_and_composed_equally PASSED [ 79%]
tests/test_normalize.py::test_normalize_folds_fullwidth_to_halfwidth PASSED [ 81%]
tests/test_normalize.py::test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title PASSED [ 83%]
tests/test_normalize.py::test_normalize_does_not_swallow_a_title_leading_dot_run PASSED [ 85%]
tests/test_normalize.py::test_audio_exts_membership_table PASSED         [ 87%]
tests/test_normalize.py::test_normalize_keeps_a_separator_with_padding_and_stops_at_two_groups PASSED [ 88%]
tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping PASSED [ 90%]
tests/test_normalize.py::test_is_audio_file_is_case_insensitive PASSED   [ 92%]
tests/test_normalize.py::test_normalize_squeezes_whitespace PASSED       [ 94%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 96%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 98%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 54 passed in 0.10s ==============================
```

(`uv run pytest -q` → `54 passed in 0.11s`. `uvx ruff check` on the three files →
`All checks passed!`; `uvx ruff format --diff` → `3 files already formatted`. Ruff is not
a declared dependency of the project — I ran it as a one-off and did not add it.)

### Mutation evidence

The brief's own test for the `strip_track_prefix` flag **does not work**, and so does
its most-repeated test. I found this by mutating the implementation and re-running, so
the numbers below are measured, not asserted:

| # | Mutation | Result |
|---|---|---|
| M1 | `relpath` via `Path(dirpath).resolve().relative_to(root)` | **caught** — 20 failed |
| M2 | `normalize(os.path.join(parent, name))` instead of the basename | **caught** — 5 failed |
| M3 | `realpath()` the root for the prefix, walk the unresolved root | **caught** — 1 failed, by the new `ValueError` (D1) |
| M4 | drop the `""` key from `track_keys` | **caught** — `test_empty_titles_…` |
| M5 | `by_name` keyed with `strip_track_prefix=True` | **caught, but only after the fixture changed** — see below |
| M6 | `track_keys` built with `strip_track_prefix=False` | **caught** — 4 failed |
| — | `root.resolve()` used for *both* the walk and the prefix | not caught, and not catchable: resolving both sides is internally consistent and yields identical relpaths. Not a bug. |

M5 is the finding worth carrying forward. `4pi` and `1st EP` — the two names the brief
picked to pin the flag — **cannot** distinguish it: a number glued to a letter is not a
track prefix, so `normalize` leaves both alone and both flags produce the same key. I
first tried `1999 - Best`, which is also immune, because `\d{1,3}` deliberately does not
match a 4-digit year (§7.5, pinned by Task 2's `test_normalize_keeps_a_four_digit_year`).
`make_library_extra` now also contains `4 - Leaves/01 t.m4a` — a 1-to-3 digit number
followed by a separator, the shape of a real numbered series — and
`test_album_name_index_keeps_a_number_a_separator_follows` asserts
`"4 - leaves" in by_name` **and** `"leaves" not in by_name`. With the flag on, that test
fails. **Without it, the project's highest-consequence parameter is untested.**

---

## Real-library verification

`/run/media/m/1A5E05A75E057D2F/Music`, external NTFS, 341 GB, mounted. Both the direct
path and the user's configured symlink were scanned.

| Metric | design §7.1.1 / §7.2.1 | measured here |
|---|---|---|
| directories walked | 4,367 | **4,367** ✓ |
| audio files | 10,184 | **10,184** ✓ |
| album directories | 3,670 | **3,670** ✓ |
| `os.walk` alone, stat only | 0.06 s | **0.044 s** (median of 5) |
| full `scan_roots` | *(not measured by the design)* | **0.091 s** (median of 5; range 0.094–0.105) |
| same scan via `/home/m/Music/HDD_Music` | — | **identical result**, 0.096 s |
| `.part` files on disk / in `track_keys` | 160 / — | **160 / 0** ✓ |
| `intro`, `escapism`, `mu`, `yoake` | 6 album dirs each | **6 each** ✓ |
| duplicate album-dir names | 218 names / 233 extra | **223 names / 238 extra** |
| titles in >1 album dir | 1,200 / 8,732 (13.7%) | **1,206 / 8,720 (13.8%)** |
| empty-string title | 6 album dirs | **8 album dirs** contain an empty key |

**`artist is None`: 10 of 3,670 (0.27%).** All ten are the album dirs whose parent is the
root: the root itself (`"."`, the loose `Yellow Big Bang!.m4a`), and the nine artist
directories at the top of the drive — `Neko Hacker`, `Nyarons`, `Sanso Nakamura`,
`Sasuke Haraguchi`, `TEMPLIME`, `r-906`, `いよわ`, `ミツキヨ`, `桃寝ちのい`. The
derivation therefore covers **99.73%** of this library, and the 0.27% it misses is exactly
the set where no artist exists to find: those directories *are* the artist, or are the
library root, so `None` ("Unknown", per §8) is the honest answer rather than a gap.

Album-dir depth histogram: **1 at depth 0** (the root), **9 at depth 1**, **40 at depth
2**, **3,319 at depth 3**, **301 at depth 4**. Zero album dirs have a format bucket as
their parent; the tree contains no symlinks at all.

The other configured root, `AppleMusicDecrypt/downloads/` (69 GB, 212 artist dirs): **1,069
album dirs, all at depth 2** (`artist/album/`, i.e. `dirPathFormat` as documented), **0
with `artist is None`** — 100% coverage — in 0.029 s. Both roots together: **4,739 album
dirs, 0.13 s, 10 None**.

### The three numbers that differ from the design, and why

1. **0.06 s is the cost of `os.walk`, not of a scan.** §7.1.1's table labels the figure
   "`os.walk`（`stat` のみ、タグ読みなし）", which is accurate: I measure 0.044 s for that.
   The full `scan_roots` — plus the extension filter, `normalize()` on all 10,184 names,
   3,670 frozen dataclasses and the `by_name` grouping — is **0.091 s**. Broken down:
   walk 0.044, `is_audio_file` filter 0.005, `normalize` 0.015, dataclass construction
   ~0.038, `by_name` grouping ~0.009. The plan's one-line summary ("measured 0.06 s for
   4,367 dirs / 10,184 files") reads as though 0.06 s were the whole scan; it is not, and
   the real figure is ~1.5×. **This does not change the decision** — §7.1.1's conclusion
   ("リクエストごとに全走査してよい", no cache) survives 0.09 s comfortably — but a task
   that later asserts a per-scan budget should use 0.1 s, not 0.06 s.
2. **223 duplicate album-dir names / 238 extra, against 218 / 233.** And 1,206 / 8,720
   titles against 1,200 / 8,732. The design's figures are a **lower bound**: they were
   measured with an ad-hoc script, whereas this scanner applies full `normalize()` (NFKC
   + casefold + whitespace squeeze), so a few more names and titles collapse. Same
   direction, same order of magnitude, and the headline "over 200 duplicated album
   directories" is confirmed. §8.2's duplicate report should expect the larger numbers.
3. **8 album dirs hold an unusable title, against the design's 6.** Counting method
   differs (I count directories containing at least one empty key; the design's table
   lists the title's occurrence count). Not material — the point §7.2.1 makes is that
   empty keys exist and must not be allowed to match, and 8 dirs is the same finding.

### What this means for the design, not just for this task

- **No cache, confirmed.** 0.09–0.13 s per request on a 341 GB + 69 GB pair, warm. The
  decision to re-scan and keep no state holds with a wide margin.
- **`artist_scope=strict` is viable.** With 99.73% / 100% artist coverage, strict matching
  has a name to compare against almost everywhere; the 10 `None` dirs are already handled
  as "cannot vouch for this".
- **The §8.2 duplicate report will be slightly larger than §7.2.1 says** — see finding 2.
- **10 album dirs at the top of the drive have no artist.** The library UI must render
  `Unknown` there (per §8) rather than deriving a name from the path, and the root-level
  `.` album dir needs a display name that is not the literal string `"."`.

---

## What Task 4 inherits

- `scan_roots` accepts `Sequence[Path] | Path`; `Settings.library_roots` is a `list[Path]`
  and Task 9's `scan_roots(settings.library_roots)` is unchanged.
- `by_name` lookups should call `album_key(album_name)` (D6) rather than re-spelling
  `normalize(..., strip_track_prefix=False)`.
- `track_keys` can contain `""`. Task 4's existing step 1 — `if title_key == "": return
  None` — is the guard, and it is now demonstrably reachable: 8 album dirs in the real
  library carry an empty key, and 4 of them would otherwise match any untitled track.
- `AlbumDir.relpath` is posix-form and joinable: `scan.roots[a.root_index] / a.relpath`.
  §8's file id (`blake2b` of root index + relpath) can be built from these directly.
- `LibraryScan.degraded` is already positional against `roots`, so §8.1's banner can name
  the missing drive without any extra bookkeeping.

---

# Fix round 1

Four review findings (I1, I2, I3) and six minors (M1–M6). No Critical issues; the
symlink handling, the descend-past-album-dirs behaviour, the `.part` exclusion and every
real-library number were independently verified correct and are untouched.

## I1 — the root scope is out of `by_name` and has no name

`AlbumDir.name` for the root scope came from `os.path.basename(dirpath)`, so one drive
had two different keys depending on the spelling of the path, and the two spellings
compared unequal. Confirmed on the real library before the fix:

```
/run/media/m/1A5E05A75E057D2F/Music  ->  by_name["music"]     == [(".",)]
/home/m/Music/HDD_Music              ->  by_name["hdd_music"] == [(".",)]
```

Three changes:

- `AlbumDir.name` is `""` for the root scope. No directory on any filesystem can have an
  empty name, so it cannot be the name of a real album — whereas a basename taken from the
  caller's spelling of the path can, and would depend on how §8.1's two reachability call
  sites happen to write the path.
- `scan_roots` skips the root scope when building `by_name` (`_ROOT_SCOPE = "."`, a module
  constant shared with `_walk_root`). The scope stays in `albums`, so its `track_keys`
  remain available; it is simply not an album *name*.
- Tests: `test_the_root_scope_is_not_indexed_as_an_album_name` (the drive contains a real
  album literally named `Music`, and it must hold only itself in `by_name["music"]`),
  `test_by_name_is_the_same_however_the_root_is_spelled` (builds a drive named `Music`,
  scans it directly and through an `HDD_Music` symlink, asserts `albums` and `by_name` are
  both equal and that neither basename is a key).

`test_a_root_that_is_itself_an_album_dir_is_still_a_scope` was kept unchanged, so the
exclusion cannot be mistaken for the scope being dropped, and
`test_the_root_scope_is_not_indexed_as_an_album_name` is where the root's absence from
`by_name` is asserted.

## I2 — the `FORMAT_BUCKETS` check is deleted

The rule is now one line, `parent's basename if a parent exists, else None`, and
`FORMAT_BUCKETS` is no longer imported. `_is_bucket` is gone. Confirmed by mutation that
the old code was unreachable-as-a-rule *and* wrong on `ALAC/Atmos/Album`, where it
answered `None`.

The tree-level test alone did not catch it: `ALAC/Atmos/Some Album` returns `"Atmos"`
under the grandparent branch too, so my first attempt at this test passed with the check
re-added. The rule is now pinned as a mapping, table-driven over the relpath components
(`test_artist_is_the_parent_name_and_nothing_else`, 7 cases), including
`("Some", "Atmos", "Album")` — a bucket parent that is *not* below a bucket, which is the
one input where "grandparent is a bucket" and "parent is a bucket" disagree. Plus
`test_artist_is_the_parent_directory_name` (the `ALAC/`-prefixed and unprefixed copies of
`POP-AID` answer the same, with no exception in the code to make it so) and
`test_an_album_with_no_artist_level_reports_the_codec_directory` (pins the one input the
rule gets wrong, so nobody later assumes it is handled).

No directory that has a parent changed: **10 of 3,670** still have `artist is None`, the
same ten as before.

## I3 — the `is_audio_file` contract is pinned

`make_library` gained `a/Album Seven/{01 real.m4a, COVER.FLAC, .m4a}`, labelled in the
fixture as contract probes rather than observed shapes (every extension in the real library
is lowercase, and no file there is named `.m4a` — which is exactly why they have to be
constructed). `test_is_audio_file_decides_what_is_a_track` asserts the key set is exactly
`{"real", "cover"}` and that `".m4a"` is not in it. A casefolding implementation passes
both, which is the point: case-insensitivity is the contract, the mechanism is not.

## Minors

- **M1** `test_album_name_index_keeps_leading_digits` no longer claims to catch the flag.
  It now says what it pins: a number glued to a word is not a track number, so `4pi` and
  `1st EP` are reachable under *either* flag. The flag claim moved entirely to
  `test_album_name_index_keeps_a_number_a_separator_follows`.
- **M2** D8 was wrong and is corrected. The real library does have an album name that
  normalizes to nothing — `ALAC/薄塩指数/!_` — and it stays indexed under `""`. Decision
  kept (Task 4 refuses to skip on an empty key), the note's "unreachable because a real
  album always has a name" is gone from the code, but the same claim survived in D8 above
  and has been removed there too (Fix round 2). The decision is now pinned by
  `test_an_album_name_that_normalizes_to_nothing_is_still_indexed` over a new `a/!_/` entry
  in the fixture, so "filter it" is no longer a change that passes the suite.
- **M3** `test_album_dirs_are_reported_in_a_stable_per_directory_order` now pins what is
  actually guaranteed: two scans of an unchanged tree agree, and **siblings** are in
  sorted order within each parent. The global-sort claim is gone. The fixture gained
  `a/A/x.m4a` and `a/A B/z.m4a` so the test cannot pass by coincidence: `"A"` sorts before
  `"A B"`, so `a/A` is reported first, which is the opposite of what a global sort of the
  relpath strings would do (a space beats a slash).
- **M4** renamed to match what they assert:
  `test_does_not_descend_into_an_album_dir` →
  `test_an_album_dirs_keys_do_not_include_a_subdirectorys_tracks` (key scoping), and
  `test_walk_reaches_a_subdirectory_of_an_album_dir` →
  `test_the_walk_descends_past_an_album_dir_into_its_subdirectories` (traversal).
- **M5** `library_scan.py`'s module docstring now says what the 0.06 s is — the cost of
  `os.walk` alone — and gives the measured breakdown of the 0.091 s scan beside it.
  `conftest.py` cites 223 duplicated album-dir names, not 218.
- **M6** `scan_roots`' docstring now names the accepted types outright
  (`Sequence[Path]`, a single `Path`, a single `str`) and states that the result is
  `tuple[Path, ...]` either way, in the order given, with a slot kept for every input.

## Test command and verbatim output

```
$ cd hub && uv run pytest -v
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 66 items

tests/test_config.py::test_requires_password PASSED                      [  1%]
tests/test_config.py::test_requires_password_when_the_variable_is_absent PASSED [  3%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [  4%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [  6%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [  7%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [  9%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [ 10%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [ 12%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 13%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 15%]
tests/test_config.py::test_rejects_a_port_outside_the_bindable_range PASSED [ 16%]
tests/test_library_scan.py::test_finds_album_dirs_at_the_shallowest_level PASSED [ 18%]
tests/test_library_scan.py::test_an_album_dirs_keys_do_not_include_a_subdirectorys_tracks PASSED [ 19%]
tests/test_library_scan.py::test_the_walk_descends_past_an_album_dir_into_its_subdirectories PASSED [ 21%]
tests/test_library_scan.py::test_groups_same_named_album_dirs_across_roots PASSED [ 22%]
tests/test_library_scan.py::test_album_name_index_keeps_leading_digits PASSED [ 24%]
tests/test_library_scan.py::test_album_name_index_keeps_a_number_a_separator_follows PASSED [ 25%]
tests/test_library_scan.py::test_a_track_number_is_still_stripped_from_track_keys PASSED [ 27%]
tests/test_library_scan.py::test_album_name_index_keeps_single_and_deluxe_suffixes PASSED [ 28%]
tests/test_library_scan.py::test_an_album_name_that_normalizes_to_nothing_is_still_indexed PASSED [ 30%]
tests/test_library_scan.py::test_derives_artist_from_structure PASSED    [ 31%]
tests/test_library_scan.py::test_artist_is_the_parent_directory_name PASSED [ 33%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts0-None] PASSED [ 34%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts1-None] PASSED [ 36%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts2-TEMPLIME] PASSED [ 37%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts3-TEMPLIME] PASSED [ 39%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts4-EmoCosine] PASSED [ 40%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts5-Atmos] PASSED [ 42%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts6-Atmos] PASSED [ 43%]
tests/test_library_scan.py::test_an_album_with_no_artist_level_reports_the_codec_directory PASSED [ 45%]
tests/test_library_scan.py::test_artist_of_a_dir_whose_parent_is_the_root_is_unknown PASSED [ 46%]
tests/test_library_scan.py::test_a_root_that_is_itself_an_album_dir_is_still_a_scope PASSED [ 48%]
tests/test_library_scan.py::test_the_root_scope_is_not_indexed_as_an_album_name PASSED [ 50%]
tests/test_library_scan.py::test_by_name_is_the_same_however_the_root_is_spelled PASSED [ 51%]
tests/test_library_scan.py::test_is_audio_file_decides_what_is_a_track PASSED [ 53%]
tests/test_library_scan.py::test_part_files_are_not_tracks PASSED        [ 54%]
tests/test_library_scan.py::test_a_directory_of_only_part_files_is_not_an_album_dir PASSED [ 56%]
tests/test_library_scan.py::test_empty_titles_are_representable_but_distinguishable PASSED [ 57%]
tests/test_library_scan.py::test_unreachable_root_is_marked_degraded PASSED [ 59%]
tests/test_library_scan.py::test_every_root_appears_in_order_with_its_own_index PASSED [ 60%]
tests/test_library_scan.py::test_root_that_is_a_symlink_is_accepted PASSED [ 62%]
tests/test_library_scan.py::test_a_walked_path_outside_the_root_raises_instead_of_finding_nothing PASSED [ 63%]
tests/test_library_scan.py::test_a_symlinked_directory_inside_a_root_is_not_followed PASSED [ 65%]
tests/test_library_scan.py::test_repeated_scans_of_one_tree_agree PASSED [ 66%]
tests/test_library_scan.py::test_a_later_change_is_visible_without_a_rescan_hook PASSED [ 68%]
tests/test_library_scan.py::test_no_roots_yields_an_empty_but_valid_scan PASSED [ 69%]
tests/test_library_scan.py::test_a_root_that_is_a_file_is_degraded_not_fatal PASSED [ 71%]
tests/test_library_scan.py::test_scan_is_fast_enough_to_run_per_request PASSED [ 72%]
tests/test_library_scan.py::test_album_dirs_are_reported_in_a_stable_per_directory_order PASSED [ 74%]
tests/test_normalize.py::test_stem_of_drops_only_a_known_audio_extension PASSED [ 75%]
tests/test_normalize.py::test_stem_of_keeps_a_name_whose_dot_is_not_an_extension PASSED [ 77%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 78%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 80%]
tests/test_normalize.py::test_normalize_keeps_a_four_digit_year PASSED   [ 81%]
tests/test_normalize.py::test_normalize_folds_decomposed_and_composed_equally PASSED [ 83%]
tests/test_normalize.py::test_normalize_folds_fullwidth_to_halfwidth PASSED [ 84%]
tests/test_normalize.py::test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title PASSED [ 86%]
tests/test_normalize.py::test_normalize_does_not_swallow_a_title_leading_dot_run PASSED [ 87%]
tests/test_normalize.py::test_audio_exts_membership_table PASSED         [ 89%]
tests/test_normalize.py::test_normalize_keeps_a_separator_with_padding_and_stops_at_two_groups PASSED [ 90%]
tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping PASSED [ 92%]
tests/test_normalize.py::test_is_audio_file_is_case_insensitive PASSED   [ 93%]
tests/test_normalize.py::test_normalize_squeezes_whitespace PASSED       [ 95%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 96%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 98%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 66 passed in 0.12s ==============================
```
```

(`uv run pytest -q` → `66 passed in 0.10s`. `uvx ruff check` → `All checks passed!`;
`uvx ruff format --diff` → `3 files already formatted`.)

## Mutation checks

| Finding | Mutation | Result |
|---|---|---|
| I2 | re-add both bucket checks (grandparent → parent, bucket parent → `None`) | **caught** — `test_artist_is_the_parent_name_and_nothing_else[parts6-Atmos]`, `_artist(("Some","Atmos","Album"))` returned `None` |
| I2 | re-add *only* the grandparent check | not caught, and not catchable: both of its branches return the parent, so it is a no-op. Documented in the table test's comment rather than chased. |
| I3 | `os.path.splitext(name)[1] in AUDIO_EXTS` | **caught** — `test_is_audio_file_decides_what_is_a_track` |
| I3 | `name.lower().endswith(tuple(AUDIO_EXTS))` | **caught** — `test_is_audio_file_decides_what_is_a_track` |
| I3 | `PurePath(name).suffix.lower() in AUDIO_EXTS` | not caught, and correctly so: it casefolds, which is the contract. |

```
$ uv run pytest -q          # with the I2 bucket checks re-added
E        +  where None = _artist(('Some', 'Atmos', 'Album'))
tests/test_library_scan.py:158: AssertionError
FAILED tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts6-Atmos]
1 failed, 65 passed in 0.15s

$ uv run pytest -q          # with is_audio_file replaced by splitext membership
tests/test_library_scan.py:228: AssertionError
FAILED tests/test_library_scan.py::test_is_audio_file_decides_what_is_a_track
1 failed, 58 passed in 0.14s

$ uv run pytest -q          # with is_audio_file replaced by endswith
tests/test_library_scan.py:228: AssertionError
FAILED tests/test_library_scan.py::test_is_audio_file_decides_what_is_a_track
1 failed, 58 passed in 0.14s
```

## Real-library re-scan: the `by_name` invariance

`/run/media/m/1A5E05A75E057D2F/Music` and `/home/m/Music/HDD_Music`, the same drive:

```
direct  0.095s  album_dirs=3670  by_name keys=3431
symlink 0.095s  album_dirs=3670  by_name keys=3431

albums equal            : True
by_name equal           : True
relpaths equal          : True
artist None             : 10 / 3670
root scope name         : ''
root scope in by_name   : False
keys in >1 dir          : 223 (extra dirs 238)
keys normalizing to ''  : True -> ['!_']
''-keyed album relpath  : ['ALAC/薄塩指数/!_']
```

**`by_name` key count: 3,432 → 3,431.** The one key that disappeared is the root scope's
— `"music"` when the drive is named directly, `"hdd_music"` through the symlink. The two
spellings previously produced *different* key sets; they are now identical, and so is
`scan.albums`, which was also unequal before because of that one `name`. Every other
figure is unchanged: 3,670 album dirs, 223 names in more than one directory / 238 extra
dirs, 10 with no artist, 0.095 s per scan. Both roots together: 4,739 album dirs, 3,431
keys, 0.122 s.

The `""` key is real and is the concrete answer to M2: `ALAC/薄塩指数/!_` is indexed under
it, as the fix-round decision says it should be, and it **does** match — the plan's
empty-key guard is on the track title, and nothing guards the album-name side.

---

# Fix round 2

No behavioural regression. Two partials (M2, M3), one coverage hole (C1) and four small
items (B1, B3, B4, B5, plus the M5 nits) closed. The `is_audio_file` call site and the
`_artist` simplification are untouched, as instructed.

## C1 — the artist wiring site is now pinned through the scanner

The finding is right that the call site was untested; the token named in the review is not
the one that mattered. Measured before the fix:

| Mutation of `artist=_artist(parts)` | All 66 tests |
|---|---|
| `parts` → `parts[1:]` | pass |
| `parts` → `parts[2:]` | **2 failed** — `test_artist_is_the_parent_directory_name`, `test_an_album_with_no_artist_level_reports_the_codec_directory` |

**Correction (round 3): the "untestable" argument above was wrong.** `_artist` does not
index `[-2]` unconditionally — it returns `None` when `len(parts) < 2`. So the two slices
agree only while the result is non-`None`; at length 2 the truthiness guard bites and
`parts[1:]` (length 1) answers `None` where `parts` answers `parts[0]`. Length 2 is exactly
the depth-2 shape C1 is about, so `parts[1:]` **is** testable, and the test added below
catches it. The claim contradicted this section's own evidence fifteen lines further down.
`parts[2:]` was already caught pre-fix, but only incidentally — by a depth-3 fixture entry
that happened to become a 1-element slice.

The underlying point is real, so the fixture now carries the two shapes that were missing,
and the assertions read the artist off `scan_roots` instead of off `_artist`:

- `a/Artist/Album/x.m4a`, scanned with `a/` as the root, is the depth-2
  `<root>/<artist>/<album>` shape — 1,069 album dirs in `downloads/` and 40 in the
  external library are exactly this, and it is the shallowest one the scanner can meet.
  Nothing else in the fixture reaches it, because the `a/` prefix puts everything else at
  depth 3 or more. Pinned by `test_artist_of_a_depth_two_album_dir`, which also scans the
  same directory with the fixture root in front, so both depths answer the same artist.
- `a/Some/Atmos/Album/t.m4a` is the disputed nested-bucket shape at tree level, and the
  one input the removed bucket rule got wrong. Pinned by
  `test_artist_of_a_codec_directory_that_is_not_below_a_codec_directory`.

Both mutations now fail. Re-measured in round 3 with the **full** `short test summary`
rather than a `tail`, which is what the round-2 version of this block truncated:

```
$ uv run pytest -q          # artist=_artist(parts[1:])
=========================== short test summary info ============================
FAILED tests/test_library_scan.py::test_artist_of_a_depth_two_album_dir - Ass...
1 failed, 67 passed in 0.13s

$ uv run pytest -q          # artist=_artist(parts[2:])
=========================== short test summary info ============================
FAILED tests/test_library_scan.py::test_artist_of_a_depth_two_album_dir - Ass...
1 failed, 67 passed in 0.13s
```

The review measured **4 failed, 64 passed** for the `parts[2:]` mutation, which I could not
reproduce: that is the count I get for the call-site slice, on the committed tree, with the
whole summary printed. I also tried two other ways of losing components and none produced
4 either — `parts = tuple(relpath.split("/")[2:])` and `…[1:]` each fail **16** tests
(they break `name` and every `by_name` lookup, not just the artist), and changing
`_artist`'s body to `parts[-3]` fails **10**. The exact edit applied is
`hub/hub/library_scan.py:226`, `artist=_artist(parts),` → `artist=_artist(parts[2:]),`;
if the 4 was measured against a different edit, the difference is in the mutation rather
than in the suite. I am not claiming the reviewer's number is wrong, only that mine does
not reproduce and the two cannot both describe this edit.

## M2 — the contradicted sentence, and the real reason

**The stale claim is gone from the report.** D8 asserted that the `""` key could not occur
in the real data, and the round-1 section then said that assertion had been removed while
it was still sitting in D8. D8 now states what is true: the real library has
`ALAC/薄塩指数/!_`, it is indexed under `""`, and it is reachable.

**The rationale is corrected, and it is a different axis than "real albums have names".**
The reason `""` must stay indexed is that **the plan's only empty-key guard is on the track
title** — Task 4's step 1 refuses to skip when `title_key == ""` — and **nothing guards the
album-name side**. So `by_name[""]` is genuinely reachable: for a download whose album
name is punctuation-only, dropping the key would re-fetch an album that is already on
disk. The review confirmed the route is live, with `find_duplicate(album_name="・・・",
track_title="!_ (intro)")` returning a hit on `ALAC/薄塩指数/!_`.

That hit is arguably *correct*: two punctuation-only album names being the same release is
plausible, and nothing in the design argues otherwise. So the decision to keep the key
stands, and the code comment now says so on those grounds rather than on the discredited
one.

### Task 4 follow-up — flagging, not deciding

**Task 4 may want an album-side empty-name guard**, and the choice belongs there, not
here. Concretely: `find_duplicate` has no `if album_key == "": return None` step, and by
omission the `""` group is matchable. The two options are (a) leave it, on the reasoning
above that a punctuation-only album name is a real identity and a match against it is
plausible, or (b) refuse, on the reasoning that `""` carries no more information than the
empty *title* does, and the title side is already refused. The arguments cut both ways and
the real library contains exactly one such album, so either choice is nearly free in
practice. Ruling on it in Task 4.

## M3 — the ordering test now discriminates

The replacement was a no-op resting on a false premise: `"a/A"` is a *prefix* of
`"a/A B"`, so a global sort and per-directory order agree on that pair, and the comment's
"a space beats a slash" was not a claim about the pair the fixture held. Space-beats-slash
only separates a *nested* album directory from a sibling, so the fixture now holds all
three: `a/A/x.m4a`, `a/A/a/x.m4a` and `a/A B/z.m4a`. Comparing `"a/A "` against `"a/A/"`
puts the space first, so a global sort reports `a/A B` before `a/A/a` and the
implementation does not. The test asserts both orderings side by side, which also documents
the difference rather than only the one that happens to hold:

```python
assert first.index("a/A/a") < first.index("a/A B")            # what is guaranteed
assert sorted(first).index("a/A B") < sorted(first).index("a/A/a")  # the other rule
```

Two mutations confirm the test has teeth:

```
$ uv run pytest -q          # sort the finished album list globally instead
FAILED tests/test_library_scan.py::test_album_dirs_are_reported_in_a_stable_per_directory_order
1 failed, 67 passed in 0.12s

$ uv run pytest -q          # drop dirnames.sort(), i.e. guarantee nothing
FAILED tests/test_library_scan.py::test_album_dirs_are_reported_in_a_stable_per_directory_order
1 failed, 67 passed in 0.13s
```

## Small items

- **B1** the ragged wrap in the module docstring ("There is" / "therefore no scan cache…")
  is reflowed into the paragraph it belongs to.
- **B3** the "track_keys are still available to a caller that wants them" comment
  restated a benefit without checking who gets it. It now says what is actually true:
  nothing is lost, because the only route to the root scope before the I1 exclusion was a
  lookup by the mount point's own basename, which is never a download's album name. Those
  loose files were already invisible to dedup; what the exclusion removed was a
  false-positive route, not a working one.
- **B4** corrected an attribution error in the round-1 section: the root's absence from
  `by_name` is asserted in `test_the_root_scope_is_not_indexed_as_an_album_name`, not in
  `test_a_root_that_is_itself_an_album_dir_is_still_a_scope`, which is unchanged.
- **B5** both `scan_roots` and `_as_roots` now annotate `Sequence[Path] | Path | str`, so
  the accepted types are visible to a type checker and not only in prose; and `_relpath`
  returns `_ROOT_SCOPE` instead of a hardcoded `"."`.
- **M5 nits** the timing test's comment no longer attributes 0.06 s to a full scan — it
  now names 0.06 s as the `os.walk` cost and 0.091 s as the `scan_roots` cost.

## Test command and verbatim output

```
$ cd hub && uv run pytest -v
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 68 items

tests/test_config.py::test_requires_password PASSED                      [  1%]
tests/test_config.py::test_requires_password_when_the_variable_is_absent PASSED [  2%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [  4%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [  5%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [  7%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [  8%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [ 10%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [ 11%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 13%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 14%]
tests/test_config.py::test_rejects_a_port_outside_the_bindable_range PASSED [ 16%]
tests/test_library_scan.py::test_finds_album_dirs_at_the_shallowest_level PASSED [ 17%]
tests/test_library_scan.py::test_an_album_dirs_keys_do_not_include_a_subdirectorys_tracks PASSED [ 19%]
tests/test_library_scan.py::test_the_walk_descends_past_an_album_dir_into_its_subdirectories PASSED [ 20%]
tests/test_library_scan.py::test_groups_same_named_album_dirs_across_roots PASSED [ 22%]
tests/test_library_scan.py::test_album_name_index_keeps_leading_digits PASSED [ 23%]
tests/test_library_scan.py::test_album_name_index_keeps_a_number_a_separator_follows PASSED [ 25%]
tests/test_library_scan.py::test_a_track_number_is_still_stripped_from_track_keys PASSED [ 26%]
tests/test_library_scan.py::test_album_name_index_keeps_single_and_deluxe_suffixes PASSED [ 27%]
tests/test_library_scan.py::test_an_album_name_that_normalizes_to_nothing_is_still_indexed PASSED [ 29%]
tests/test_library_scan.py::test_derives_artist_from_structure PASSED    [ 30%]
tests/test_library_scan.py::test_artist_of_a_depth_two_album_dir PASSED  [ 32%]
tests/test_library_scan.py::test_artist_of_a_codec_directory_that_is_not_below_a_codec_directory PASSED [ 33%]
tests/test_library_scan.py::test_artist_is_the_parent_directory_name PASSED [ 35%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts0-None] PASSED [ 36%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts1-None] PASSED [ 38%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts2-TEMPLIME] PASSED [ 39%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts3-TEMPLIME] PASSED [ 41%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts4-EmoCosine] PASSED [ 42%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts5-Atmos] PASSED [ 44%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts6-Atmos] PASSED [ 45%]
tests/test_library_scan.py::test_an_album_with_no_artist_level_reports_the_codec_directory PASSED [ 47%]
tests/test_library_scan.py::test_artist_of_a_dir_whose_parent_is_the_root_is_unknown PASSED [ 48%]
tests/test_library_scan.py::test_a_root_that_is_itself_an_album_dir_is_still_a_scope PASSED [ 50%]
tests/test_library_scan.py::test_the_root_scope_is_not_indexed_as_an_album_name PASSED [ 51%]
tests/test_library_scan.py::test_by_name_is_the_same_however_the_root_is_spelled PASSED [ 52%]
tests/test_library_scan.py::test_is_audio_file_decides_what_is_a_track PASSED [ 54%]
tests/test_library_scan.py::test_part_files_are_not_tracks PASSED        [ 55%]
tests/test_library_scan.py::test_a_directory_of_only_part_files_is_not_an_album_dir PASSED [ 57%]
tests/test_library_scan.py::test_empty_titles_are_representable_but_distinguishable PASSED [ 58%]
tests/test_library_scan.py::test_unreachable_root_is_marked_degraded PASSED [ 60%]
tests/test_library_scan.py::test_every_root_appears_in_order_with_its_own_index PASSED [ 61%]
tests/test_library_scan.py::test_root_that_is_a_symlink_is_accepted PASSED [ 63%]
tests/test_library_scan.py::test_a_walked_path_outside_the_root_raises_instead_of_finding_nothing PASSED [ 64%]
tests/test_library_scan.py::test_a_symlinked_directory_inside_a_root_is_not_followed PASSED [ 66%]
tests/test_library_scan.py::test_repeated_scans_of_one_tree_agree PASSED [ 67%]
tests/test_library_scan.py::test_a_later_change_is_visible_without_a_rescan_hook PASSED [ 69%]
tests/test_library_scan.py::test_no_roots_yields_an_empty_but_valid_scan PASSED [ 70%]
tests/test_library_scan.py::test_a_root_that_is_a_file_is_degraded_not_fatal PASSED [ 72%]
tests/test_library_scan.py::test_scan_is_fast_enough_to_run_per_request PASSED [ 73%]
tests/test_library_scan.py::test_album_dirs_are_reported_in_a_stable_per_directory_order PASSED [ 75%]
tests/test_normalize.py::test_stem_of_drops_only_a_known_audio_extension PASSED [ 76%]
tests/test_normalize.py::test_stem_of_keeps_a_name_whose_dot_is_not_an_extension PASSED [ 77%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 79%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 80%]
tests/test_normalize.py::test_normalize_keeps_a_four_digit_year PASSED   [ 82%]
tests/test_normalize.py::test_normalize_folds_decomposed_and_composed_equally PASSED [ 83%]
tests/test_normalize.py::test_normalize_folds_fullwidth_to_halfwidth PASSED [ 85%]
tests/test_normalize.py::test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title PASSED [ 86%]
tests/test_normalize.py::test_normalize_does_not_swallow_a_title_leading_dot_run PASSED [ 88%]
tests/test_normalize.py::test_audio_exts_membership_table PASSED         [ 89%]
tests/test_normalize.py::test_normalize_keeps_a_separator_with_padding_and_stops_at_two_groups PASSED [ 91%]
tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping PASSED [ 92%]
tests/test_normalize.py::test_is_audio_file_is_case_insensitive PASSED   [ 94%]
tests/test_normalize.py::test_normalize_squeezes_whitespace PASSED       [ 95%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 97%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 98%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 68 passed in 0.12s ==============================
```

(`uv run pytest -q` → `68 passed in 0.11s`. `uvx ruff check` → `All checks passed!`;
`uvx ruff format --diff` → `3 files already formatted`.)

## Real-library re-scan — the numbers did not move

```
direct  0.093s  album_dirs=3670  by_name keys=3431
symlink 0.092s  album_dirs=3670  by_name keys=3431
both    0.119s  album_dirs=4739  by_name keys=3431

albums equal (direct vs symlink) : True
by_name equal                     : True
artist None                       : 10 / 3670
artist None (downloads/)          : 0 / 1069
keys in >1 dir                    : 223 (extra 238)
'' key                            : ['ALAC/薄塩指数/!_']
```

4,739 album dirs across both roots, 3,670 external, 3,431 `by_name` keys, 10 with no
artist, ~0.1 s per scan. One further check, since C1 is about artist coverage: **0** of
the 3,669 non-root album dirs are missing from `by_name[album_key(their own name)]`, and
**0** of the 49 album dirs at depth ≤ 2 are missing a key. The 47/49 figure in an earlier
scratch check of mine was an artifact of that script comparing `name.casefold()` instead
of `album_key(name)`; there is no hole.

`artist None` is unchanged at 10, and 0 on `downloads/` — so the depth-2 coverage argument
the design rests on still holds after adding the wiring tests, which is the point of C1.

---

# Fix round 3

Documentation accuracy only. No behaviour changed: `library_scan.py` is byte-identical to
the round-2 commit, and the artist wiring, the `_artist` simplification, the root-scope
exclusion and the `is_audio_file` call site are all untouched. The one non-documentation
change is a fixture rename (N2), which cannot change what the scanner does on real data
but would have changed what Task 4 inherits.

## N2 — the nested album is no longer its parent's twin

`a/A/a/x.m4a` made `by_name["a"]` group a directory with its own child, both holding track
key `"x"` — a shape no real library produces, in a tree that `conftest.py` explicitly
certifies as reproducing real shapes *for Task 4*, which is the next task and groups by
album name. Renamed to `a/A/CD 1/x.m4a`, so the two are now distinct albums, and the
shared track key `"x"` is the real shape instead: the same title in two different albums,
which is what `intro` does six times over. The M3 ordering test follows the rename, from
`a/A/a` to `a/A/CD 1`.

Verified that the sort discrimination is intact — the whole point of the rename was not to
weaken it:

```
$ uv run pytest -q          # sort the finished album list globally instead
=========================== short test summary info ============================
FAILED tests/test_library_scan.py::test_album_dirs_are_reported_in_a_stable_per_directory_order
1 failed, 67 passed in 0.18s
```

`"a/A "` against `"a/A/"` still puts the space first, so a global sort still reports
`a/A B` before `a/A/CD 1` and still contradicts the implementation.

## N3 — the contract enumeration matches the tree

`conftest.py`'s module docstring listed three contract probes; the tree now holds six
groups. It enumerates all of them with what each pins and whether the shape is observed or
constructed: the two `is_audio_file` boundaries, the `!_` album name, the depth-2 artist
shape, the nested-codec artist shape, the ordering trio, and `extra/4 - Leaves`.

## M2 residue — the sentence is gone, and the grep is clean

Deleted the last surviving instance of the discredited claim. It sat in the round-1
section's real-library re-scan, immediately after the `by_name` invariance numbers, and
contradicted both the corrected D8 and the observation a few lines below it. The paragraph
now says the `""` key **does** match, because the plan's empty-key guard is on the track
title and nothing guards the album-name side.

Proof, run over the whole report after the edits. The phrase is gone from the report
entirely — including from the round-1 narrative, which had quoted it while describing its
own removal, so that a plain grep over this document comes back empty:

```
$ grep -nE "unreachable[[:space:]]in[[:space:]]practice|a[[:space:]]real[[:space:]]album[[:space:]]always[[:space:]]has[[:space:]]a[[:space:]]name|existing[[:space:]]empty-title[[:space:]]guard" \
    .superpowers/sdd/2026-09-26-amd-hub-phase1/task-3-report.md
grep exit: 1 (1 = no matches, which is the pass condition)
```

`[[:space:]]` rather than a literal space so the search cannot match its own command line,
which is the only reason an earlier version of this proof reported a hit on itself.

## N1 — the "untestable mutation" argument was wrong

The report claimed `parts[1:]` "is a no-op at the call site, so no test can catch it and
none should try", and then contradicted itself fifteen lines later by showing a test
catching it. The argument is simply false: `_artist` returns `None` when
`len(parts) < 2` rather than indexing `[-2]`, so the two slices agree only while the
answer is non-`None`. At length 2 the guard bites — `parts[1:]` is length 1 and answers
`None` where `parts` answers `parts[0]` — and length 2 is precisely the depth-2 shape C1
is about. The section now carries the correction and points at the evidence.

I also replaced the truncated mutation listing with the full `short test summary`, which
is what round 2's `tail -4` had cut down to its last line.

**On the `parts[2:]` count.** I measure **1 failed, 67 passed**, not the 4/64 reported,
and I could not reproduce 4 by any route: `parts = tuple(relpath.split("/")[2:])` and
`…[1:]` each fail 16 (they break `name` and every `by_name` lookup, not just the artist),
and `_artist` returning `parts[-3]` fails 10. The edit is stated verbatim in the report
(`hub/hub/library_scan.py:226`) so the two runs can be compared like for like. I am not
asserting the review's number is wrong — only that it does not reproduce here, and the two
cannot both describe this edit.

## Test command and verbatim output

```
$ cd hub && uv run pytest -v
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/amdl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/amdl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 68 items

tests/test_config.py::test_requires_password PASSED                      [  1%]
tests/test_config.py::test_requires_password_when_the_variable_is_absent PASSED [  2%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [  4%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [  5%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [  7%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [  8%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [ 10%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [ 11%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 13%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 14%]
tests/test_config.py::test_rejects_a_port_outside_the_bindable_range PASSED [ 16%]
tests/test_library_scan.py::test_finds_album_dirs_at_the_shallowest_level PASSED [ 17%]
tests/test_library_scan.py::test_an_album_dirs_keys_do_not_include_a_subdirectorys_tracks PASSED [ 19%]
tests/test_library_scan.py::test_the_walk_descends_past_an_album_dir_into_its_subdirectories PASSED [ 20%]
tests/test_library_scan.py::test_groups_same_named_album_dirs_across_roots PASSED [ 22%]
tests/test_library_scan.py::test_album_name_index_keeps_leading_digits PASSED [ 23%]
tests/test_library_scan.py::test_album_name_index_keeps_a_number_a_separator_follows PASSED [ 25%]
tests/test_library_scan.py::test_a_track_number_is_still_stripped_from_track_keys PASSED [ 26%]
tests/test_library_scan.py::test_album_name_index_keeps_single_and_deluxe_suffixes PASSED [ 27%]
tests/test_library_scan.py::test_an_album_name_that_normalizes_to_nothing_is_still_indexed PASSED [ 29%]
tests/test_library_scan.py::test_derives_artist_from_structure PASSED    [ 30%]
tests/test_library_scan.py::test_artist_of_a_depth_two_album_dir PASSED  [ 32%]
tests/test_library_scan.py::test_artist_of_a_codec_directory_that_is_not_below_a_codec_directory PASSED [ 33%]
tests/test_library_scan.py::test_artist_is_the_parent_directory_name PASSED [ 35%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts0-None] PASSED [ 36%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts1-None] PASSED [ 38%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts2-TEMPLIME] PASSED [ 39%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts3-TEMPLIME] PASSED [ 41%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts4-EmoCosine] PASSED [ 42%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts5-Atmos] PASSED [ 44%]
tests/test_library_scan.py::test_artist_is_the_parent_name_and_nothing_else[parts6-Atmos] PASSED [ 45%]
tests/test_library_scan.py::test_an_album_with_no_artist_level_reports_the_codec_directory PASSED [ 47%]
tests/test_library_scan.py::test_artist_of_a_dir_whose_parent_is_the_root_is_unknown PASSED [ 48%]
tests/test_library_scan.py::test_a_root_that_is_itself_an_album_dir_is_still_a_scope PASSED [ 50%]
tests/test_library_scan.py::test_the_root_scope_is_not_indexed_as_an_album_name PASSED [ 51%]
tests/test_library_scan.py::test_by_name_is_the_same_however_the_root_is_spelled PASSED [ 52%]
tests/test_library_scan.py::test_is_audio_file_decides_what_is_a_track PASSED [ 54%]
tests/test_library_scan.py::test_part_files_are_not_tracks PASSED        [ 55%]
tests/test_library_scan.py::test_a_directory_of_only_part_files_is_not_an_album_dir PASSED [ 57%]
tests/test_library_scan.py::test_empty_titles_are_representable_but_distinguishable PASSED [ 58%]
tests/test_library_scan.py::test_unreachable_root_is_marked_degraded PASSED [ 60%]
tests/test_library_scan.py::test_every_root_appears_in_order_with_its_own_index PASSED [ 61%]
tests/test_library_scan.py::test_root_that_is_a_symlink_is_accepted PASSED [ 63%]
tests/test_library_scan.py::test_a_walked_path_outside_the_root_raises_instead_of_finding_nothing PASSED [ 64%]
tests/test_library_scan.py::test_a_symlinked_directory_inside_a_root_is_not_followed PASSED [ 66%]
tests/test_library_scan.py::test_repeated_scans_of_one_tree_agree PASSED [ 67%]
tests/test_library_scan.py::test_a_later_change_is_visible_without_a_rescan_hook PASSED [ 69%]
tests/test_library_scan.py::test_no_roots_yields_an_empty_but_valid_scan PASSED [ 70%]
tests/test_library_scan.py::test_a_root_that_is_a_file_is_degraded_not_fatal PASSED [ 72%]
tests/test_library_scan.py::test_scan_is_fast_enough_to_run_per_request PASSED [ 73%]
tests/test_library_scan.py::test_album_dirs_are_reported_in_a_stable_per_directory_order PASSED [ 75%]
tests/test_normalize.py::test_stem_of_drops_only_a_known_audio_extension PASSED [ 76%]
tests/test_normalize.py::test_stem_of_keeps_a_name_whose_dot_is_not_an_extension PASSED [ 77%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 79%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 80%]
tests/test_normalize.py::test_normalize_keeps_a_four_digit_year PASSED   [ 82%]
tests/test_normalize.py::test_normalize_folds_decomposed_and_composed_equally PASSED [ 83%]
tests/test_normalize.py::test_normalize_folds_fullwidth_to_halfwidth PASSED [ 85%]
tests/test_normalize.py::test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title PASSED [ 86%]
tests/test_normalize.py::test_normalize_does_not_swallow_a_title_leading_dot_run PASSED [ 88%]
tests/test_normalize.py::test_audio_exts_membership_table PASSED         [ 89%]
tests/test_normalize.py::test_normalize_keeps_a_separator_with_padding_and_stops_at_two_groups PASSED [ 91%]
tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping PASSED [ 92%]
tests/test_normalize.py::test_is_audio_file_is_case_insensitive PASSED   [ 94%]
tests/test_normalize.py::test_normalize_squeezes_whitespace PASSED       [ 95%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 97%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 98%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 68 passed in 0.14s ==============================
```

(`uv run pytest -q` → `68 passed in 0.12s`. `uvx ruff check` → `All checks passed!`;
`uvx ruff format --diff` → `3 files already formatted`.)

## Real-library baseline — unmoved

```
direct  0.116s  album_dirs=3670  by_name keys=3431
symlink 0.116s  album_dirs=3670  by_name keys=3431
both    0.158s  album_dirs=4739  by_name keys=3431

albums equal (direct vs symlink) : True
by_name equal                     : True
artist None                       : 10 / 3670
artist None (downloads/)          : 0 / 1069
keys in >1 dir                    : 223 (extra 238)
'' key                            : ['ALAC/薄塩指数/!_']
```

4,739 album dirs across both roots, 3,670 external, 3,431 `by_name` keys, 10 with no
artist, 0 on `downloads/`, 0.12–0.16 s per scan. (The timings sit above round 2's
0.093/0.119 s because the drive was being read concurrently; the counts are the thing that
must not move, and they did not.)
