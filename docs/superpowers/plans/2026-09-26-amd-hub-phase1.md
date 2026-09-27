# amd-hub Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `docker compose up`-able web UI that logs into Apple Music, accepts URLs, downloads tracks through `AppleMusicDecrypt`'s existing pipeline, and skips tracks that already exist in the user's real (messy, 400 GB) libraries.

**Architecture:** One container. A FastAPI app supervises `wrapper-lite-rootless` as a child process (so 2FA can be a web form) and imports `AppleMusicDecrypt/src` in-process to do the downloading. Dedup is **stateless and filesystem-derived**: every request re-walks the library roots (measured 0.06 s for 4,367 dirs / 10,184 files), discovers album directories by name, and matches the track title *within that album scope only*. The only persisted state is a `job` table for the queue.

**Tech Stack:** Python 3.13, FastAPI, uvicorn, HTMX (server-rendered, no Node toolchain), SQLite (WAL), pytest, uv. Multi-stage Docker: stage 1 builds `wrapper/` with NDK r23b + cmake, stage 2 is the Python runtime.

**Spec:** `docs/superpowers/specs/2026-09-26-amd-hub-design.md` — read it before starting; this plan argues from it and does not restate it.

**Scope:** Phase 1 only. Phase 2 (library browsing UI, duplicate report UI) and Phase 3 (playback) are separate spec→plan cycles per spec §13.

## Global Constraints

- Python floor `3.11` (spec §3, matches `AppleMusicDecrypt/pyproject.toml` `requires-python`). Local venv is 3.13.7; **never bare `python3`** (system is 3.14.7 and will not have the deps).
- Library roots come from `AMD_LIBRARY_ROOTS`, a comma-separated list. **Not** `AMD_LIBRARY_ROOT` (singular) — spec §7.2 changed this to plural.
- Default `AMD_BIND=0.0.0.0`, port `8080`. Only 8080 is published. The wrapper listens on loopback `12340` and is never published.
- `AMD_PASSWORD` is required; the app refuses to start without it. Compare with `secrets.compare_digest`.
- Apple credentials are held **in memory only**. Never env vars, never the DB, never logs.
- Dedup reads the filesystem per request. **No `recording` / `release` / `library_file` tables, no cache, no TTL** (spec §7.1.1: measured 0.06 s).
- `dedup.artist_scope` default is `loose` (album name only). `strict` also requires the artist to match.
- `force` bypasses the entire dedup check.
- The only persisted table is `job`, with the partial unique index `job_active_dedupe ON job(adam_id, codec) WHERE status IN ('queued','waiting','running')`.
- `AppleMusicDecrypt/src` must not be modified. **Only `hub/hub/ripper_host.py` may import it.**
- The upstream TUI must keep working: `cd AppleMusicDecrypt && uv run python main.py`.
- The 69 GB `downloads/` tree and the external NTFS drive must never enter the Docker build context (`.dockerignore` is written; keep it that way).
- Commits are atomic, one logical change, English message, conventional-commit prefix.

## Review Focus

Five input classes the spec implies but that are easy to get silently wrong. Each has a test pinned to the task that owns the code.

1. **A library root that is a symlink.** The user's own path is `/home/m/Music/HDD_Music` → `/run/media/m/1A5E05A75E057D2F/Music`. If root validation compares a non-resolved path against `realpath` output, every file looks like it is outside the root and dedup silently finds nothing. → Task 3
2. **Empty or punctuation-only track titles.** The real library has 6 such titles. `normalize()` returns `""` for them, and `""` matches every other empty title in the same album scope, skipping real downloads. → Task 3
3. **`artist_scope=loose` false-skip.** Two different artists with identically-named albums ⇒ a legitimate download is skipped. The mitigation is not perfect detection, it is that `skip_reason` always carries the matched real paths so a human can adjudicate. → Task 4
4. **External drive not mounted.** Dedup degrades to "nothing found" and re-downloads the whole library. This must be loud, not silent. → Task 3
5. **`.part` files from an interrupted download.** The real library has 160. If they count as existing tracks, a re-request is wrongly skipped. → Task 3

---

## File Structure

```
Dockerfile                      multi-stage: wrapper/ build → python runtime
compose.yaml                    single service, two library roots
.env.example                    AMD_PASSWORD, AMD_BIND, AMD_LIBRARY_ROOTS
hub/pyproject.toml              deps + pytest config
hub/hub/
  __init__.py
  config.py                     env → Settings (pydantic)
  normalize.py                  AUDIO_EXTS, stem_of, normalize          §7.5
  library_scan.py               AlbumDir, LibraryScan, scan_roots      §7.3 Step 1
  dedup.py                      DuplicateHit, find_duplicate            §7.3 Step 2-3
  events.py                     EventBroker (in-proc pub/sub → SSE)
  jobs.py                       JobStore, JobScheduler, Leaf
  resolver.py                   expand(url) -> [Leaf]                  §5.1
  ripper_host.py                creart bootstrap; ONLY file importing src.*
  wrapper_supervisor.py         child-process lifecycle + 2FA login
  auth.py                       session cookie, rate limit
  app.py                        FastAPI factory, lifespan, static mount
  api/auth.py  api/jobs.py  api/wrapper.py  api/library.py
  web/templates/  web/static/
tests/
  conftest.py                   fixtures: tmp library trees
  test_normalize.py
  test_library_scan.py
  test_dedup.py
  test_jobs.py
  test_resolver.py
  test_auth.py
  test_supervisor.py            marked slow; needs the real binary
docs/superpowers/
  specs/2026-09-26-amd-hub-design.md
  plans/2026-09-26-amd-hub-phase1.md
  findings/2026-09-26-wrapper-child-process-spike.md
```

Boundaries: `normalize` has no I/O and no config. `library_scan` does I/O but no matching logic. `dedup` does matching but no I/O — it consumes a `LibraryScan`. `jobs` owns SQLite. `ripper_host` is the only module that knows `AppleMusicDecrypt` exists.

---

## Task 1: Spike — can the rootless launcher run as a child process?

Spec §16 step 1 and §15 row 1. This is a **spike**: the deliverable is a written finding, not production code. It gates the supervisor design, so it goes first.

**Files:**
- Create: `hub/spike/child_process_probe.py`
- Create: `docs/superpowers/findings/2026-09-26-wrapper-child-process-spike.md`

**Interfaces:**
- Consumes: nothing.
- Produces: a finding doc with a verdict of `works` or `needs-fallback`. Task 5 branches on that verdict.

- [ ] **Step 1: Build `wrapper-lite-rootless` (this is the expensive step, ~10–20 min)**

Per spec §14 / `wrapper/.github/workflows/build-lite.yml`, inside `wrapper/`:

```bash
aria2c -o android-ndk-r23b-linux.zip https://dl.google.com/android/repository/android-ndk-r23b-linux.zip
unzip -q android-ndk-r23b-linux.zip
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_HOST_LAUNCHERS=ON
cmake --build build -j"$(nproc)"
```

Expected: `wrapper/wrapper-lite-rootless` and `wrapper/rootfs/system/bin/lite` exist. If the build fails, stop and report — `-Wall -Werror` is on for Release and that is upstream's constraint, not ours to relax.

- [ ] **Step 2: Write the probe script**

`hub/spike/child_process_probe.py` spawns the launcher as a **child of a normal Python process** (not PID 1), captures stdout, and reports whether it reached the point of printing its listen banner.

```python
# Contract the probe must satisfy (assert these in the script itself):
# - proc = subprocess.Popen([binary, "--base-dir", tmp, "--host", "127.0.0.1",
#                            "--port", str(port)], stdout=PIPE, stderr=STDOUT)
# - within 30 s, combined output contains b"12340" or b"listen"
# - the process is NOT pid 1 (assert proc.pid != 1 and os.getppid() != 0)
# - the child survives its parent not being pid 1: i.e. no userns EPERM
```

Use `seccomp` default (unconfined). The upstream `compose.yaml` uses only `security_opt: [seccomp:unconfined]` and no `privileged`, so that is the configuration under test.

- [ ] **Step 3: Run the probe on the host**

Run: `cd hub && uv run python spike/child_process_probe.py --binary ../wrapper/wrapper-lite-rootless`
Expected: prints a verdict line `VERDICT: works` or `VERDICT: needs-fallback` plus the captured output.

- [ ] **Step 4: Run the probe inside a container with only `seccomp:unconfined`**

```bash
docker run --rm -v "$PWD:/w" -w /w/hub --security-opt seccomp=unconfined \
  python:3.13-slim uv run python spike/child_process_probe.py \
  --binary /w/wrapper/wrapper-lite-rootless
```
Expected: same verdict. A `works` on the host but `needs-fallback` in the container is the important case — it means compose needs `cap_add` or the two-container topology.

- [ ] **Step 5: Write the finding**

`docs/superpowers/findings/2026-09-26-wrapper-child-process-spike.md` records: the exact commands, both verdicts, the captured launcher output, and the conclusion. State plainly which topology Task 5 must build.

- [ ] **Step 6: Commit**

```bash
git add hub/spike/child_process_probe.py docs/superpowers/findings/
git commit -m "spike: verify wrapper-lite-rootless runs as a child process"
```

---

## Task 2: Project skeleton, config, and normalize()

Pure logic first — cheapest to verify, and `normalize` is the function every later task depends on.

**Files:**
- Create: `hub/pyproject.toml`
- Create: `hub/hub/__init__.py`
- Create: `hub/hub/config.py`
- Create: `hub/hub/normalize.py`
- Create: `tests/conftest.py`
- Create: `tests/test_normalize.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `AUDIO_EXTS: frozenset[str]`
  - `is_audio_file(name: str) -> bool` — takes a **filename or path**, not a bare
    extension (`is_audio_file(".m4a")` is `False`; a leading dot means no suffix)
  - `stem_of(filename: str) -> str` — returns the input with a known audio extension
    removed; returns the input **unchanged** otherwise. It does not take a basename:
    `stem_of("album/01. Track.flac")` is `"album/01. Track"`, so **pass basenames,
    never joined paths**, or every key acquires a directory prefix and nothing ever matches
  - `normalize(name: str, *, strip_track_prefix: bool = True) -> str`
  - `FORMAT_BUCKETS: frozenset[str]`
  - `Settings` (pydantic BaseModel) with fields `password: str`, `bind: str`, `port: int`, `library_roots: list[Path]`, `wrapper_binary: Path`, `wrapper_base_dir: Path`, `wrapper_host: str`, `wrapper_port: int`, `dedup_artist_scope: Literal["loose","strict"]`, `db_path: Path`, `session_secret: bytes`
  - `load_settings(env: Mapping[str, str] | None = None) -> Settings` — raises `RuntimeError` if `AMD_PASSWORD` is unset/empty

- [ ] **Step 1: Write `pyproject.toml` and the package skeleton**

`hub/pyproject.toml`: `requires-python = ">=3.11"`.

**Only this task's dependencies go in now.** Task 2 must stay the fast, hermetic
task it exists to be, and the heavy crates (`temari` ships platform cdylibs,
`pywidevine` is large) are not needed to test a string function and a settings
model. Each later task adds its own deps as it needs them:

| Task | Adds |
|---|---|
| 2 (this one) | `pydantic>=2` |
| 5 | `httpx` |
| 6 | the `AppleMusicDecrypt` runtime set: `loguru`, `m3u8`, `tenacity`, `regex`, `beautifulsoup4`, `lxml`, `tabulate`, `async-lru`, `creart`, `temari`, `pywidevine`, `pycryptodome`, `prompt-toolkit` |
| 8 | nothing new |
| 9 | `fastapi`, `uvicorn[standard]`, `itsdangerous`, `jinja2`, `python-multipart` |

Dev group: `pytest`, `pytest-asyncio`. Configure
`[tool.pytest.ini_options] asyncio_mode = "auto"`.

`hub/hub/__init__.py` is empty except for a one-line docstring.

- [ ] **Step 2: Write the failing normalize tests**

```python
# tests/test_normalize.py
from hub.normalize import AUDIO_EXTS, normalize, stem_of

def test_stem_of_drops_only_a_known_audio_extension():
    assert stem_of("1-01 Caribbean Blue.m4a") == "1-01 Caribbean Blue"
    assert stem_of("Song. Pt. 2.m4a") == "Song. Pt. 2"
    assert stem_of("no-extension") == "no-extension"

def test_stem_of_keeps_a_name_whose_dot_is_not_an_extension():
    # playlistSongNameFormat renders "01. Artist - Title" with no extension.
    # pathlib.PurePath(...).stem would reduce that to "01", collapsing every
    # playlist-downloaded file to a bare track index. Only AUDIO_EXTS suffixes
    # may be stripped.
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
    # Album dirs are matched with strip_track_prefix=False (spec §7.5 Step 2a).
    # The flag is the single highest-consequence parameter in this file: Task 3
    # builds its by_name index with the flag off and Task 4 looks up with it off,
    # so a mismatch makes every album lookup miss and nothing is ever skipped.
    # The assertion must therefore be on a literal the flag actually changes --
    # "4pi" and "1st EP" cannot, because neither has a separator after its
    # leading digit, so both give the same answer either way.
    assert normalize("01 Title", strip_track_prefix=False) == "01 title"
    assert normalize("01 Title", strip_track_prefix=True) == "title"
    # Real album names, documenting intent. Not discriminating on their own.
    assert normalize("4pi", strip_track_prefix=False) == "4pi"
    assert normalize("1st EP", strip_track_prefix=False) == "1st ep"

def test_normalize_folds_case_unicode_and_whitespace():
    assert normalize("  ABC   Title  ") == "abc title"
    assert normalize("Caf\u00e9") == normalize("Cafe\u0301")   # composed == decomposed
    assert normalize("a   b") == normalize("a b")

def test_normalize_folds_fullwidth_to_halfwidth():
    # a Japanese library routinely contains both. NFC does NOT do this;
    # NFKC is required, and casefold alone leaves U+FF21 as U+FF41.
    assert normalize("ＡＢＣ Title") == "abc title"
    assert normalize("ＡＢＣ Title") == normalize("ABC Title")

def test_normalize_keeps_a_fullwidth_solidus_as_part_of_the_title():
    # NFKC folds ／ (U+FF0F) to "/". If stem_of used pathlib.PurePath, that "/"
    # would act as a path separator and "01. A／B.m4a" would key to "b",
    # colliding with "01. B.m4a". ／ is ordinary Japanese typography.
    assert normalize("01. A／B.m4a") == "a／b"
    assert normalize("01. A／B.m4a") != normalize("01. B.m4a")

def test_normalize_does_not_swallow_a_title_leading_dot_run():
    # Real file in the 341 GB library: 13. …to mo da ti _.m4a
    # NFKC turns … into "...", and a greedy [\s._-]+ run would eat it.
    assert normalize("13. …to mo da ti _.m4a") == "...to mo da ti _"
    assert normalize("04. ...And Then") != normalize("And Then")

def test_normalize_keeps_a_four_digit_year():
    # \d{1,3} deliberately does not match 4 digits, so a year survives and
    # "1979 - Song" matches "01-1979 - Song". Widening to \d{1,4} would break this.
    assert normalize("1979 - Song.m4a") == "1979 - song"
    assert normalize("01-1979 - Song") == "1979 - song"

def test_normalize_folds_fullwidth_structure_before_stripping():
    # NFKC must run BEFORE the structural strips, or fullwidth track numbers and
    # fullwidth extensions survive into the key and nothing ever matches.
    assert normalize("０１．Artist - Title") == normalize("01. Artist - Title")
    assert normalize("Song．Ｍ４Ａ") == "song"

def test_normalize_returns_empty_for_unusable_titles():
    # Review Focus #2: these must be detectable, not silently match everything
    assert normalize("") == ""
    assert normalize("...") == ""
    assert normalize("1-01 ") == ""
    assert normalize("1-01 ...") == ""

def test_audio_exts_cover_the_real_library():
    # measured on /run/media/m/1A5E05A75E057D2F/Music
    assert {".m4a", ".flac", ".mp4"} <= AUDIO_EXTS
    assert ".jpg" not in AUDIO_EXTS and ".lrc" not in AUDIO_EXTS and ".part" not in AUDIO_EXTS
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd hub && uv run pytest tests/test_normalize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.normalize'`

- [ ] **Step 4: Implement `normalize.py`**

`AUDIO_EXTS` = the measured set plus reasonable siblings: `{".m4a",".mp4",".m4b",".flac",".aac",".ec3",".ac3",".wav",".ogg",".opus"}`. `FORMAT_BUCKETS` = `{"alac","atmos"}` (casefolded).

`normalize` applies, in order: suffix removal **only when the suffix is in `AUDIO_EXTS`** (reuse `stem_of` — do NOT use `pathlib.PurePath(...).stem`, which reduces `01. Artist - Title` to `01`); then if `strip_track_prefix` apply `re.sub(r"^(\d{1,3}[\s._-]+){1,2}", "", s)` — note `\d{1,3}` deliberately does not match a 4-digit year, so `1979 - Song` keeps its number; then `unicodedata.normalize("NFKC", s)` — **NFKC, not NFC**, so fullwidth `ＡＢＣ Title` folds to `abc title`; then `.casefold()`; then `re.sub(r"\s+", " ", s).strip()`; then return `""` if no alphanumeric character survives.

Keep it a pure function with no config or filesystem access.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd hub && uv run pytest tests/test_normalize.py -v`
Expected: PASS, 6 passed.

- [ ] **Step 6: Add config and its tests**

`tests/test_config.py`:

```python
import pytest
from hub.config import load_settings

def test_requires_password():
    with pytest.raises(RuntimeError, match="AMD_PASSWORD"):
        load_settings({"AMD_PASSWORD": ""})

def test_parses_multiple_library_roots():
    s = load_settings({"AMD_PASSWORD": "x", "AMD_LIBRARY_ROOTS": "/library/a,/library/b"})
    assert [p.as_posix() for p in s.library_roots] == ["/library/a", "/library/b"]

def test_defaults_match_the_spec():
    s = load_settings({"AMD_PASSWORD": "x"})
    assert s.bind == "0.0.0.0" and s.port == 8080
    assert s.wrapper_port == 12340 and s.wrapper_host == "127.0.0.1"
    assert s.dedup_artist_scope == "loose"
```

- [ ] **Step 7: Commit**

```bash
git add hub/pyproject.toml hub/hub/ tests/
git commit -m "feat: project skeleton, settings, and filename normalization"
```

---

## Task 3: Library scan — album discovery over multiple messy roots

Spec §7.3 Step 1 and §8. This is where Review Focus items 1, 2, 4, 5 live.

**Files:**
- Create: `hub/hub/library_scan.py`
- Create: `tests/test_library_scan.py`
- Modify: `tests/conftest.py`

**Interfaces:**
- Consumes: `AUDIO_EXTS`, `FORMAT_BUCKETS`, `normalize`, `is_audio_file`, `stem_of` from
  `hub.normalize` (Task 2). Use `is_audio_file` rather than raw `AUDIO_EXTS` membership,
  and pass **basenames** to `normalize` — `os.listdir` already yields basenames, which is
  why the album-index loop must not join the parent directory back on.
- Produces:
  - `@dataclass(frozen=True, slots=True) class AlbumDir` with fields `root_index: int`, `relpath: str`, `name: str`, `artist: str | None`, `track_keys: frozenset[str]`
  - `@dataclass(frozen=True, slots=True) class LibraryScan` with fields `roots: tuple[Path, ...]`, `reachable: tuple[bool, ...]`, `albums: tuple[AlbumDir, ...]`, `by_name: Mapping[str, tuple[AlbumDir, ...]]`, and property `degraded: tuple[Path, ...]`
  - `scan_roots(roots: Sequence[Path]) -> LibraryScan`

`by_name` is keyed by `normalize(album_basename, strip_track_prefix=False)` — the flag is load-bearing and must match `dedup.find_duplicate`.

- [ ] **Step 1: Add library fixtures to `conftest.py`**

Two fixtures, both defined in this task because this task's own tests need them:

`make_library(tmp_path)` builds the shapes observed in the real 341 GB library, so the
regression fixtures are the spec's §12.1 table:

```
lib/a/ALAC/鎖那/Hush a by little girl/01 track.m4a
lib/b/new-dl/鎖那/Hush a by little girl/01 track.m4a        # 種別 A: 同一リリースの複数配置
lib/a/ALAC/EmoCosine/らぶふぉーゆー - Single/t.m4a
lib/a/ALAC/ころねぽち/らぶふぉーゆー - Single/t.m4a          # 種別 B: コラボ・ファンアウト
lib/a/ALAC/TEMPLIME/POP-AID/x.m4a
lib/a/TEMPLIME/POP-AID/y.m4a                               # フォーマットバケット有無の違い
lib/a/Album One/intro.m4a
lib/a/Album Two/intro.m4a                                 # 別アルバムに同名 → skip しない
lib/a/Album Three/intro.m4a
lib/a/Nyarons/A.flac                                       # artist 直下の散在ファイル
lib/a/TEMPLIME/Escapism/t.m4a
lib/a/TEMPLIME/HIKO.flac                                   # 子ディレクトリと同居
lib/a/Album Four/..m4a                                      # タイトルが空（Review Focus #2）
lib/a/Album Four/01 ..m4a                                   # トラック番号だけ
lib/a/Album Five/01 real.m4a.part                          # Review Focus #5
lib/a/Album Six/01 real.m4a
```

`make_library_extra(tmp_path)` covers album-name identity, which the main fixture cannot
express because its album names carry no leading digits:

```
lib/extra/4pi/01 t.m4a
lib/extra/1st EP/01 t.m4a
lib/extra/4 - Leaves/01 t.m4a
lib/extra/Song - Single/t.m4a
lib/extra/Album [Deluxe]/t.m4a
lib/extra/・・・/t.m4a          # punctuation-only album name -> "" key
```

Both return the tmp root so tests can pass either to `scan_roots`.

- [ ] **Step 2: Write the failing scan tests**

```python
# tests/test_library_scan.py
def test_finds_album_dirs_at_the_shallowest_level(make_library):
    scan = scan_roots(make_library)
    names = {a.relpath for a in scan.albums}
    assert "a/ALAC/鎖那/Hush a by little girl" in names
    assert "a/Nyarons" in names                      # loose files directly under artist
    assert "a/TEMPLIME" in names                     # dir with both files and subdirs

def test_does_not_descend_into_an_album_dir(make_library):
    # "TEMPLIME" is an album dir because it holds HIKO.flac; "TEMPLIME/Escapism"
    # is a separate album dir, and TEMPLIME/Escapism's keys are not merged in.
    scan = scan_roots(make_library)
    temp = next(a for a in scan.albums if a.relpath == "a/TEMPLIME")
    assert "x" in temp.track_keys and "hiko" in temp.track_keys
    assert "t" not in temp.track_keys

def test_groups_same_named_album_dirs_across_roots(make_library):
    scan = scan_roots(make_library)
    hush = scan.by_name["hush a by little girl"]
    assert {a.relpath for a in hush} == {
        "a/ALAC/鎖那/Hush a by little girl",
        "b/new-dl/鎖那/Hush a by little girl",
    }

def test_album_name_index_keeps_leading_digits(make_library_extra):
    # spec §7.5: directory names do NOT get the track-number strip, so an album
    # literally named "4pi" must be reachable. This is the test that catches a
    # by_name index built with the wrong strip_track_prefix flag — the most
    # damaging silent failure in the whole dedup path, since every album lookup
    # would miss and nothing would ever be skipped.
    scan = scan_roots(make_library_extra)
    # Discriminating literal: "4 - Leaves" has a separator after its leading
    # digits, so the index key differs depending on the flag. "4pi" and "1st EP"
    # cannot discriminate, because neither has a separator after its digit.
    assert "4 - leaves" in scan.by_name
    # keys are casefolded, so compare against the folded form, not the raw name
    assert "leaves" not in scan.by_name          # index used the flag
    # Real album names, documenting intent. Not discriminating on their own.
    assert "4pi" in scan.by_name
    assert "1st ep" in scan.by_name

def test_derives_artist_from_structure(make_library):
    scan = scan_roots(make_library)
    hush = scan.by_name["hush a by little girl"][0]
    assert hush.artist == "鎖那"
    nyarons = next(a for a in scan.albums if a.relpath == "a/Nyarons")
    assert nyarons.artist is None or nyarons.artist == ""   # undeterminable, not a guess

def test_part_files_are_not_tracks(make_library):
    # Review Focus #5: 160 .part files exist in the real library
    scan = scan_roots(make_library)
    six = scan.by_name["album six"][0]
    assert "real" in six.track_keys
    assert not any(".part" in k for k in six.track_keys)

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
    assert len(scan.albums) > 0     # the good root still works (Review Focus #4)

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
```

Add `make_library_extra` to `conftest.py` containing `lib/extra/4pi/01 t.m4a`.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd hub && uv run pytest tests/test_library_scan.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.library_scan'`

- [ ] **Step 4: Implement `library_scan.py`**

`scan_roots` walks each root with `os.walk`. An `AlbumDir` is a directory whose immediate `os.listdir` contains at least one file for which
`is_audio_file(f)` is true. `.part` is excluded by `AUDIO_EXTS` itself — do not add a second
`.part` check. `f` is a basename, which is what `normalize` expects. Do not descend into an album dir for further album-dir detection — but `os.walk` must still continue past it, because `TEMPLIME/Escapism` is its own album dir.

`relpath` is `path.relative_to(root).as_posix()` using the root **as the caller passed it**, with the path never `resolve()`d — that is what keeps symlinked roots working (Review Focus #1).

`artist` derivation (spec §8): if a parent directory exists, artist = the parent's
basename; else `None`. **Do not add a `FORMAT_BUCKETS` grandparent check** — both
branches return the parent, so it is a no-op, and the only input where it changes
the answer (`ALAC/Atmos/Album`) it makes the answer worse by reporting a codec
directory as the artist.

Reachability: `root.is_dir()`. An unreachable root still appears in `roots` with `reachable=False` and contributes no albums.

Build `by_name` in the same pass, keyed with `strip_track_prefix=False`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd hub && uv run pytest tests/test_library_scan.py -v`
Expected: PASS.

- [ ] **Step 6: Run the whole suite and commit**

Run: `cd hub && uv run pytest -v` → all pass.

```bash
git add hub/hub/library_scan.py tests/
git commit -m "feat: discover album directories across multiple messy library roots"
```

---

## Task 4: Dedup — album-scoped title matching

Spec §7.3 Steps 2–3, §7.4, §7.6.

**Files:**
- Create: `hub/hub/dedup.py`
- Create: `tests/test_dedup.py`

**Interfaces:**
- Consumes: `LibraryScan`, `AlbumDir` from `hub.library_scan` (Task 3); `normalize` from `hub.normalize` (Task 2).
- Produces:
  - `@dataclass(frozen=True, slots=True) class DuplicateHit` with field `matched: tuple[str, ...]` (relpaths, sorted)
  - `find_duplicate(scan: LibraryScan, *, album_name: str, track_title: str, artist_name: str | None, artist_scope: str) -> DuplicateHit | None`

- [ ] **Step 1: Write the failing dedup tests**

```python
# tests/test_dedup.py
from hub.dedup import find_duplicate
from hub.library_scan import scan_roots

def test_skips_when_same_album_exists_in_two_places(make_library):
    # 種別 A — the real library has 233 of these
    hit = find_duplicate(scan_roots(make_library), album_name="Hush a by little girl",
                         track_title="track", artist_name="鎖那", artist_scope="loose")
    assert hit is not None
    assert set(hit.matched) == {"a/ALAC/鎖那/Hush a by little girl",
                                "b/new-dl/鎖那/Hush a by little girl"}

def test_loose_scope_catches_collab_fanout(make_library):
    # 種別 B — same release filed under each credited artist
    hit = find_duplicate(scan_roots(make_library), album_name="らぶふぉーゆー - Single",
                         track_title="t", artist_name="EmoCosine", artist_scope="loose")
    assert hit is not None and len(hit.matched) == 2

def test_strict_scope_requires_artist(make_library):
    kw = dict(album_name="らぶふぉーゆー - Single", track_title="t")
    assert find_duplicate(scan_roots(make_library), artist_scope="strict",
                          artist_name="EmoCosine", **kw) is not None
    # an artist that filed no copy of this album must not match
    assert find_duplicate(scan_roots(make_library), artist_scope="strict",
                          artist_name="Unrelated Artist", **kw) is None

def test_a_hit_is_confined_to_the_album_that_was_asked_for(make_library):
    # "intro" exists in 3 albums here and 6 in the real library. Asking for a
    # track an album does NOT hold must miss; asking for one it does hold must
    # return that album alone, never another album's copy.
    scan = scan_roots(make_library)
    assert find_duplicate(scan, album_name="Album One", track_title="not here",
                          artist_name="x", artist_scope="loose") is None
    for album in ("Album One", "Album Two", "Album Three"):
        hit = find_duplicate(scan, album_name=album, track_title="intro",
                             artist_name="x", artist_scope="loose")
        assert hit is not None
        assert hit.matched == (f"a/{album}",)

def test_format_bucket_prefix_does_not_matter(make_library):
    # The same album sits at a/ALAC/TEMPLIME/POP-AID and a/TEMPLIME/POP-AID.
    # A lookup for "x" must hit only the first, "y" only the second. Expecting
    # one copy's relpath from a lookup for the other's track cannot hold.
    scan = scan_roots(make_library)
    assert find_duplicate(scan, album_name="POP-AID", track_title="x",
                          artist_name="TEMPLIME",
                          artist_scope="loose").matched == ("a/ALAC/TEMPLIME/POP-AID",)
    assert find_duplicate(scan, album_name="POP-AID", track_title="y",
                          artist_name="TEMPLIME",
                          artist_scope="loose").matched == ("a/TEMPLIME/POP-AID",)

def test_album_name_keeps_single_suffix_and_deluxe(make_library_extra):
    # spec §7.5: " - Single" and "[Deluxe]" are part of album identity
    scan = scan_roots(make_library_extra)
    assert find_duplicate(scan, album_name="4pi", track_title="t",
                          artist_name=None, artist_scope="loose") is not None
    assert find_duplicate(scan, album_name="Song - Single", track_title="t",
                          artist_name=None, artist_scope="loose") is not None
    # Must HIT: if " - Single" or "[Deluxe]" were stripped these albums would
    # fuse into one group and the lookup would miss -- the exact bug guarded here.
    assert find_duplicate(scan, album_name="Album [Deluxe]", track_title="t",
                          artist_name=None, artist_scope="loose") is not None
    assert find_duplicate(scan, album_name="Album [Deluxe]", track_title="t",
                          artist_name=None,
                          artist_scope="loose").matched != \
        find_duplicate(scan, album_name="Song - Single", track_title="t",
                       artist_name=None, artist_scope="loose").matched

def test_refuses_to_skip_on_an_empty_album_name(make_library_extra):
    # by_name[""] is reachable: the real library has ALAC/薄塩指数/!_ which
    # normalizes to "". A download whose album name is punctuation-only must
    # not scope-match it.
    assert "" in scan_roots(make_library_extra).by_name
    assert find_duplicate(scan_roots(make_library_extra), album_name="・・・",
                          track_title="t", artist_name=None,
                          artist_scope="loose") is None

def test_refuses_to_skip_on_an_empty_title(make_library):
    # Review Focus #2 — must not treat "" as matching every untitled track
    assert find_duplicate(scan_roots(make_library), album_name="Album Four",
                          track_title="", artist_name=None,
                          artist_scope="loose") is None
    assert find_duplicate(scan_roots(make_library), album_name="Album Four",
                          track_title="...", artist_name=None,
                          artist_scope="loose") is None

def test_unknown_album_returns_none(make_library):
    assert find_duplicate(scan_roots(make_library), album_name="Never Downloaded",
                          track_title="t", artist_name=None,
                          artist_scope="loose") is None

def test_hit_paths_are_sorted_for_stable_display(make_library):
    hit = find_duplicate(scan_roots(make_library), album_name="Hush a by little girl",
                         track_title="track", artist_name=None, artist_scope="loose")
    assert list(hit.matched) == sorted(hit.matched)
```

Add to `make_library_extra` in `conftest.py`: `lib/extra/4pi/01 t.m4a`, `lib/extra/Song - Single/t.m4a`, `lib/extra/Album [Deluxe]/t.m4a`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd hub && uv run pytest tests/test_dedup.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.dedup'`

- [ ] **Step 3: Implement `dedup.py`**

```python
def find_duplicate(scan, *, album_name, track_title, artist_name, artist_scope) -> DuplicateHit | None
```

1. `title_key = normalize(track_title)`; if `title_key == ""`, return `None` (Review Focus #2).
   **Also reject an empty *album* key** — `by_name[""]` is real and reachable: the library
   contains `ALAC/薄塩指数/!_`, which normalizes to `""`. Task 3 measured that a
   punctuation-only album name matches it. Never skip on a key we cannot trust, and that
   principle applies symmetrically to both sides of the comparison.
2. `candidates = scan.by_name.get(normalize(album_name, strip_track_prefix=False), ())`.
3. If `artist_scope == "strict"`: keep only candidates whose `artist` is truthy and equals `normalize(artist_name)` when `artist_name` is truthy.
4. `matched = tuple(sorted(c.relpath for c in candidates if title_key in c.track_keys))`.
5. Return `DuplicateHit(matched)` if `matched` else `None`.

Pure function: no filesystem access, no config reads. `artist_scope` is a parameter, never read from settings inside.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd hub && uv run pytest tests/test_dedup.py -v`
Expected: PASS.

- [ ] **Step 5: Add a cost guard for `find_duplicate` itself**

Not for `scan_roots` — Task 3 already owns that, and duplicating it under the same name
only adds flakiness. `find_duplicate` is what runs **once per track on every request**,
and it must stay a pure in-memory operation. Assert a per-call budget, and make the test
fail loudly if a filesystem walk is introduced:

```python
def test_find_duplicate_costs_no_filesystem_access(make_library, monkeypatch):
    # 0.0069 ms measured; a 0.1 ms budget is ~14x headroom yet still fails if a
    # walk is added. Poison os.scandir/listdir so a walk cannot pass silently.
    scan = scan_roots(make_library)
    with monkeypatch.context() as m:
        m.setattr(os, "scandir", _boom)
        m.setattr(os, "listdir", _boom)
        t0 = time.perf_counter()
        for _ in range(200):
            find_duplicate(scan, album_name="Hush a by little girl",
                           track_title="track", artist_name="鎖那", artist_scope="loose")
        assert (time.perf_counter() - t0) / 200 * 1000 < 0.1
```

Keep the poisoning inside `monkeypatch.context()` — leaking it outlives the test and
breaks the session.

- [ ] **Step 6: Commit**

```bash
git add hub/hub/dedup.py tests/
git commit -m "feat: album-scoped title matching for download dedup"
```

---

## Task 5: Wrapper supervisor

**Depends on Task 1's verdict.** If the spike said `needs-fallback`, implement `WrapperSupervisor` as a thin HTTP client of the second container instead of a child-process manager, and say so in the finding doc before starting.

**Files:**
- Create: `hub/hub/wrapper_supervisor.py`
- Create: `tests/test_supervisor.py`

**Interfaces:**
- Consumes: `Settings.wrapper_binary`, `.wrapper_base_dir`, `.wrapper_host`, `.wrapper_port` (Task 2).
- Produces:
  - `@dataclass class LoginChallenge` with `id: str`, `expires_at: float`
  - `class WrapperSupervisor` with `__init__(self, *, binary: Path, base_dir: Path, host: str, port: int, log_sink: Callable[[str], None], twofa_ttl: float = 300.0, max_restarts: int = 3, startup_timeout: float = 60.0, adopt_existing: bool = True)`, and `async def start(self) -> None`, `async def stop(self) -> None`, `async def status(self) -> dict`, `async def login(self, username: str, password: str) -> LoginChallenge`, `async def submit_2fa(self, challenge_id: str, code: str) -> None`, `property def running(self) -> bool`, `property def adopted(self) -> bool`, `property def pid(self) -> int | None`, `property def bound_port(self) -> int`
    - `port=0` means "bind an ephemeral port"; `bound_port` then reports the port actually chosen. Tests rely on this to avoid port collisions.

**Four behaviors the Task 1 spike established. Each is load-bearing:**

- **R6 — readiness is `GET /status` == 200, never log text.** The launcher prints its
  banner before it serves. Measured time to actually ready: 9.8–18.4 s, so
  `startup_timeout` must exceed 20 s. A log-based gate reports ready too early and the
  first download fails.
- **R7 — stop signals the launcher, never the process group.** `CLONE_NEWPID` makes the
  payload `lite` PID 1 of a nested PID namespace; a group signal does not reach it.
- **R8 — adopt a healthy wrapper already on the port.** This host already runs one on
  127.0.0.1:12340 and the user's own `AppleMusicDecrypt/config.toml` points there, so
  local development collides constantly. When `adopt_existing` is true and `/status`
  answers on the configured port, do not spawn; set `adopted = True`. An adopted
  instance cannot be logged into over stdin, so `login()` on an adopted supervisor must
  raise `SupervisorError` telling the user to log in on the wrapper side. The UI shows
  which mode it is in.
- **Readiness also gates on a pre-flight port check.** If the port is occupied by
  something that is not answering `/status`, `start()` must fail fast with a clear
  "port in use by another process" message rather than spawning a child that will
  EADDRINUSE-exit and self-signal — which reads exactly like an external kill.
  - `class SupervisorError(RuntimeError)`

- [ ] **Step 1: Write the failing supervisor tests**

Use a fake launcher: a small Python script that prints a banner, optionally prompts for a 2FA code on stdin, and serves a `/status` returning `{"code":0,"data":{"regions":["jp"]}}`. This keeps the tests hermetic; the real binary is covered by Task 1.

```python
# tests/test_supervisor.py
async def test_start_waits_until_regions_are_reported(fake_launcher, tmp_path):
    sup = WrapperSupervisor(binary=fake_launcher, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lambda _: None)
    await sup.start()
    try:
        assert sup.running
        assert sup.bound_port > 0
        assert (await sup.status())["regions"] == ["jp"]
    finally:
        await sup.stop()

async def test_stop_terminates_the_child(fake_launcher, tmp_path):
    sup = WrapperSupervisor(binary=fake_launcher, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lambda _: None)
    await sup.start()
    pid = sup.pid
    await sup.stop()
    assert not sup.running
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

async def test_login_raises_a_challenge_when_2fa_is_prompted(fake_launcher_2fa, tmp_path):
    sup = WrapperSupervisor(binary=fake_launcher_2fa, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lambda _: None)
    ch = await sup.login("user", "pass")
    assert ch.id and ch.expires_at > time.time()
    await sup.submit_2fa(ch.id, "123456")

async def test_expired_challenge_is_rejected(fake_launcher_2fa, tmp_path):
    sup = WrapperSupervisor(binary=fake_launcher_2fa, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lambda _: None,
                            twofa_ttl=0.01)
    ch = await sup.login("u", "p")
    await asyncio.sleep(0.05)
    with pytest.raises(SupervisorError, match="expired"):
        await sup.submit_2fa(ch.id, "000000")

async def test_credentials_never_reach_the_log_sink(fake_launcher_2fa, tmp_path):
    lines: list[str] = []
    sup = WrapperSupervisor(binary=fake_launcher_2fa, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lines.append)
    await sup.login("SECRET_USER", "SECRET_PASS")
    await asyncio.sleep(0.1)
    assert not any("SECRET_USER" in l or "SECRET_PASS" in l for l in lines)

async def test_crash_is_reported_not_silently_retried_forever(fake_launcher_crash, tmp_path):
    sup = WrapperSupervisor(binary=fake_launcher_crash, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lambda _: None,
                            max_restarts=3)
    with pytest.raises(SupervisorError):
        await sup.start()

# --- R6: readiness is the HTTP status, not the banner ---------------------
async def test_readiness_waits_for_status_not_for_the_banner(fake_launcher_slow, tmp_path):
    # the launcher prints its banner ~9 s before it actually serves
    sup = WrapperSupervisor(binary=fake_launcher_slow, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lambda _: None,
                            startup_timeout=30.0)
    await sup.start()
    try:
        assert (await sup.status())["regions"] == ["jp"]
    finally:
        await sup.stop()

async def test_start_times_out_when_never_ready(fake_launcher_hang, tmp_path):
    sup = WrapperSupervisor(binary=fake_launcher_hang, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lambda _: None,
                            startup_timeout=1.0)
    with pytest.raises(SupervisorError, match="ready"):
        await sup.start()

# --- R7: signal the launcher, not the group ------------------------------
async def test_stop_signals_the_launcher_pid(fake_launcher, tmp_path):
    sup = WrapperSupervisor(binary=fake_launcher, base_dir=tmp_path,
                            host="127.0.0.1", port=0, log_sink=lambda _: None)
    await sup.start()
    pid = sup.pid
    await sup.stop()
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

# --- R8: adopt an existing healthy wrapper -------------------------------
async def test_adopts_a_healthy_wrapper_already_on_the_port(fake_launcher_external, tmp_path):
    port = await _start_external_wrapper(fake_launcher_external)   # occupies a port
    sup = WrapperSupervisor(binary=fake_launcher_external, base_dir=tmp_path,
                            host="127.0.0.1", port=port, log_sink=lambda _: None,
                            adopt_existing=True)
    await sup.start()
    try:
        assert sup.adopted is True
        assert sup.pid is None          # we did not spawn anything
        assert (await sup.status())["regions"] == ["jp"]
    finally:
        await sup.stop()
        await _stop_external_wrapper()

async def test_does_not_adopt_when_adopt_existing_is_false(fake_launcher_external, tmp_path):
    port = await _start_external_wrapper(fake_launcher_external)
    sup = WrapperSupervisor(binary=fake_launcher_external, base_dir=tmp_path,
                            host="127.0.0.1", port=port, log_sink=lambda _: None,
                            adopt_existing=False)
    with pytest.raises(SupervisorError, match="port"):
        await sup.start()
    await _stop_external_wrapper()

async def test_login_on_an_adopted_supervisor_is_refused(fake_launcher_external, tmp_path):
    port = await _start_external_wrapper(fake_launcher_external)
    sup = WrapperSupervisor(binary=fake_launcher_external, base_dir=tmp_path,
                            host="127.0.0.1", port=port, log_sink=lambda _: None)
    await sup.start()
    try:
        with pytest.raises(SupervisorError, match="adopted"):
            await sup.login("u", "p")
    finally:
        await sup.stop()
        await _stop_external_wrapper()
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub && uv run pytest tests/test_supervisor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.wrapper_supervisor'`

- [ ] **Step 3: Implement `wrapper_supervisor.py`**

`start()` spawns `asyncio.create_subprocess_exec` with `stdout=PIPE, stderr=STDOUT`, writes credentials to stdin (never argv, never env — argv is world-readable in `/proc`), and pumps stdout into `log_sink`. Detect the 2FA prompt by scanning lines for a 2FA marker (the `wrapper/gui/main.go` `check2FA` heuristic), and resolve readiness by polling `/status` until `regions` is non-empty or `startup_timeout` (default 60 s) elapses.

Credential redaction: the line pump must scrub anything matching the credential values before calling `log_sink`. This is what `test_credentials_never_reach_the_log_sink` pins.

Auto-restart: at most `max_restarts` (default 3) with exponential backoff, then raise `SupervisorError`. Never loop forever (spec §10).

- [ ] **Step 4: Run to verify pass**

Run: `cd hub && uv run pytest tests/test_supervisor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hub/hub/wrapper_supervisor.py tests/
git commit -m "feat: supervise wrapper-lite-rootless as a child process with 2FA login"
```

---

## Task 6: The AppleMusicDecrypt seam

The single most important boundary in the codebase. Nothing else may import `src.*`.

**Files:**
- Create: `hub/hub/ripper_host.py`
- Create: `tests/test_ripper_host.py`

**Interfaces:**
- Consumes: `Settings` (Task 2); `Leaf` from `hub.jobs`.
  **Ordering: Task 7 must be executed BEFORE this task.** `Leaf` lives in
  `hub/hub/jobs.py`, which Task 7 creates. Do not re-declare `Leaf` here.
- Produces:
  - `class RipperHost` with `__init__(self, config_path: Path)`, `async def start(self) -> None`, `async def run_song(self, leaf: Leaf, *, force: bool) -> None`, `async def run_music_video(self, leaf: Leaf, *, force: bool) -> None`, `async def close(self) -> None`, `async def wrapper_status(self) -> dict`
  - `class RipperHostError(RuntimeError)`

- [ ] **Step 1: Write a failing test that proves the boundary holds**

```python
# tests/test_ripper_host.py
import ast, pathlib

def test_only_ripper_host_imports_applemusicdecrypt():
    # spec Global Constraints: the upstream tree must not be coupled to the hub
    offenders = []
    for p in pathlib.Path("hub/hub").rglob("*.py"):
        if p.name == "ripper_host.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mod = getattr(node, "module", None) or ""
            names = [a.name for a in getattr(node, "names", [])]
            if mod == "src" or mod.startswith("src."):
                offenders.append(f"{p}: from {mod}")
            if any(n == "src" or n.startswith("src.") for n in names):
                offenders.append(f"{p}: import {names}")
    assert offenders == []

def test_registers_every_creart_creator_in_dependency_order():
    src = pathlib.Path("hub/hub/ripper_host.py").read_text(encoding="utf-8")
    order = re.findall(r"add_creator\((\w+)\)", src)
    # Six, not seven. AppleMusicDecrypt/main.py also registers TaskTreeCreator,
    # which is TUI-only; the hub renders its own queue, and src/rip.py never
    # resolves it. Omitting it is deliberate.
    assert order == ["LoggerCreator", "ConfigCreator", "APICreator", "WrapperCreator",
                     "DecryptorCreator", "MeasurerCreator"]
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub && uv run pytest tests/test_ripper_host.py -v`
Expected: FAIL — `hub/hub/ripper_host.py` does not exist

- [ ] **Step 3: Implement `ripper_host.py`**

Bootstrap mirroring `AppleMusicDecrypt/main.py` exactly, in the same order — Logger → Config → API → Wrapper → Decryptor → Measurer. Registration order is load-bearing because creart's `create()` runs eagerly and touches config.

Add the vendor path once, at import time: the repo root's `AppleMusicDecrypt/` must be on `sys.path` for `import src.*` to resolve. Derive it from `Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"` and assert it exists, raising `RipperHostError` with an actionable message if not.

`run_song` translates a `Leaf` into the upstream call:

```python
url = Song(url=leaf.url, storefront=leaf.storefront, id=leaf.adam_id, type=URLType.Song)
await self._ripper.rip_song(url, leaf.codec, Flags(force_save=force, language=leaf.language))
```

`run_music_video` calls `MVRipper().rip(MusicVideo(...))`.

Wrap upstream exceptions in `RipperHostError` preserving the message — do not swallow.

- [ ] **Step 4: Run to verify pass**

Run: `cd hub && uv run pytest tests/test_ripper_host.py -v`
Expected: PASS.

- [ ] **Step 5: Add an import smoke test guarded by the vendor path**

```python
@pytest.mark.skipif(not (pathlib.Path(__file__).resolve().parents[2]
                         / "AppleMusicDecrypt" / "src").is_dir(),
                    reason="AppleMusicDecrypt checkout not present")
def test_seam_imports_cleanly():
    from hub.ripper_host import RipperHost
    assert RipperHost is not None
```

- [ ] **Step 6: Commit**

```bash
git add hub/hub/ripper_host.py tests/
git commit -m "feat: AppleMusicDecrypt seam as the only importer of src.*"
```

---

## Task 7: Job store, queue dedup, and the scheduler

**Files:**
- Create: `hub/hub/jobs.py`
- Create: `hub/hub/events.py`
- Create: `tests/test_jobs.py`

**Interfaces:**
- Consumes: `Leaf` defined here; `EventBroker` defined here.
- Produces:
  - `@dataclass class Leaf` with `adam_id: str`, `title: str`, `album_name: str`, `artist_name: str`, `codec: str`, `language: str`, `url: str`, `storefront: str`, `is_music_video: bool = False`
  - `JobStatus = Literal["queued","waiting","running","done","failed","skipped","cancelled"]`
  - `@dataclass class Job` with `id: int`, `parent_id: int | None`, `parent_url: str`, `parent_type: str`, `adam_id: str | None`, `title: str | None`, `codec: str`, `language: str`, `force: bool`, `status: JobStatus`, `skip_reason: str | None`, `progress: float | None`, `bytes_done: int | None`, `bytes_total: int | None`, `error: str | None`, `created_at: str`, `started_at: str | None`, `finished_at: str | None`
  - `@dataclass class BatchResult` with `created: list[int]`, `skipped: list[int]`, `deduplicated: list[int]`
  - `class JobStore` with `__init__(self, db_path: Path)`, `create_batch(self, parent_url: str, parent_type: str, leaves: Sequence[Leaf], *, force: bool) -> BatchResult`, `claim_next(self) -> Job | None`, `mark(self, job_id: int, status: JobStatus, **fields) -> None`, `get(self, job_id: int) -> Job | None`, `list(self, *, status: JobStatus | None = None, parent_id: int | None = None) -> list[Job]`, `resume_waiting(self) -> int`
  - `class EventBroker` with `publish(self, channel: str, data: dict) -> None` and `subscribe(self, channel: str) -> AsyncIterator[str]` (yields SSE-formatted `data: {json}\n\n`)

- [ ] **Step 1: Write the failing job tests**

```python
# tests/test_jobs.py
def leaf(adam_id="1", codec="alac"):
    return Leaf(adam_id=adam_id, title="t", album_name="A", artist_name="B",
                codec=codec, language="ja", url="https://music.apple.com/jp/song/x/1",
                storefront="jp")

def test_create_batch_deduplicates_identical_leaves(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf("1"), leaf("1"), leaf("2")], force=False)
    assert len(res.created) == 2
    assert len(res.deduplicated) == 1

def test_queue_dedup_ignores_language_and_force(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    a = store.create_batch("u", "album", [leaf("1")], force=False)
    b = store.create_batch("u", "album", [Leaf(**{**vars(leaf("1")), "language": "en-US"})], force=True)
    assert b.created == [] and len(b.deduplicated) == 1

def test_queue_dedup_key_includes_codec(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf("1", "alac")], force=False)
    res = store.create_batch("u", "album", [leaf("1", "ec3")], force=False)
    assert len(res.created) == 1

def test_a_finished_job_frees_the_dedupe_slot(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    res = store.create_batch("u", "album", [leaf("1")], force=False)
    job = store.claim_next()
    store.mark(job.id, "done")
    again = store.create_batch("u", "album", [leaf("1")], force=False)
    assert len(again.created) == 1

def test_waiting_jobs_hold_the_dedupe_slot(tmp_path):
    # spec §6: 'waiting' is in the index so a token-blocked job is not re-run
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf("1")], force=False)
    job = store.claim_next()
    store.mark(job.id, "waiting")
    assert store.create_batch("u", "album", [leaf("1")], force=False).created == []
    assert store.resume_waiting() == 1

def test_claim_next_is_exclusive(tmp_path):
    store = JobStore(tmp_path / "hub.db")
    store.create_batch("u", "album", [leaf(str(i)) for i in range(3)], force=False)
    a = store.claim_next()
    b = store.claim_next()
    assert a.id != b.id

async def test_broker_delivers_to_a_late_subscriber_the_current_snapshot():
    broker = EventBroker()
    broker.publish("jobs", {"kind": "snapshot", "jobs": []})
    got = []
    async for chunk in broker.subscribe("jobs"):
        got.append(chunk)
        break
    assert '"snapshot"' in got[0] and got[0].startswith("data: ")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd hub && uv run pytest tests/test_jobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.jobs'`

- [ ] **Step 3: Implement `jobs.py` and `events.py`**

Schema exactly as spec §6, including the partial unique index. Open with `sqlite3.connect(path, isolation_level=None)` and set `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=5000`, `PRAGMA foreign_keys=ON`.

`create_batch` must distinguish three outcomes per leaf: created, deduplicated (an active job already holds the slot), and — when `force` is true — still deduplicated, because the spec's unique index deliberately excludes `force`. Return job ids in the first two lists.

`claim_next` performs `UPDATE job SET status='running', started_at=? WHERE id = (SELECT id FROM job WHERE status='queued' ORDER BY id LIMIT 1) RETURNING *` so it is atomic under concurrency.

`EventBroker` keeps a per-channel ring buffer of the last 50 messages so a browser that connects mid-download still renders the current queue, then yields live messages.

- [ ] **Step 4: Run to verify pass**

Run: `cd hub && uv run pytest tests/test_jobs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hub/hub/jobs.py hub/hub/events.py tests/
git commit -m "feat: job store with queue dedup and an SSE event broker"
```

---

## Task 8: Resolver — flatten container URLs to leaves

**Files:**
- Create: `hub/hub/resolver.py`
- Create: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `Leaf` from `hub.jobs` (Task 7). Takes the upstream `WebAPI` instance as a parameter — never constructs its own, so tests can pass a fake.
- Produces: `async def expand(url: str, *, codec: str, language: str, web_api: WebAPI) -> list[Leaf]` and `class ResolveError(RuntimeError)`

- [ ] **Step 1: Write the failing resolver tests with a fake WebAPI**

```python
# tests/test_resolver.py
class FakeWebAPI:
    def __init__(self, albums=None, songs=None, playlists=None, artist_albums=None):
        self.calls = []
        self._albums, self._songs = albums or {}, songs or {}
        self._playlists, self._artist_albums = playlists or {}, artist_albums or {}
    async def get_album_info(self, album_id, storefront, lang): self.calls.append(("album_info", album_id)); return self._albums[album_id]
    async def get_album_tracks(self, album_id, storefront, lang, offset=0): return self._songs[album_id]
    async def get_playlist_info_and_tracks(self, pid, storefront, lang): return self._playlists[pid]
    async def get_songs_from_artist(self, aid, storefront, lang, offset=0): return self._artist_albums[aid]

SONG_URL = "https://music.apple.com/jp/album/nameless/1688539265"
ALBUM_URL = "https://music.apple.com/jp/album/nameless/1688539265"
PLAYLIST_URL = "https://music.apple.com/jp/playlist/x/pl.u-Ympg5s39LRqp"

def test_song_url_yields_one_leaf():
    leaves = asyncio.run(expand(SONG_URL, codec="alac", language="ja", web_api=FakeWebAPI()))
    assert len(leaves) == 1 and leaves[0].adam_id == "1688539274"

def test_album_url_yields_one_leaf_per_track():
    fake = FakeWebAPI(albums={"1688539265": album_info}, songs={"1688539265": album_tracks_fixture()})
    leaves = asyncio.run(expand(ALBUM_URL, codec="alac", language="ja", web_api=fake))
    assert [l.adam_id for l in leaves] == ["t1", "t2"]
    assert all(l.album_name == "nameless" for l in leaves)
    assert all(l.codec == "alac" and l.language == "ja" for l in leaves)

def test_playlist_url_preserves_order():
    ...

def test_rejects_non_https_and_unsupported_urls():
    with pytest.raises(ResolveError):
        asyncio.run(expand("http://music.apple.com/jp/album/x/1", codec="alac", language="ja", web_api=FakeWebAPI()))
    with pytest.raises(ResolveError):
        asyncio.run(expand("https://example.com/album/1", codec="alac", language="ja", web_api=FakeWebAPI()))
    with pytest.raises(ResolveError):
        asyncio.run(expand("file:///etc/passwd", codec="alac", language="ja", web_api=FakeWebAPI()))

def test_artist_url_expands_to_albums_then_tracks():
    ...

def test_empty_album_yields_no_leaves():
    ...
```

Build the fixtures from the real pydantic models: `AlbumMeta` / `AlbumTracks` from `hub`'s import of `src.models`. Simplest is to construct them via `AppleMusicURL.parse_url` plus minimal model instances that satisfy the fields the resolver reads: `data[0].attributes.name`, `.artistName`, and each track's `id` / `attributes.name` / `.albumName` / `.artistName`.

- [ ] **Step 2: Run to verify failure**

Run: `cd hub && uv run pytest tests/test_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'hub.resolver'`

- [ ] **Step 3: Implement `resolver.py`**

```python
async def expand(url: str, *, codec: str, language: str, web_api: WebAPI) -> list[Leaf]
```

Validate with `AppleMusicURL.parse_url` after an explicit `url.startswith("https://")` check (spec §11 allowlist). Dispatch on `url.type`: `Song` → one leaf; `Album` → `get_album_info` + `get_album_tracks` (paginate on `offset` while a full page returns); `Playlist` → `get_playlist_info_and_tracks` preserving order; `Artist` → `get_albums_from_artist` then recurse per album; `MusicVideo` → one leaf with `is_music_video=True`.

Never construct or hold a `WebAPI` here — it is always the injected instance, which is what keeps tests off the network.

- [ ] **Step 4: Run to verify pass**

Run: `cd hub && uv run pytest tests/test_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add hub/hub/resolver.py tests/
git commit -m "feat: flatten container URLs into per-track leaves"
```

---

## Task 9: Auth, HTTP API, and the HTMX queue UI

**Files:**
- Create: `hub/hub/auth.py`, `hub/hub/app.py`
- Create: `hub/hub/api/__init__.py`, `api/auth.py`, `api/wrapper.py`, `api/jobs.py`, `api/library.py`
- Create: `hub/hub/web/templates/*.html`, `hub/hub/web/static/app.css`
- Create: `tests/test_auth.py`, `tests/test_api_jobs.py`
- Modify: `hub/pyproject.toml` (add `fastapi`, `uvicorn[standard]`, `itsdangerous`, `jinja2`, `python-multipart`; Task 2's dependency table is the authority)

**Interfaces:**
- Consumes: `load_settings` (2), `WrapperSupervisor` (5), `RipperHost` (6), `JobStore`/`EventBroker`/`Leaf` (7), `expand` (8), `scan_roots` (3), `find_duplicate` (4).
- Produces: `hub.app.create_app(settings: Settings) -> FastAPI`; `hub.auth.SessionStore` with `issue() -> str`, `verify(token: str) -> bool`, `check_rate_limit(ip: str) -> None`; the routers listed in spec §9.

- [ ] **Step 1: Write the failing auth tests**

```python
# tests/test_auth.py
def test_password_compared_in_constant_time(monkeypatch):
    # must not use ==; assert compare_digest is actually called
    import hub.auth as auth
    called = []
    monkeypatch.setattr(auth.secrets, "compare_digest", lambda a, b: called.append((a, b)) or True)
    auth.verify_password("pw", "pw")
    assert called == [("pw", "pw")]

def test_rate_limit_blocks_after_ten_attempts():
    store = auth.SessionStore(secret=b"x" * 32, max_attempts=10, window=300.0)
    for _ in range(10):
        store.check_rate_limit("10.0.0.1")
    with pytest.raises(auth.RateLimited):
        store.check_rate_limit("10.0.0.1")

def test_token_roundtrip():
    store = auth.SessionStore(secret=b"x" * 32)
    assert store.verify(store.issue())
    assert not store.verify("forged")
```

- [ ] **Step 2: Run to verify failure, then implement `auth.py`**

Run: `cd hub && uv run pytest tests/test_auth.py -v` → FAIL, then implement.

`SessionStore` signs with `itsdangerous.URLSafeTimedSerializer`. `verify_password` uses `secrets.compare_digest`. Rate limit is a per-IP deque of timestamps. Cookies are set `httponly=True, samesite="lax"`, and `secure=True` when the request is TLS.

- [ ] **Step 3: Write the failing API tests**

```python
# tests/test_api_jobs.py  (httpx ASGI transport, no live server)
async def test_jobs_require_auth(client):
    r = await client.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    assert r.status_code == 401

async def test_health_needs_no_auth(client):
    assert (await client.get("/api/health")).status_code == 200

async def test_post_jobs_reports_created_skipped_and_deduplicated(client, fake_resolver, fake_ripper, make_library):
    r = await client.post("/api/jobs", json={"urls": [ALBUM_URL], "codec": "alac"})
    body = r.json()
    assert set(body) == {"created", "skipped", "deduplicated"}

async def test_second_identical_request_is_deduplicated(client, ...):
    ...

async def test_force_bypasses_dedup(client, ...):
    ...

async def test_status_reports_degraded_roots(client, tmp_path):
    r = await client.get("/api/status")
    assert r.json()["degraded_roots"] == [...]
```

- [ ] **Step 4: Implement the routers and app factory**

`create_app` wires singletons on `app.state` in a lifespan context: settings, `EventBroker`, `JobStore`, `WrapperSupervisor`, `RipperHost`. Startup order: start the supervisor, wait for readiness, then start `RipperHost`, then start the download scheduler task.

**A successful login must be followed by a supervisor restart.** A wrapper that is already
serving does not pick up an account logged in afterwards — the token cache is read at
process start. So the login flow is: `POST /api/wrapper/login` (and `/2fa` if challenged),
then `stop()` + `start()`, and only then report success. Reporting success on the login
call alone would leave the user with a serving-but-unauthenticated wrapper, which is
indistinguishable from a broken one.

**A wrapper reporting `regions: []` is not ready and must not be shown as healthy.** The
supervisor's two failure messages are distinct — "no account logged in" versus "did not
become ready" — and the UI must pass that distinction through rather than collapsing both
into "unavailable".

`POST /api/jobs` flow, which is the spec §7.3 algorithm in its final form:

1. `expand` each URL into leaves
2. `create_batch` → created / deduplicated
3. return the three lists

**`create_batch` raises `ValueError` if any leaf is unusable, and applies the earlier
leaves before raising.** That is deliberate — partial application is reported, never
silently swallowed — but it means a 19-track album with one bad leaf must not surface as a
500. The handler catches `ValueError`, calls `store.list(parent_url=…)` to see what actually
landed, and answers 200 with the partial result plus a `rejected` list naming the leaf.

**Filter by `parent_url`, not `parent_id`.** The spec's `parent_id` is a self-reference that
nothing writes — `create_batch` has no such parameter — so filtering by it is impossible and
`list(parent_id=None)` means *no filter*, which would render the entire queue instead of this
batch. `list()` therefore needs a `parent_url` filter; `parent_id` stays for the day something
writes it.
`POST /api/jobs` therefore answers `{created[], skipped[], deduplicated[], rejected[]}`.

**`find_duplicate` must be given a *rendered filename*, never a tag title.**
Normalize is not idempotent — 6 of the 8,721 real library keys are not fixed points
(`1-01 1 a.m. (feat. shinoだす。).m4a` keys to `1 a.m. (feat. shinoだす。)`, and
normalizing that again drops the leading `1 `). Passing a tag title double-normalizes
and mis-keys those. Render the candidate the same way `rip_song` will write it, through
`get_song_name_and_dir_path` + `get_suffix`, and pass exactly that.

Per-job execution, in the scheduler task: `claim_next` → if `force` skip the check, else
`scan_roots(settings.library_roots)` then `find_duplicate(...)`. The `track_title` argument
is the **rendered output filename**, produced by the same
`get_song_name_and_dir_path(codec, metadata)` + `get_suffix(codec, atmosConventToM4a)` pair
`rip_song` will use — not the tag title, because normalize is not idempotent and the two
differ for 6 real keys. On a hit, `mark(job_id, "skipped", skip_reason=f"duplicate:{'|'.join(hit.matched)}")`
and publish to the broker; otherwise delegate to `RipperHost.run_song` /
`run_music_video` and `mark` the terminal status.

`hit.matched` must reach `skip_reason` **intact**. It is the only thing that makes a `loose`
skip adjudicable by a human, and the queue UI is specified to render those paths verbatim.

Expose `GET /api/jobs/stream` as an SSE `StreamingResponse` over `EventBroker.subscribe("jobs")`.

Templates: `base.html`, `login.html`, `queue.html`. The queue table shows status, title, progress, and for skipped rows the matched paths verbatim so a human can adjudicate (Review Focus #3).

- [ ] **Step 5: Run to verify pass**

Run: `cd hub && uv run pytest -v` → all pass.

- [ ] **Step 6: Commit**

```bash
git add hub/
git commit -m "feat: auth, JSON API, and the HTMX queue UI"
```

---

## Task 10: Container image, compose, and Phase 1 acceptance

**Files:**
- Create: `Dockerfile`, `compose.yaml`, `.env.example`, `README.md`
- Modify: `AGENTS.md` (add the `amd-hub` section described in Step 5)

**Interfaces:**
- Consumes: everything above.
- Produces: a running `amd-hub` on `:8080`.

- [ ] **Step 1: Write the multi-stage Dockerfile**

Stage 1 mirrors `wrapper/Dockerfile`: `debian:13.2`, install `build-essential cmake unzip git lsb-release gnupg aria2`, install LLVM, fetch and unzip NDK r23b **before** `COPY wrapper/`, then `cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j$(nproc)`. Emit `/out/wrapper-lite-rootless` and `/out/rootfs`.

Stage 2: `python:3.13-slim`, install `curl` and `ffmpeg` (Phase 3 needs it; cheap now),
`COPY --from=stage1 /out /opt/wrapper`, `COPY hub/ /app/hub`, install with `uv`,
`COPY AppleMusicDecrypt /app/AppleMusicDecrypt`, `ENV PYTHONPATH=/app/hub`, `EXPOSE 8080`.

**Two corrections, both fatal, both found by the Task 10 acceptance run:**

- **`PYTHONPATH` is `/app/hub`, not `/app`.** `/app/hub/` has no `__init__.py`, so `/app` on the
  path makes `import hub` find the *project directory* — a namespace package whose `__path__` is
  `['/app/hub']`. `hub.app` never resolves and the container dies at `CMD`. The value that works is
  the directory that *contains* the package. This fails at the first `docker compose up`, not at
  build time, which makes it an expensive mistake.
- **The client goes to `/app/AppleMusicDecrypt`, not `/opt/`.** `ripper_host.py::_VENDOR_ROOT` and
  `app.py::vendor_config_path` are both `Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"`.
  There is no environment variable and no argument for it. `/opt/AppleMusicDecrypt` produces a
  `RipperHostError` naming a path that exists and is not the one the derivation wants — at every
  boot, after a build that reported success.

`APP_LIBRARY_ROOTS` must default to the first writable root, and
`AMD_WRAPPER_BINARY=/opt/wrapper/wrapper-lite-rootless`.

**Two deployment constraints the seam established, which the image must honour:**

- The upstream config is effectively pinned to `<vendor>/config.toml`. `ConfigCreator` calls
  `load_from_config()` with its own default and there is no upstream hook, so bind-mounting a
  hub-owned `config.toml` anywhere else would mean a *different* file is read than the one the
  caller named. Either ship `config.toml` inside `AppleMusicDecrypt/` and edit it there, or
  mount over `<vendor>/config.toml` itself. Do not mount it somewhere else and assume it works.
- `RipperHost` `chdir`s into `AppleMusicDecrypt/` for its lifetime because upstream loads
  `config.toml` by relative path. Every path the image passes must therefore be **absolute**;
  a relative `ENV` or working directory will resolve against the vendor tree.
- `hub/pyproject.toml` carries upstream's dependency pins individually, not as a path
  dependency, because `src.*` must keep resolving through the seam's `sys.path` insert. That is
  a deliberate maintenance coupling: an upstream bump needs both lists moved together.

- [ ] **Step 2: Write `compose.yaml` and `.env.example`**

Per spec §14: one service, publish 8080 only, mount both library roots,
`security_opt: [seccomp:unconfined, systempaths=unconfined]`, healthcheck against `/api/health`.

**The Apple token DB is not on `/data`.** The launcher `chroot`s *before* resolving `--base-dir`,
so `/data/wrapper` means `<rootfs>/data/wrapper` inside the chroot and the volume never sees the
account. It must be `wrapper-data:/opt/wrapper/rootfs/data`. Observed with it missing: the launcher
logs `mkdir base_dir_arg failed` and the Apple account dies on every `down`/`up`.

`security_opt` needs **both** entries: `seccomp:unconfined` alone gives `mount proc failed: EPERM`,
and `cap_add: [SYS_ADMIN]` was a control experiment proving it cannot help. `cap_add` stays absent. The NTFS root must be a commented-out override so the stack still starts when the drive is absent (Review Focus #4).

- [ ] **Step 3: Build and smoke-test**

```bash
docker compose build
docker compose up -d
curl -fsS localhost:8080/api/health
```
Expected: `200` with `{"status":"ok"}`.

- [ ] **Step 4: Phase 1 acceptance run**

1. `docker compose logs -f amd-hub` and complete the Apple login, including 2FA, in the browser.
2. `GET /api/status` shows the wrapper ready and `degraded_roots` matching reality.
3. Request one album. Every leaf downloads; progress appears over SSE.
4. Request the same album again. Every leaf is `skipped` and each `skip_reason` names a real path.
5. Request a track whose title also exists in a different album. It downloads (not skipped).
6. Unmount the NTFS drive, restart, confirm the UI says degraded rather than silently re-downloading.

- [ ] **Step 5: Update `AGENTS.md` and add `README.md`**

`AGENTS.md` gains an `amd-hub` section: the two clones are gitignored (not submodules), `hub/` is the only code we own, the seam rule (`ripper_host.py` is the only `src.*` importer, enforced by a test), and the review-focus hazards. `README.md` documents `cp .env.example .env`, `docker compose up`, and the 2FA flow.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile compose.yaml .env.example AGENTS.md README.md
git commit -m "feat: multi-stage image, compose stack, and Phase 1 acceptance"
```

## Execution Order (revised by pre-flight scan)

The plan originally listed Task 6 (the seam) before Task 7 (jobs). **Execute Task 7 first.**
`Leaf` is defined in `hub/hub/jobs.py`, which Task 7 creates and Task 6 imports; the
original order had two tasks creating the same file. The dispatch order is:

**1 → 2 → 3 → 4 → 5 → 7 → 6 → 8 → 9 → 10**

Everything else is unchanged. Tasks 5 and 7 are independent of each other and of 2–4, so if
the implementer for Task 5 is blocked on the Task 1 build, Task 7 may proceed in parallel —
but never two implementers writing the same file.

---

## Self-Review

**Spec coverage.** §1–§2 goals 1–4 land in Tasks 7–9; goals 5–6 (library browsing, playback) are Phase 2/3 by design per spec §13. §3 topology in Task 10, §3.1 single-container in Task 5, §3.2 layer caching in Task 10. §4 layout in Task 10 Step 5. §5 components across Tasks 2–9; §5.1 flattening in Task 8. §6 schema in Task 7. §7 all of Task 3 + Task 4. §8 read-only listing in Task 9 (`library.py`), duplicate report is Phase 2. §9 endpoints in Task 9. §10 error handling in Tasks 5, 7, 9. §11 security in Task 9 (auth, path-traversal by ID, https allowlist in Task 8, credentials in Task 5). §12 tests throughout; §12.1 fixtures in Tasks 3–4. §14 compose in Task 10.

**Gaps I am accepting deliberately.** The duplicate *report* endpoint (`GET /api/library/duplicates`) is Phase 2; only its backing scan exists after Task 3. The `.part` exclusion is specified and tested in Task 3 but §8's library listing also needs it — noted in Task 9.

**Type consistency.** `Leaf` is produced by Task 7 and consumed by Tasks 6 and 8; Task 6 says to declare it if Task 7 has not landed. `find_duplicate` and `scan_roots` signatures are fixed in Tasks 3–4 and used verbatim in Task 9. `DuplicateHit.matched` is a `tuple[str, ...]` in Task 4 and Task 9 joins it with `|`.

**Proportion.** 10 tasks, ~5 steps each, dominated by test assertions and signatures rather than implementation bodies.
