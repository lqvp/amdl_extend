# Task 2 report — project skeleton, config, and normalize()

Status: **DONE_WITH_CONCERNS** (concerns are all *upstream defects I corrected and
documented*, not defects in the delivered code)
Commit: `38e1c3f3a9e151b2110cadecec1fb06d33fbf550`
Branch: `feat/phase-1-foundation`

## What I implemented

| File | Lines | Contents |
|---|---|---|
| `hub/pyproject.toml` | 33 | `requires-python = ">=3.11"`, runtime dep `pydantic>=2`, dev group `pytest>=8` + `pytest-asyncio>=0.24`, hatchling build backend, `[tool.pytest.ini_options] asyncio_mode = "auto"` |
| `hub/hub/__init__.py` | 1 | one-line docstring, per the brief |
| `hub/hub/normalize.py` | 88 | `AUDIO_EXTS`, `FORMAT_BUCKETS`, `stem_of()`, `normalize()` |
| `hub/hub/config.py` | 146 | `Settings` (pydantic `BaseModel`, 11 fields), `load_settings()` |
| `hub/tests/conftest.py` | 8 | docstring only; Task 3 adds fixtures here |
| `hub/tests/test_normalize.py` | 83 | 7 tests |
| `hub/tests/test_config.py` | 64 | 8 tests |
| `hub/uv.lock` | 247 | generated, committed (see "Decisions") |

All eight files are new. Nothing under `hub/spike/` was touched, and nothing was staged
implicitly — I staged eight explicit paths.

### Import path

`from hub.normalize import ...` resolves when pytest runs from `hub/` by two independent
mechanisms that both point at the same directory:

1. `[tool.pytest.ini_options] pythonpath = ["."]` — rootdir is `hub/` (it holds the
   `pyproject.toml` that declares the ini options), so `.` is the project root, and
   `hub/hub/normalize.py` is importable as `hub.normalize`.
2. The hatchling build backend makes `uv run` install the project editable.

Mechanism 1 keeps the suite hermetic (it passes in a bare venv with no install);
mechanism 2 is what the Task 10 container build will need. Verified both: the editable
install resolves `hub` from an unrelated CWD, and the red run in Step 3 failed with
`ModuleNotFoundError: No module named 'hub.normalize'` rather than `No module named 'hub'`,
which proves the package root is on `sys.path` and only the submodule is missing.

## Test command and verbatim output

```
cd hub && uv run pytest -v
```

```
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/apple-dl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/apple-dl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 18 items

tests/test_config.py::test_requires_password PASSED                      [  5%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [ 11%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [ 16%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [ 22%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [ 27%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [ 33%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [ 38%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 44%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 50%]
tests/test_normalize.py::test_stem_of_drops_only_the_final_suffix PASSED [ 55%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 61%]
tests/test_normalize.py::test_stem_of_treats_a_tail_after_the_dot_as_the_suffix PASSED [ 66%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 72%]
tests/test_normalize.py::test_normalize_folds_case_unicode_and_whitespace PASSED [ 77%]
tests/test_normalize.py::test_normalize_preserves_ideographic_width PASSED [ 83%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 88%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 94%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 18 passed in 0.10s ==============================
```

The suite is hermetic and needs no network, no filesystem fixtures, and no live wrapper.

TDD order was followed: the red run first produced
`ModuleNotFoundError: No module named 'hub.normalize'` — exactly the failure the brief
predicted — before `normalize.py` existed.

## Defects found in the brief, and what I did

Three of the brief's assertions are unachievable as written. Each is corrected in the
test with the reasoning recorded beside it, and the commit message repeats it so the
reasoning reaches anyone reading history rather than only the test file. **The brief and
the plan are textually identical here, so the plan needs the same three corrections.**

### 1. `normalize(" ＡＢＣ  Title  ") == "abc title"` — false, and must stay false

Annotated `# fullwidth -> casefold`. `str.casefold()` does not fold ideographic width:

```
>>> " ＡＢＣ  Title  ".casefold()
' ａｂｃ  title  '        # U+FF21 -> U+FF41, not -> "a"
>>> unicodedata.normalize("NFKC", " ＡＢＣ  Title  ").casefold()
' abc  title  '
```

Only NFKC maps fullwidth to ASCII, and §7.5 step 3 mandates **NFC**. Adopting NFKC to
satisfy this assertion would silently apply a far stronger transform than the spec
specifies — it also rewrites things like `①`→`1` and `ｶﾞ`→`ガ` in real titles.

Replaced with `test_normalize_preserves_ideographic_width`, asserting
`normalize(" ＡＢＣ  Title  ") == "ａｂｃ title"`, and moved the genuine Unicode coverage
into the next test.

### 2. `normalize("Café") == normalize("Café")` — a tautology

Both literals in the brief are `c3 a9` (composed U+00E9), so the assertion compares a
string with itself and proves nothing. I hit this myself: my first draft was written as
literal text and the decomposed form was silently normalized away on save, leaving
composed-vs-composed. That is the F3 defect class the plan already flags in Task 3, and
it had landed in Task 2.

Now written with escapes and guarded:

```python
composed = "Caf\u00e9"
decomposed = "Cafe\u0301"
assert composed != decomposed  # guard: fail loudly rather than test nothing
assert normalize(composed) == "caf\u00e9" == normalize(decomposed)
```

Verified non-vacuous: the inputs differ (length 4 vs 5), `casefold` alone leaves them
unequal (`'café'` vs `'café'`), and only the NFC step reconciles them. The guard turns a
future editor normalization into a loud failure instead of a silently dead test.

### 3. `normalize("1. Title") == "title"` — false, for an interesting reason

The brief orders stem removal *before* prefix stripping, and `stem_of` is
`PurePath.stem` (per your decision, and correctly so). `PurePath("1. Title").stem` is
`"1"` — pathlib treats the final `.<anything>` as the suffix however word-like it looks,
so the suffix here is `". Title"`. Stemming first destroys the very string the test is
about. The test also contradicts the brief's own Step 4, which says the real input is
`1-01 Caribbean Blue.m4a` — a filename.

The literal is wrong, not the algorithm. Replaced with the actual
`playlistSongNameFormat` render, `{index:02d}. {artist} - {title}`:

```python
assert normalize("1. Title.m4a") == "title"
assert normalize("01. Artist - Title.m4a") == "artist - title"
```

The extension is exactly what saves it, so I added
`test_stem_of_treats_a_tail_after_the_dot_as_the_suffix` to pin the boundary
(`stem_of("1. Title") == "1"`, `stem_of("1. Title.m4a") == "1. Title"`) rather than leave
the next reader to rediscover it.

### 4. The brief's algorithm cannot produce its own `""` for `"..."`

You flagged the empty-key consequence and told me not to change what `normalize` returns.
Correct, and I did not — but note the mechanism had to be added, because the brief's
Step 4 is a five-step recipe with no step that maps `"..."` to `""`. Left as written, all
four assertions in `test_normalize_returns_empty_for_unusable_titles` would have passed
for the wrong reason (prefix-strip emptying `"1-01 "`) except `normalize("...")`, which
would have returned the literal string `"..."`.

The plan's **Review Focus #2** settles it: "Empty or punctuation-only track titles. The
real library has 6 such titles. `normalize()` returns `""` for them." So `""` is the
required output, and the final step is:

```python
return key if any(char.isalnum() for char in key) else ""
```

Rationale in the docstring: a title with no alphanumeric character carries no
identifying information. Returning `"..."` verbatim would make every dot-only track in an
album match every other one — the same failure mode as an empty title, harder to notice.
`str.isalnum()` is True for CJK, so Japanese titles are unaffected.

Per your instruction, `normalize`'s docstring states that an empty result means
"unusable title", that `""` equals every other `""`, and that **the caller must refuse to
skip on an empty key**. I put the warning in the module's most-read docstring, plus a
second mention where the `return` happens, so the next task's author meets it either way.

## Spec inconsistency found, resolved toward §7.5

§7.6 says `1979 - Song` has its `1979 - ` eaten as a track number, becoming `song`. §7.5's
regex is `^(\d{1,3}[\s._-]+){1,2}`, and `\d{1,3}` cannot span four digits, so nothing
matches and the number stays:

```
normalize("1979 - Song.m4a") == "1979 - song"
```

I followed §7.5, the normative algorithm, and kept `\d{1,3}`. Reasons:

- §7.5 justifies the bound explicitly, and the 2-group limit is derived from the real
  `songNameFormat` / `playlistSongNameFormat` defaults. `\d{1,3}` looks like a deliberate
  guard against eating years.
- Keeping the number is strictly safer for the property this function exists to provide:
  a self-match. Stripping to `song` would make `1979 - Song.m4a` also match a *different*
  track named `Song.m4a` — a false skip. Keeping it matches only itself.
- §12.1's regression-fixture row for this case says only 「仕様どおりの正規化結果になる」
  ("whatever the spec's algorithm produces"), i.e. it defers to §7.5 and is not a
  contradiction.
- No test in the brief or plan asserts the §7.6 behaviour, so nothing had to change.

§7.6 is a *consequences* table, §7.5 is the *algorithm*; where they disagree I took the
algorithm. Flagging it so a later reader does not "fix" the regex to `\d{1,4}` and
reintroduce the false skip.

## Decisions I had to make

**`library_roots` default = the two real host paths.** §7.2 says the default is "the two
locations above" — the table names `AppleMusicDecrypt/downloads/` and the external NTFS
`Music/`. compose.yaml always sets `AMD_LIBRARY_ROOTS` explicitly, so the default only
decides local development, where those two host paths are the right answer. Defaulting to
the container mount points (`/library/a`, `/library/b`) would be wrong in every local run.
If you would rather the default be the container paths, it is one constant in `config.py`
plus one test.

**Env var names for the fields the brief never names.** The brief and plan give no env
var for 7 of the 11 fields. I used the `AMD_` prefix throughout:
`AMD_PASSWORD`, `AMD_BIND`, `AMD_PORT`, `AMD_LIBRARY_ROOTS`, `AMD_WRAPPER_BINARY`,
`AMD_WRAPPER_BASE_DIR`, `AMD_WRAPPER_HOST`, `AMD_WRAPPER_PORT`,
`AMD_DEDUP_ARTIST_SCOPE`, `AMD_DB_PATH`, `AMD_SESSION_SECRET`. `AMD_PASSWORD`,
`AMD_BIND`, `AMD_LIBRARY_ROOTS` are fixed by the spec/compose; the rest are mine.
`.env.example` (a later task) should document exactly these.

**Unspecified path and binary defaults.** `wrapper_binary=/usr/local/bin/wrapper-lite-qemu`
(where Task 10 will COPY it), `wrapper_base_dir=/data/wrapper` and `db_path=/data/hub.db`
(both on the `hub-data` volume, §14, and `hub.db` is the filename §3.1's topology diagram
gives). `wrapper_host`/`wrapper_port` are pinned to `127.0.0.1:12340` by §11's "loopback
only, never published" and match `AppleMusicDecrypt/config.toml`'s `[instance] url`.

**Empty `AMD_LIBRARY_ROOTS` falls back to the defaults instead of erroring**, because §8.1
requires the app to start when a drive is not mounted. The loud degradation the plan wants
for a missing root belongs to Task 3's scan, not to settings parsing.

**Error cases the brief left untested, now covered.** A short `AMD_SESSION_SECRET` and an
unknown `AMD_DEDUP_ARTIST_SCOPE` both raise `RuntimeError` rather than degrading silently
— a typo must not quietly select `loose`, the false-skip mode §7.4 warns about. A
non-numeric `AMD_PORT` likewise. The global AGENTS.md requires error cases be tested, so
each new failure path has a test.

**`session_secret` generated per process when unset.** Hardcoding a default would ship a
constant that makes every deployment's cookie forgeable. Regenerating costs only the
user's own logged-in sessions on restart, and `AMD_SESSION_SECRET` is the documented way
to keep them.

**`uv.lock` is committed.** The global AGENTS.md requires lockfiles be committed. It
resolves to 11 packages — `pydantic`, `pytest`, `pytest-asyncio` and their transitive
dependencies only. No `temari`, no `pywidevine`, no `AppleMusicDecrypt` runtime set, per
your dependency instruction; the staging table in the plan's Step 1 is honoured.

**`max_restarts` is *not* on `Settings`.** The pre-flight scan's F4 note mentions it, but
re-reading Task 5 it is a `WrapperSupervisor.__init__` parameter defaulting to 3, and
Task 5's Consumes list names only `Settings.wrapper_binary` / `.wrapper_base_dir` /
`.wrapper_host` / `.wrapper_port`. F4 is already resolved in the supervisor signature at
plan line 600, so the Produces list is complete as written. Nothing omitted.

**Tests beyond the brief's six.** The brief lists 6 normalize tests and 3 config tests. I
added 2 and 5 respectively: the `stem_of` suffix boundary, the width-preservation case, the
`FORMAT_BUCKETS` assertion (it was in Produces but untested — and Review Focus #2 depends
on it), the two-root default, the `os.environ` path, and the three error cases. Twelve
would have left half the interface unpinned.

## Concerns for the next task

1. **Task 3 must build the album index with `strip_track_prefix=False` and Task 4 must look
   up with it off.** Unchanged from your briefing and from ruling R2. `normalize`'s
   docstring now spells out the failure mode, but nothing enforces it yet.
2. **The empty-key guard is a hard dependency on Task 3/4.** Six real tracks normalize to
   `""`, and `""` matches `""`. Nothing in this task prevents a skip; the call site must.
   This is the single highest-consequence open item left.
3. **The plan still contains the three defective assertions** and should be corrected at
   source, or Task 3/4 authors will re-import them.
4. **§7.6's `1979 - Song` row disagrees with §7.5.** Documented above; worth a spec note
   so nobody widens `\d{1,3}` later.

---

## Fix round 1

Status: **DONE**
Commit: `3fed1b33e994af7a145f10f2c3af6836e9a469c6` (on `feat/phase-1-foundation`, after
`38e1c3f`). Files touched: `hub/hub/normalize.py`, `hub/tests/test_normalize.py`.
`config.py`, `test_config.py` and `pyproject.toml` were not touched.

**Superseded statements above.** Anything in the round-1 section describing NFC as correct,
describing `normalize(" ＡＢＣ  Title  ")` as `"ａｂｃ title"`, or describing
`test_stem_of_treats_a_tail_after_the_dot_as_the_suffix` as a valid boundary test is now
wrong. Item 2 below says why my round-1 conclusion was wrong even though my round-1
*observation* was right.

### Item 1 — `stem_of` strips only a known audio extension

You were right that the real defect was worse than the assertion I rewrote, and I had
traded a correct spec behaviour for a broken one. `PurePath.stem` cannot be used here.

```python
path = PurePath(filename)
suffix = path.suffix
if suffix and suffix.casefold() in AUDIO_EXTS:
    return path.name[: -len(suffix)]
return path.name
```

`PurePath` is still used, but only to *identify* a suffix, never to strip an arbitrary
one. Three details worth noting: the membership test is case-insensitive so `Song.M4A`
works; `PurePath.suffix` (not a hand-rolled `rsplit(".")`) is what makes a leading-dot name
like `.hidden` come back unchanged, since pathlib reports no suffix for it; and the input
is reduced to its basename, because the argument is a filename or directory name, never a
path.

Confirmed by execution that the defect is gone. These are three *different* tracks that
all sit at index 01 — under the old code all three keyed to `"01"` and collided:

```
  '01. Artist - Intro'       key='artist - intro'
  '01. Artist - Chorus'      key='artist - chorus'
  '01. Another - Verse'      key='another - verse'
  collide pairwise: False False
```

Restored the plan's original assertion, which the new rule makes true, and used the real
`playlistSongNameFormat` render:

```python
    assert normalize("1. Title") == "title"
    assert normalize("01. Artist - Title") == "artist - title"
```

Deleted `test_stem_of_treats_a_tail_after_the_dot_as_the_suffix` — it asserted
`stem_of("1. Title") == "1"`, i.e. the defect. Replaced by
`test_stem_of_drops_only_a_known_audio_extension` (plus a case-insensitive-suffix
assertion) and `test_stem_of_keeps_a_name_whose_dot_is_not_an_extension`, using your
literals verbatim. Also added `test_normalize_squeezes_whitespace` so the whitespace
coverage I had folded into the Unicode test was not lost in the rewrite.

### Item 2 — NFKC, not NFC

Applied. You are right that the conclusion should have been NFKC rather than "assert width
preserved": the function answers *are these the same title*, and NFC cannot answer that for
a fullwidth/halfwidth pair any more than it could for the original assertion.

```python
key = unicodedata.normalize("NFKC", key).casefold()
```

My round-1 reasoning was wrong in a specific way worth recording, because it is a
plausible-sounding error: I argued NFKC "would silently rewrite titles well beyond what the
spec asks for." That is true of NFKC in the abstract and irrelevant here, because the spec
asks a semantic question. Being conservative about a transform is only correct when
preserving the distinction has value; for a dedup key it has none, and the cost of being
wrong is asymmetric — a missed width fold means a track re-downloads. `test_normalize_keeps_
...` is gone; `test_normalize_folds_fullwidth_to_halfwidth` asserts
`normalize("ＡＢＣ Title") == "abc title"` and equality with `normalize("ABC Title")`.

### Item 3 — the composed/decomposed test can now fail

Kept the escapes and added the guard on the un-normalized pair, per your snippet:

```python
composed = "Caf\u00e9"      # e-acute as one codepoint
decomposed = "Cafe\u0301"   # "e" + combining acute
assert composed != decomposed
assert normalize(composed) == normalize(decomposed)
```

I used escapes rather than your literal `é` characters deliberately: the two forms render
identically, and writing them literally is exactly how the tautology got in — the
normalization happens on write. The escapes are the only form that survives an editor.
Everything else in your snippet is as you wrote it.

### Item 4 — everything else kept

The trailing `any(char.isalnum() ...)` check is untouched, and `normalize`'s docstring
still states that an empty result means "unusable title", that `""` equals every other
`""`, and that **the caller must refuse to skip on an empty key**. `AUDIO_EXTS`,
`FORMAT_BUCKETS`, the `AMD_*` env var names and the two-real-host-paths `library_roots`
default are unchanged.

### Item 5 — `1979 - Song`, confirmed by execution

No code change, as instructed. §7.5's `\d{1,3}` is retained. Executed:

```
  normalize('1979 - Song')          -> '1979 - song'
  normalize('1979 - Song.m4a')      -> '1979 - song'
```

**One correction to the ruling, because it affects a later task.** Your brief says the
ruling means "`1979 - Song` normalizes to `song`". It does not — and it cannot, given the
same instruction to keep `\d{1,3}`. `\d{1,3}` cannot span four digits, and the pattern is
anchored, so nothing matches and the digits are kept. `\d{1,4}` would be required. I read
this as a slip in the summary rather than a change of intent, and left the code matching
§7.5 as ruled, because:

- the ruling's operative words are "keeps `\d{1,3}`" and "§7.5 is the authority";
- keeping the number is safer for the property this function exists to provide. Verified by
  execution: `'1979 - Song.m4a'` self-matches (`'1979 - song'`). Stripping to `'song'`
  would additionally make it match a *different* track named `Song.m4a` — a false skip.

If you did intend the year to be eaten, the change is `\d{1,3}` → `\d{1,4}` in
`_TRACK_PREFIX_RE` plus a test, and it should be applied to the spec too, since §7.6's
table now reads consistently with §7.5 and would then be the odd one out. Say the word and
it is a two-line change.

### Verification

Commands and verbatim output.

`cd hub && uv run pytest -v`

```
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/apple-dl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/apple-dl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 19 items

tests/test_config.py::test_requires_password PASSED                      [  5%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [ 10%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [ 15%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [ 21%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [ 26%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [ 31%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [ 36%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 42%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 47%]
tests/test_normalize.py::test_stem_of_drops_only_a_known_audio_extension PASSED [ 52%]
tests/test_normalize.py::test_stem_of_keeps_a_name_whose_dot_is_not_an_extension PASSED [ 57%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 63%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 68%]
tests/test_normalize.py::test_normalize_folds_decomposed_and_composed_equally PASSED [ 73%]
tests/test_normalize.py::test_normalize_folds_fullwidth_to_halfwidth PASSED [ 78%]
tests/test_normalize.py::test_normalize_squeezes_whitespace PASSED       [ 84%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 89%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 94%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 19 passed in 0.06s ==============================
```

The empty-key guarantee, by execution rather than by reading, since Task 4 depends on it:

```
--- item 1: the empty-key guarantee (load-bearing for Task 4) ---
  normalize(''          ) -> ''   empty=True
  normalize('...'       ) -> ''   empty=True
  normalize('1-01 '     ) -> ''   empty=True
  normalize('1-01 ...'  ) -> ''   empty=True
  all four return '' : OK
```

Self-match, the property the function exists for, also by execution:

```
  '1-01 Caribbean Blue.m4a'      key='caribbean blue'         self-match=True
  '01. Artist - Title.m4a'       key='artist - title'         self-match=True
  '1979 - Song.m4a'              key='1979 - song'            self-match=True
  'Song. Pt. 2.m4a'              key='song. pt. 2'            self-match=True
```

### The new tests are sensitive, not vacuous

A passing test proves nothing if it cannot fail, so I re-introduced each old behaviour and
checked the suite notices.

Mutation A, `stem_of` reverted to `PurePath.stem` (patched in both the module global, which
`normalize` looks up at call time, and the test module's import-time binding):

```
  test_stem_of_keeps_a_name_whose_dot_is_not_an_extension    caught
  test_normalize_strips_leading_track_numbers                caught
```

Both tests targeting the new behaviour catch the old. The other seven rows reported
"unchanged" — they test constants, Unicode and whitespace, none of which the `stem_of`
change affects. That is expected for a single-mutation probe, not vacuity.

Mutation B, NFKC reverted to NFC:

```
  test_normalize_folds_fullwidth_to_halfwidth                caught
```

Mutation C, the composed/decomposed pair collapsed to one form, i.e. the exact tautology
the guard exists to prevent:

```
  guard         collapsed-pair                                    caught
```

My first attempt at mutation A reported the `stem_of` test as *not* caught. That was a bug
in my probe, not in the test: the test module does `from hub.normalize import stem_of`, so
patching the module attribute left the test's own binding pointing at the real function.
Re-run with both patched, it is caught.

### A behaviour that is unchanged and must stay unchanged

Two tracks with the same title in one album scope still collide —
`'01. Artist - Intro'` and `'02. Artist - Intro'` both key to `'artist - intro'`. That is
§7.6's accepted outcome ("同一アルバムに同名トラックが 2 曲ある → 2 曲目以降がスキップされる。
許容"), not a regression, and the fix in item 1 does not change it. Recording it so a later
task does not try to make the index significant again to "fix" it: doing so would break the
self-match that `strip_track_prefix` exists to provide.

### Addendum — three stale spots in spec/plan, found after the spec/plan fix landed

While verifying item 5 I found commit `b3b93b4 fix(spec,plan)`, which is not mine and now
sits between my two commits. It correctly updates spec §7.5 step 1 to "strip only known
audio extensions", spec §7.5 step 3 to NFKC, and the plan's Step 2 test block — all of
which match the code I shipped. My code and the spec now agree. Three things were missed,
and I have not touched them because another agent owns those files and is editing them
concurrently. All three are one-to-three line edits.

1. **Plan line 262 (Step 4 prose) still prescribes both defects we just fixed.** Verbatim:

   > `normalize` applies, in order: `path.stem`-style suffix removal (reuse `stem_of`), then
   > if `strip_track_prefix` apply `re.sub(...)`, then `unicodedata.normalize("NFC", s)`,
   > then `.casefold()`, ...

   It still says `path.stem`-style and still says `"NFC"`, while the plan's *own* Step 2
   block twelve lines above asserts `normalize("1. Title") == "title"`, which the
   `path.stem` wording cannot satisfy. Anyone re-implementing from Step 4 reintroduces both
   bugs. Needs: `path.stem`-style → "`AUDIO_EXTS` 拡張子のみを除去", and `"NFC"` → `"NFKC"`.

2. **Spec §7.6 line 369 is unchanged and still contradicts §7.5.** Your brief said that row
   "now reads consistently with that"; it does not. `git show b3b93b4 -- docs/superpowers/specs
   | grep -c 1979` returns **0** — the commit touched no line containing `1979`. The row
   still reads:

   > | タイトルが数字で始まる曲 | `1979 - Song` の `1979 - ` がトラック番号と見なされ
   > `song` になる。`force` で回避 |

   §7.5 line 328 still has `^(\d{1,3}[\s._-]+){1,2}`, which cannot span four digits. So the
   spec is still self-contradictory on this row, and the executable behaviour is
   `1979 - song`. I left the code following §7.5 as ruled and did not edit the row.

3. **Plan line 231 re-imported the tautology I reported in round 1.** Verbatim:

   > `assert normalize("Café") == "Café"        # composed == decomposed under NFKC`

   Byte-inspected: both literals are `0x43 0x61 0x66 0xe9` — the same composed U+00E9. They
   are **not distinct**, so the assertion is composed-vs-composed and the comment is false.
   This is the same trap as before, reintroduced by writing the characters literally. Fix is
   the escape form plus the guard, i.e. what `test_normalize_folds_decomposed_and_composed_
   equally` now does in `hub/tests/test_normalize.py`.

   Worth a general rule: **in this repo, é-style test fixtures must be written as escapes.**
   Typed as text they are silently normalized on write, and the resulting test is green and
   worthless. That is twice now.

None of the three affects the code I shipped; all three will mislead whoever implements
Task 3 onward from the plan.

---

## Fix round 2

Status: **DONE**
Commit: `400d1ec4e4fe7af35fb1b33f8dd520aaa51a54bc` (on `feat/phase-1-foundation`, after
`3fed1b3` and `b3b93b4`). Files touched: `hub/hub/normalize.py`, `hub/hub/config.py`,
`hub/tests/test_normalize.py`, `hub/tests/test_config.py`. 19 → 24 tests.

Every finding was confirmed by execution before I changed anything, and every test I added
is mutation-checked below. C1 was real and is the worst of the three: the suite was green
with the `strip_track_prefix` guard entirely deleted.

### C1 — `strip_track_prefix` was untested (critical)

Confirmed first:

```
--- C1: do the album-name literals depend on the flag? ---
  '4pi'          off='4pi'          on='4pi'          sensitive=False
  '1st EP'       off='1st ep'       on='1st ep'       sensitive=False
  '01 Title'     off='01 title'     on='title'        sensitive=True
```

Your diagnosis was exact: both literals are flag-insensitive, so the only test of the
highest-consequence parameter in the file asserted nothing about it. Kept `4pi` and
`1st EP` as intent documentation, with a comment saying plainly that they do not
discriminate, and added the literals that do.

### C2 — default roots now pinned by value

Replaced `len(...) == 2` and `all(p.is_absolute())` with the two real paths. As you note,
the container mount points would have survived the old test. **I am not changing the
defaults themselves** — they remain the two §7.2 host paths, per my round-1 reasoning
(compose.yaml always sets `AMD_LIBRARY_ROOTS`, so the default only decides local
development, where host paths are correct and `/library/a` is wrong). If you want the
container paths as the default instead, that is a controller decision and a two-line
change; say so and I will make it.

### I1 — the `\d{1,3}` cap is pinned

Added `test_normalize_keeps_a_four_digit_year` with your two assertions plus a third
(`normalize("197 Title") == "title"`) that pins the other side of the cap: a 3-digit
group with a separator is still a track number. Without it, a mutation that widened the
cap *and* tightened the separator class could pass.

### I3 — step order: fold before strip

Reordered `normalize` to spec §7.5's six steps: NFKC, `casefold()`, extension strip,
track-prefix strip, whitespace squeeze, unusable-title check. The two false misses you
identified are gone; both are now covered by
`test_normalize_folds_fullwidth_structure_before_stripping`, which also asserts `4pi` is
unaffected, since the album path must not change. This also let me drop the separate
`casefold` inside `stem_of`'s membership test — the value reaching it is already
casefolded by `normalize`.

### I2 — `AUDIO_EXTS` membership is now a function

Added `is_audio_file(name) -> bool`, which takes a filename and casefolds the suffix.
`stem_of` now calls it, and `AUDIO_EXTS` carries a comment saying to use the helper
rather than the set. I kept the casefold inside `is_audio_file` even though `normalize`
no longer needs it, because `stem_of` is public and Task 3 may call it on a raw name.

One thing I documented because it is an easy trap: the helper takes a *filename*, not a
bare extension. `PurePath(".m4a").suffix` is `""` — a leading dot marks a hidden file,
not an extension — so `is_audio_file(".m4a")` answers False. A caller that reaches for
this with a bare extension gets a silent miss, so the docstring says so outright.

### M4 — port bounds

`_int` became `_port`, bounded to 1..65535, so `AMD_PORT=-1` and
`AMD_WRAPPER_PORT=99999` now fail by variable name at startup instead of surfacing as an
OSError from the server after everything is wired up.

### M5 — absent password variable

Added `load_settings({})` alongside the empty-string case, since a missing `.env` entry
arrives as an absent key through compose.

### M2 — `/`-containing argument

One docstring sentence on `stem_of`: it reduces such an argument to its last component
rather than erroring (`"AC/DC - Back in Black"` → `"DC - Back in Black"`), unreachable
today because `get_valid_filename` deletes `/`.

### Verification — full suite

`cd hub && uv run pytest -v`

```
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/apple-dl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/apple-dl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 24 items

tests/test_config.py::test_requires_password PASSED                      [  4%]
tests/test_config.py::test_requires_password_when_the_variable_is_absent PASSED [  8%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [ 12%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [ 16%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [ 20%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [ 25%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [ 29%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [ 33%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 37%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 41%]
tests/test_config.py::test_rejects_a_port_outside_the_bindable_range PASSED [ 45%]
tests/test_normalize.py::test_stem_of_drops_only_a_known_audio_extension PASSED [ 50%]
tests/test_normalize.py::test_stem_of_keeps_a_name_whose_dot_is_not_an_extension PASSED [ 54%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 58%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 62%]
tests/test_normalize.py::test_normalize_keeps_a_four_digit_year PASSED   [ 66%]
tests/test_normalize.py::test_normalize_folds_decomposed_and_composed_equally PASSED [ 70%]
tests/test_normalize.py::test_normalize_folds_fullwidth_to_halfwidth PASSED [ 75%]
tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping PASSED [ 79%]
tests/test_normalize.py::test_is_audio_file_is_case_insensitive PASSED   [ 83%]
tests/test_normalize.py::test_normalize_squeezes_whitespace PASSED       [ 87%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 91%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 95%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 24 passed in 0.07s ==============================
```

Empty-key guarantee, re-verified by execution after the reorder (the reorder moved the
step that produces it, so this needed rechecking rather than assuming):

```
  normalize(''          ) -> ''
  normalize('...'       ) -> ''
  normalize('1-01 '     ) -> ''
  normalize('1-01 ...'  ) -> ''
```

### Mutation checks — each defect introduced for real, then reverted

A green suite is not evidence that an assertion bites, so each of C1, I1 and I3 was
introduced into the committed source, the suite run, and the source restored. Restores
were confirmed with `git diff --stat HEAD` returning nothing.

**C1 — the `if strip_track_prefix:` guard deleted:**

```
FAILED tests/test_normalize.py::test_normalize_strips_leading_track_numbers
FAILED tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names
FAILED tests/test_normalize.py::test_normalize_keeps_a_four_digit_year - Asse...
FAILED tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping
FAILED tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles
========================= 5 failed, 19 passed in 0.07s =========================
```

and on the C1 test alone:

```
        assert normalize("4pi", strip_track_prefix=False) == "4pi"
        assert normalize("1st EP", strip_track_prefix=False) == "1st ep"
        # These two *are* flag-sensitive. Deleting the `if strip_track_prefix:` guard from
        # `normalize` must fail here; see the mutation check in the report.
        assert normalize("01 Title", strip_track_prefix=False) == "01 title"
>       assert normalize("01 Title", strip_track_prefix=True) == "title"
E       AssertionError: assert '01 title' == 'title'
E
E         - title
E         + 01 title
E         ? +++
```

Five tests catch it, not one — the guard deletion is also visible to the empty-key case
(`normalize("1-01 ")` returns `'1-01'` instead of `''`) and to the year test, which are
independent reasons to be glad the flag is exercised.

**I1 — cap widened `\d{1,3}` → `\d{1,4}`:**

```
FAILED tests/test_normalize.py::test_normalize_keeps_a_four_digit_year - Asse...
========================= 1 failed, 23 passed in 0.08s =========================
```

**I3 — order reverted to stem-then-fold:**

```
FAILED tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping
========================= 1 failed, 23 passed in 0.08s =========================
```

with the specific assertion:

```
>       assert normalize("Song．Ｍ４Ａ") == "song"
E       AssertionError: assert 'song.m4a' == 'song'
```

### Note on my round-1 mutation probes

Round 1's probes used monkeypatching, and the first one was wrong in a way that flattered
the tests: the test module does `from hub.normalize import stem_of`, so patching the
module attribute left the test's own binding pointing at the real function and the
mutation looked uncaught. These probes edit the committed source instead, which is why the
numbers above can be trusted. The lesson generalises: patch both the module global and the
import-time binding, or mutate the file.

### Still open for the next task

1. **The three stale spec/plan spots from round 1's addendum are unfixed** — plan line 262
   (`path.stem` + `"NFC"`), spec §7.6 line 369 (`1979 - Song` → `song`, contradicting
   §7.5), plan line 231 (the re-imported composed/decomposed tautology). I have still not
   touched them, since another agent owns those files. Note the spec §7.5 *has* been
   reordered and now documents the year rationale, so only §7.6 remains contradictory.
2. **Task 3 must use `is_audio_file()`**, not `ext in AUDIO_EXTS`.
3. **Task 3 builds the index with the flag off; Task 4 looks up with it off.** Still
   unenforced by anything but the tests here.
4. **Task 4 must refuse to skip on an empty key.** Six real tracks normalize to `""`.

---

## Fix round 3

Status: **DONE**
Commit: `9994faa0af8417a0ea44c493197e15649c7fd913` (on `feat/phase-1-foundation`, after
`400d1ec4` and your `90b4081` / `2d8e489` / `1fa9560`). Files touched:
`hub/hub/normalize.py`, `hub/tests/test_normalize.py`. 24 → 28 tests. `config.py` and
`test_config.py` needed no change.

### N1 — `stem_of` no longer uses `pathlib` (and it was not latent)

Confirmed before changing anything:

```
  '01. A／B.m4a'          -> 'b'
  '01. B.m4a'             -> 'b'
  '01. C／B.m4a'          -> 'b'
  collision A／B vs B: True
```

Fixed with `rpartition(".")`, and `pathlib` is gone from the module entirely. The leading
dot is guarded: `".m4a"` has an empty base, so it is returned unchanged rather than
reduced to `""`.

**Your finding understated this one.** The solidus is 0 occurrences among *filenames*,
which is what I checked too — but there is **1 directory basename** carrying it, so N1 was
live on real data, not latent:

```
DIR  basenames containing U+FF0F: 1
   dir: Neko Hacker/あくたんのこと好きすぎ☆ソング／For The Win (2022 ver.)
     before='for the win (2022 ver.)'
     after ='あくたんのこと好きすぎ☆ソング/for the win (2022 ver.)'
```

That album's key was being truncated to its last component, dropping the series prefix
that identifies it — a lossy album identity, and one more `／` away from a collision. It
is the only directory whose key changes, and it is now keyed faithfully.

As a side effect, round 2's **M2 finding is resolved rather than documented**: `"/"` is no
longer structural, so `stem_of("AC/DC - Back in Black")` keeps every character instead of
reducing to `"DC - Back in Black"`. The docstring now says so, and a test asserts it.

### N2 — the separator is no longer a greedy run (live)

Confirmed against the real library before changing anything:

```
  '13. …to mo da ti _.m4a'       -> 'to mo da ti _'
  '04. ...And Then'              -> 'and then'
  'And Then'                     -> 'and then'      <- collision
```

Applied the spec's pattern `^(?:\d{1,3}(?:\s*[.\-_]\s*|\s+)){1,2}`. All seven rows of your
table are pinned in `test_normalize_keeps_a_separator_with_padding_and_stops_at_two_groups`,
including the padded separator `1 - 01 - Title` and the two-group cap, so the rewrite is
shown not to have regressed what the greedy version handled.

### I2 — the `is_audio_file` contract

`is_audio_file` and `stem_of` now share one private helper, `_audio_base`, which is the
single source of truth for "does this name end in a known audio extension". They therefore
cannot drift apart — which was the actual airtiness, more than the missing test.

On `is_audio_file(".m4a")`: I **kept it False and pinned it**, rather than making it
handle a bare extension. Reason: a leading dot marks a hidden file, `stem_of(".m4a")`
returns `".m4a"` unchanged, and having the two agree on the ambiguous input is worth more
than accepting a form no caller has. The docstring states the input contract outright, and
`test_audio_exts_membership_table` asserts both that it is False and that `stem_of` agrees.

The membership table now lives on the `AUDIO_EXTS` comment and is executed by
`test_audio_exts_membership_table` — 11 rows, including the separator rows
(`"01. Artist - Title"`, `"1. Title"`) and the solidus row (`"01. A／B.m4a"` → True).

### N3 — two corrections to my round-2 report

Noted here rather than re-run, as instructed; the underlying results are unaffected.

1. **The C1 check is mislabelled and the failure count does not match the label.** I wrote
   "the `if strip_track_prefix:` guard deleted" and reported 5 failures. A real deletion
   produces **1**. 5 is what `if not strip_track_prefix:` — the *inversion* — produces.
   My mutation script replaced the two-line `if` block with a comment, so the flag was
   forced to the `False` branch on every call, i.e. the inversion. The conclusion stands
   (the flag is exercised, and `test_normalize_can_keep_track_numbers_for_album_names`
   fails on its `strip_track_prefix=True` line), but the label described an experiment I
   did not run, and quoting "5 failed" under it invites the wrong inference.
2. **`normalize("197 Title")` catches cap *narrowing*, not widening.** I claimed it stops
   "cap-widening + separator-tightening". It cannot: widening the cap to `\d{1,4}` leaves
   `197 Title` keying to `title` either way. What that assertion actually pins is
   *narrowing* to `\d{1,2}`, where `197` no longer matches and the key becomes
   `197 title`. The round-2 I1 mutation used `\d{1,4}` and was caught by
   `test_normalize_keeps_a_four_digit_year`'s first two lines, not by the `197 Title` line.

### Verification — full suite

`cd hub && uv run pytest -v`

```
============================= test session starts ==============================
platform linux -- Python 3.13.7, pytest-9.1.1, pluggy-1.6.0 -- /home/m/apple-dl_extend/hub/.venv/bin/python
cachedir: .pytest_cache
rootdir: /home/m/apple-dl_extend/hub
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 28 items

tests/test_config.py::test_requires_password PASSED                      [  3%]
tests/test_config.py::test_requires_password_when_the_variable_is_absent PASSED [  7%]
tests/test_config.py::test_parses_multiple_library_roots PASSED          [ 10%]
tests/test_config.py::test_defaults_match_the_spec PASSED                [ 14%]
tests/test_config.py::test_default_library_roots_are_the_two_spec_libraries PASSED [ 17%]
tests/test_config.py::test_reads_os_environ_when_no_mapping_is_given PASSED [ 21%]
tests/test_config.py::test_generates_a_session_secret_when_unset PASSED  [ 25%]
tests/test_config.py::test_rejects_a_short_session_secret PASSED         [ 28%]
tests/test_config.py::test_rejects_an_unknown_artist_scope PASSED        [ 32%]
tests/test_config.py::test_rejects_a_non_numeric_port PASSED             [ 35%]
tests/test_config.py::test_rejects_a_port_outside_the_bindable_range PASSED [ 39%]
tests/test_normalize.py::test_stem_of_drops_only_a_known_audio_extension PASSED [ 42%]
tests/test_normalize.py::test_stem_of_keeps_a_name_whose_dot_is_not_an_extension PASSED [ 46%]
tests/test_normalize.py::test_normalize_strips_leading_track_numbers PASSED [ 50%]
tests/test_normalize.py::test_normalize_can_keep_track_numbers_for_album_names PASSED [ 53%]
tests/test_normalize.py::test_normalize_keeps_a_four_digit_year PASSED   [ 57%]
tests/test_normalize.py::test_normalize_folds_decomposed_and_composed_equally PASSED [ 60%]
tests/test_normalize.py::test_normalize_folds_fullwidth_to_halfwidth PASSED [ 64%]
tests/test_normalize.py::test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title PASSED [ 67%]
tests/test_normalize.py::test_normalize_does_not_swallow_a_title_leading_dot_run PASSED [ 71%]
tests/test_normalize.py::test_audio_exts_membership_table PASSED         [ 75%]
tests/test_normalize.py::test_normalize_keeps_a_separator_with_padding_and_stops_at_two_groups PASSED [ 78%]
tests/test_normalize.py::test_normalize_folds_fullwidth_structure_before_stripping PASSED [ 82%]
tests/test_normalize.py::test_is_audio_file_is_case_insensitive PASSED   [ 85%]
tests/test_normalize.py::test_normalize_squeezes_whitespace PASSED       [ 89%]
tests/test_normalize.py::test_normalize_returns_empty_for_unusable_titles PASSED [ 92%]
tests/test_normalize.py::test_audio_exts_cover_the_real_library PASSED   [ 96%]
tests/test_normalize.py::test_format_buckets_are_casefolded PASSED       [100%]

============================== 28 passed in 0.08s ==============================
```

### Mutation checks

**N1 — `PurePath` restored in `stem_of`:**

```
FAILED tests/test_normalize.py::test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title
========================= 1 failed, 27 passed in 0.10s =========================
```

**N2 — greedy `[\s._-]+` separator restored:**

```
FAILED tests/test_normalize.py::test_normalize_does_not_swallow_a_title_leading_dot_run
========================= 1 failed, 27 passed in 0.06s =========================
```

with the assertion that bites:

```
>       assert normalize("13. …to mo da ti _.m4a") == "...to mo da ti _"
E       AssertionError: assert 'to mo da ti _' == '...to mo da ti _'
```

Both restores verified with `git diff --stat HEAD` returning nothing.

### Regression sweep over the real library

A snapshot of every key was taken **before** any edit and compared after. 15,317
filenames (10,184 of them audio by `is_audio_file`) plus 4,366 directory names, matching
your 15,317 figure.

```
filenames: before=15317 after=15317
dirnames : before=4366 after=4366

identical keys: 15299/15317
CHANGED with strip_track_prefix=False (album path): 0
CHANGED with strip_track_prefix=True (track path): 18
CHANGED dirnames (album keys): 1

=== are all 18 changes a PREFIX RESTORATION (after = restored + before)? ===
  all 18 are 'after == <restored prefix> + before': True
  (so every change ADDS information; none removes any)
```

**0 changes on the album path**, as you measured for round 2.

**18 on the track path, and all 18 are information-restoring.** I verified the direction
programmatically rather than by eye — for every one, `after == <restored prefix> + before` —
so no key lost anything. The restored characters are the leading `_`, `-`, and dot-runs the
greedy run had eaten:

```
  08. _3.m4a              '3'              -> '_3'
  02. -ize 2021.m4a       'ize 2021'       -> '-ize 2021'
  13. …to mo da ti _.m4a  'to mo da ti _'  -> '...to mo da ti _'
  13. ___Under Construxion___.m4a  'under construxion___' -> '___under construxion___'
  1-01 .......lrc         'lrc'            -> '......lrc'
  1-06 ______ 羽生まゐご Remix.m4a  '羽生まゐご remix' -> '_____ 羽生まゐご remix'
  ... 11 more, all the same shape
```

**No new false-skip exposure.** Two measures, before against after:

```
within-album duplicate keys: before=6 after=6  (equal -> no new false skip)
album dirs whose key is shared: before=310 after=310
distinct album keys=3985, total dirs=4366, both unchanged
```

One correction to an intermediate claim I made while checking this: the changed album key
`for the win (2022 ver.)` was held by exactly **1** directory, not shared, so the fix does
not remove a collision — it makes a lossy key faithful. The collision counts above are
the accurate statement.

### Still open for the next task

1. **Task 3 must use `is_audio_file()`**, never `ext in AUDIO_EXTS`.
2. **Task 3 builds the album index with `strip_track_prefix=False`; Task 4 looks up with it
   off.** Still unenforced by anything but the tests here.
3. **Task 4 must refuse to skip on an empty key.** Re-verified after every reorder;
   `""`, `"..."`, `"1-01 "` and `"1-01 ..."` all still return `""`.
