# Portable Library Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the last machine-specific absolute paths from the shipped tree so the stack deploys on a machine that is not this one, with no external drive.

**Architecture:** Host-specific values fall into three tiers, and treating them as one problem is what kept them returning. Repo-relative paths are derived from `parents[2]` (the convention `ripper_host._VENDOR_ROOT` already follows). Operator-owned paths become required with no default, failing at startup by name (the policy `config.py`'s own module docstring already states, and `AMD_PASSWORD` already follows). Container-internal paths are named in exactly one place, `AMD_DOWNLOAD_ROOT`, from which both the runtime env and the baked `dirPathFormat` derive.

**Tech Stack:** Python 3.13 / pydantic v2, pytest + pytest-asyncio, ruff, Docker Compose v2, bash.

**Spec:** `docs/superpowers/specs/2026-09-27-portable-library-config-design.md` — the plan argues from the spec, so the spec travels with it; executors read both.

## Global Constraints

- Comment and commit message language is **English**, per the global `~/.config/opencode/AGENTS.md`. This plan's prose is Japanese only where the spec's replacement text is Japanese; the `.env.example` block in Task 5 is the one place replacement copy is English, because the file is already entirely English.
- Conventional Commits prefixes: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`.
- **Never modify anything under `AppleMusicDecrypt/` or `wrapper/`.** They are pinned submodules; a local edit is invisible to `git status` at the root and lost on the next `submodule update`.
- **Never modify anything under `docs/superpowers/findings/**`.** Those are verbatim transcripts of command output from 2026-09-26/27.
- The working tree carries **77 uncommitted lines across 15 files** that are a deliberate repo-wide `apple-dl_extend` → `amdl_extend` rename. `git add` in any step must name paths explicitly. **Never `git add -A`, `git add .`, or `git commit -a`** — those would sweep it into an unrelated commit.
- `hub/spike/` is git-tracked and lint-exempt (`pyproject.toml:108`). Do not add it back to lint.
- Substrate facts, all measured — do not re-derive, and do not "fix" them if a run disagrees without reporting:
  - `load_settings` is called 22 times under `hub/tests/`; 17 lack `AMD_LIBRARY_ROOTS` and **all 17 are in `hub/tests/test_config.py`**.
  - `Settings(` is constructed **0** times in tests; `test_api_jobs.py:324`'s `settings` fixture passes `AMD_LIBRARY_ROOTS` at line 329.
  - `create_app()` is called with no arguments **0** times in tests, so `hub/hub/app.py:789`'s `load_settings()` fallback is not exercised.
  - The suite is **645 tests**, all passing, 0 skipped, before any change.
  - Compose's `:?` form renders as `required variable <NAME> is missing a value: <message>`, so the message must **not** repeat the variable name.
- No step may add a dependency, and no step may relax `test_deployment.py:1125-1131`'s property-not-mechanism stance for the two persistent trees.

## Review Focus

Five input classes the spec implies that no existing test exercises. Each line names a condition and the behaviour a reasonable person would expect; the task that owns the code carries the test.

1. **`AMD_LIBRARY_HOST` points at a directory that does not exist.** The user follows the README exactly on a fresh machine and runs `docker compose up -d` before creating anything. Expectation: a failed start naming `AMD_LIBRARY_HOST`, never an empty library that reads as healthy. Pinned in Task 4.
2. **`AMD_LIBRARY_HOST` points at a plain directory on a machine with no external drive at all** — the primary target user. Expectation: the stack starts and the scan finds the tree. Pinned in Task 4 by asserting the compose file names no drive, UUID, or filesystem type anywhere in its non-comment lines.
3. **`AMD_LIBRARY_ROOTS` unset while `AMD_PASSWORD` is set**, in a bare `docker run` that bypasses compose. Expectation: `RuntimeError` naming `AMD_LIBRARY_ROOTS` and showing the format, not a silent empty scan. Pinned in Task 2.
4. **Neither `AMD_PASSWORD` nor `AMD_LIBRARY_ROOTS` set.** Expectation: the message names `AMD_PASSWORD`, because that is the one the operator is likelier to have meant. Pinned in Task 2.
5. **The repository is cloned or moved to a different absolute path.** Expectation: no file in the shipped tree names the old path, and `ripper_host._VENDOR_ROOT` / `app.vendor_config_path()` still resolve. Pinned in Task 3 as a repo-wide grep with an explicit exclusion list, and re-checked in Task 5.

---

### Task 1: Pin the required-variable contract before changing it

Two settings are required and both fail by name, so the order between them is a contract rather than an implementation detail. This task writes the tests that pin it and nothing else; no production code changes, so the only behaviour under test is the ordering and the message's content.

**Files:**
- Modify: `hub/tests/test_config.py` (add three tests after `test_defaults_match_the_spec`, which ends at line 27)
- Test: `hub/tests/test_config.py`

**Interfaces:**
- Consumes: `hub.config.load_settings(env: Mapping[str, str] | None) -> hub.config.Settings`, and `hub.config.Settings.rip_concurrency: int`.
- Produces: three test names that Tasks 2 and 3 keep green — `test_requires_library_roots`, `test_the_missing_library_roots_message_says_how_to_set_it`, `test_the_password_is_still_checked_before_the_library_roots`.

- [ ] **Step 1: Write the failing tests**

Insert after `test_defaults_match_the_spec` in `hub/tests/test_config.py`:

```python
def test_requires_library_roots():
    # No default. The two roots this default was sized for no longer both exist, and the
    # surviving one points into a directory upstream gitignores, so a fresh clone would
    # scan nothing and report nothing -- the silent-empty-root failure the deployment
    # notes are most careful about.
    with pytest.raises(RuntimeError, match="AMD_LIBRARY_ROOTS"):
        load_settings({"AMD_PASSWORD": "x"})


def test_the_missing_library_roots_message_says_how_to_set_it():
    # A message that only names the variable leaves the operator to guess the format, and
    # the format is a comma-separated list of *container* paths -- not the host path they
    # put in AMD_LIBRARY_HOST. Both halves are asserted because the point of this change is that
    # a new user can get it right without reading the source.
    with pytest.raises(RuntimeError) as excinfo:
        load_settings({"AMD_PASSWORD": "x"})
    message = str(excinfo.value)
    assert "AMD_LIBRARY_ROOTS" in message
    assert "comma-separated" in message


def test_the_password_is_still_checked_before_the_library_roots():
    # Both required settings fail this way, so the order is a contract: a caller who has
    # set neither must be told about the one it is more likely to have meant. This is what
    # keeps the two existing password tests honest -- without it they would pass for the
    # wrong reason and stop testing the password.
    with pytest.raises(RuntimeError, match="AMD_PASSWORD"):
        load_settings({})
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd hub && uv run pytest tests/test_config.py -k "library_roots or checked_before" -v`

Expected: `test_requires_library_roots` and `test_the_missing_library_roots_message_says_how_to_set_it` **FAIL** with `DID NOT RAISE`, because `load_settings({"AMD_PASSWORD": "x"})` currently succeeds by falling back to the default. `test_the_password_is_still_checked_before_the_library_roots` **PASSES** already, and that is the point: it is the control that proves the other two fail for the right reason.

- [ ] **Step 3: Commit**

```bash
git add hub/tests/test_config.py
git commit -m "test: require AMD_LIBRARY_ROOTS and pin the check order

The two required settings both fail by name, so the order between them is
a contract rather than an implementation detail. These fail today, which
is the point: load_settings currently falls back to a default instead."
```

---

### Task 2: Make the roots required and delete the defaults

**Files:**
- Modify: `hub/hub/config.py` (remove lines 19-25; change `_paths` at 102-111; add the check in `load_settings` at 183-200)
- Modify: `hub/tests/test_config.py` (add `AMD_LIBRARY_ROOTS` to the 15 call sites listed below; delete `test_default_library_roots_are_the_two_spec_libraries` at 30-40)
- Test: `hub/tests/test_config.py`

**Interfaces:**
- Consumes: the three test names from Task 1.
- Produces: `_required_paths(env: Mapping[str, str], key: str, example: str) -> list[Path]`, a module-level function in `hub/hub/config.py`. Tasks 3-5 refer to it only through `load_settings`; no later task calls it directly.

- [ ] **Step 1: Delete the default constant and its comment**

In `hub/hub/config.py`, delete lines 19-25 in full — the comment block beginning `# §7.2's two libraries, which exist on the host this was developed against` and the `DEFAULT_LIBRARY_ROOTS` tuple. The `DEFAULT_BIND` assignment on line 26 becomes the first constant in the file.

- [ ] **Step 2: Add the required-path reader**

Immediately after `_paths` (which ends at line 111), add:

```python
def _required_paths(env: Mapping[str, str], key: str, example: str) -> list[Path]:
    """The same parsing as `_paths`, with no fallback.

    A default for one of these is a host-specific path baked into the source, and it is
    wrong on every machine but the one it was written on. The message carries `example`
    so the operator can see the format rather than infer it -- and `example` is a
    *container* path, because that is what this list holds, not the host path the
    operator thinks in terms of.
    """
    roots = _paths(env, key, ())
    if not roots:
        raise RuntimeError(
            f"{key} is unset. Name every host directory that holds your music library, "
            f"comma-separated, e.g. {key}={example}"
        )
    return roots
```

Note the call passes `()` as the default, so `_paths` returns `[]` for an unset variable and the `key is set but contains no usable path` branch inside `_paths` keeps its own distinct message. A variable set only to whitespace is treated as unset, because `_text` strips.

- [ ] **Step 3: Wire it into `load_settings`, after the password check**

In `load_settings`, the password block ends at line 181. Insert this block after it, so the password is always reported first:

```python
    # Both of these are required, and the order matters: a caller who has set neither
    # should be told about the password, which is the one it is more likely to have
    # meant. `test_the_password_is_still_checked_before_the_library_roots` pins this.
    library_roots = _required_paths(source, "AMD_LIBRARY_ROOTS", "/library")
```

Then change the `library_roots=` line in the `Settings(...)` call from
`library_roots=_paths(source, "AMD_LIBRARY_ROOTS", DEFAULT_LIBRARY_ROOTS),`
to
`library_roots=library_roots,`

- [ ] **Step 4: Add the variable to the existing test call sites**

In `hub/tests/test_config.py`, add `"AMD_LIBRARY_ROOTS": "/library"` to the dict of every `load_settings({...})` call that does not already pass it, **except** these two:

- `test_requires_password` (line 8)
- `test_requires_password_when_the_variable_is_absent` (line 15)

Those two must keep passing nothing but `AMD_PASSWORD`, so that `pytest.raises(..., match="AMD_PASSWORD")` keeps proving the password is checked and not something else. The dict-literal sites are lines 24, 36, 54, 55, 63, 72, 76, 79, 84, 93, 100, 105, 112, 114.

For the bare `load_settings()` at line 46, which reads `os.environ`, add to the same test's `monkeypatch` block:

```python
    monkeypatch.setenv("AMD_LIBRARY_ROOTS", "/library")
```

- [ ] **Step 5: Delete the obsolete test**

Delete `test_default_library_roots_are_the_two_spec_libraries` (lines 30-40), comment included. There is no default left to pin. The intent it carried — that the container mount points must not become the local default — is already held by `hub/tests/test_deployment.py:1075`, which asserts no `/library/` target other than the real one is mounted.

- [ ] **Step 6: Run the module to verify it passes**

Run: `cd hub && uv run pytest tests/test_config.py -v`

Expected: **18 passed** (the 15 survivors plus the 3 from Task 1).

- [ ] **Step 7: Run the full suite to confirm nothing else depended on the default**

Run: `cd hub && uv run pytest -q`

Expected: **645 passed**. The count is unchanged from before this plan started, because Task 1 added 3 tests and this step removes 1 (`test_default_library_roots_are_the_two_spec_libraries`) while Task 1's 3 already exist on top of the 645 baseline. If the number differs, the difference is the net of those edits and nothing else — report it rather than adjusting the expectation.

- [ ] **Step 8: Commit**

```bash
git add hub/hub/config.py hub/tests/test_config.py
git commit -m "fix: require AMD_LIBRARY_ROOTS instead of defaulting to host paths

DEFAULT_LIBRARY_ROOTS is deleted rather than repointed. One of the two
roots it named no longer exists, and the survivor points into a directory
upstream gitignores, so a fresh clone scanned nothing and said nothing.
That reverses a recorded decision (2026-09-26-normalize-report.md:231)
whose stated reason no longer holds.

The password check stays first so that a caller who has set neither is
told about the one it is more likely to have meant."
```

---

### Task 3: Rename the container mount point to `/library`

`b` was the second of two roots; `/library/a` stopped being mounted when the drive became the only library, and the letter outlived the thing it indexed. The rename is one value in one place, and the runtime env and the baked `dirPathFormat` both derive from it.

**Files:**
- Modify: `compose.yaml:29` (`AMD_DOWNLOAD_ROOT`), `compose.yaml:112` (mount `target`), `compose.yaml:170` (`AMD_LIBRARY_ROOTS`)
- Modify: `Dockerfile:12` (the `ARG AMD_DOWNLOAD_ROOT` default)
- Modify: `hub/hub/spike/task4_real_library_check.py:16-19`
- Modify: `hub/tests/test_deployment.py:550, 604, 1072, 1073, 1075, 1112, 1139, 1560, 1582`
- Test: `hub/tests/test_deployment.py`

**Interfaces:**
- Consumes: `load_settings` from Task 2. `hub.config._required_paths` is private to Task 2 and is not called here.
- Produces: no new names. The invariant produced is that the container-side mount point is `/library`, stated in `AMD_DOWNLOAD_ROOT` and nowhere else.

- [ ] **Step 1: Change the three compose occurrences and the Dockerfile default**

`compose.yaml:29` — `        AMD_DOWNLOAD_ROOT: /library/b` becomes `        AMD_DOWNLOAD_ROOT: /library`

`compose.yaml:112` — `        target: /library/b` becomes `        target: /library`

`compose.yaml:170` — `      AMD_LIBRARY_ROOTS: /library/b` becomes `      AMD_LIBRARY_ROOTS: /library`

`Dockerfile:12` — `ARG AMD_DOWNLOAD_ROOT=/library/b` becomes `ARG AMD_DOWNLOAD_ROOT=/library`

Change **nothing else**. `Dockerfile:302`'s `ENV AMD_LIBRARY_ROOTS=${AMD_DOWNLOAD_ROOT}` and the `dirPathFormat` seds already interpolate `${AMD_DOWNLOAD_ROOT}`; writing `/library` into either of them would create the second source of truth that `hub/tests/test_deployment.py:1519-1547` exists to prevent.

- [ ] **Step 2: Update the test literals**

In `hub/tests/test_deployment.py`, replace `/library/b` with `/library` at lines 550, 604, 1072, 1073, 1112, 1139, 1560, 1582.

Line 1075 needs a different edit, because it is the one assertion that *excludes* library mounts:

```python
    assert not any(t.startswith("/library/") and t != "/library" for t in targets), targets
```

Leave the prose in the docstrings at 162-168, 180, and 1499-1503 for Task 5 — they are comments, and Task 5 owns all comment work.

- [ ] **Step 3: Rewrite the spike's root list**

`hub/spike/task4_real_library_check.py` currently reads:

```python
ROOTS = [
    Path("/home/m/amdl_extend/AppleMusicDecrypt/downloads"),
    Path("/run/media/m/1A5E05A75E057D2F/Music"),
]
```

Replace it with a read of the live setting — this script is a check against whatever configuration is actually running, so a hardcoded pair is the wrong shape for it — and add `import os` to the imports:

```python
ROOTS = [
    Path(part.strip())
    for part in os.environ.get("AMD_LIBRARY_ROOTS", "").split(",")
    if part.strip()
]
if not ROOTS:
    raise SystemExit(
        "set AMD_LIBRARY_ROOTS to the roots to check, e.g. AMD_LIBRARY_ROOTS=/library"
    )
```

- [ ] **Step 4: Run the deployment tests**

Run: `cd hub && uv run pytest tests/test_deployment.py -v`

Expected: **32 passed**, 0 skipped. `test_the_library_root_is_mounted_through_the_symlink_and_not_resolved` still passes here because Task 4 has not yet changed `AMD_LIBRARY_HOST`'s syntax — it is Task 4 that replaces it.

- [ ] **Step 5: Run the full suite and lint**

Run: `cd hub && uv run pytest -q && uv run ruff check .`

Expected: **645 passed**, and `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add compose.yaml Dockerfile hub/hub/spike/task4_real_library_check.py hub/tests/test_deployment.py
git commit -m "refactor: rename the library mount point from /library/b to /library

b was the second of two roots. /library/a stopped being mounted when the
drive became the only library, and the letter outlived the thing it
indexed -- which is a question every reader of the compose file has to
ask. AMD_DOWNLOAD_ROOT is the single place that names it, and both the
runtime env and the baked dirPathFormat derive from it, so this is one
value in one file."
```

---

### Task 4: Make `AMD_LIBRARY_HOST` required so no host path ships in the compose file

A default here is a host-specific path baked into a file every deployment reads, and it is wrong on every machine but the one it was written on. The two tests that currently pin it — one of which asserts a personal directory name as a literal — are replaced rather than updated.

**Files:**
- Modify: `compose.yaml:111` (the bind `source`)
- Modify: `hub/tests/test_deployment.py:1085-1112`, `:1550-1571`
- Test: `hub/tests/test_deployment.py`

**Interfaces:**
- Consumes: the compose file from Task 3, whose `AMD_LIBRARY_HOST` source is at line 111 and whose `/library` target is at line 112.
- Produces: no new code names. The invariant produced is that the compose file's non-comment lines contain no `/home/m/` and no `/run/media/`.

- [ ] **Step 1: Make the variable required**

`compose.yaml:111` — replace

```yaml
        source: ${AMD_LIBRARY_HOST:-/home/m/Music/HDD_Music}
```

with

```yaml
        source: ${AMD_LIBRARY_HOST:?set AMD_LIBRARY_HOST in .env to the host directory that holds your music library}
```

The message deliberately does not name `AMD_LIBRARY_HOST`: compose renders the `:?` form as `required variable AMD_LIBRARY_HOST is missing a value: <message>`, so naming it again would print it twice. Measured, not assumed.

`create_host_path: false` on line 114 is unchanged. It is the property that turns a missing directory into a failed start instead of a directory Docker created and mounted as an empty library that reads as healthy — and it is what makes Review Focus 1 and 2 safe.

- [ ] **Step 2: Write the replacement test, then delete the two it replaces**

In `hub/tests/test_deployment.py`, delete `test_the_external_drive_is_mounted_by_the_base_compose_and_cannot_be_invented` (lines 1550-1571) and `test_the_library_root_is_mounted_through_the_symlink_and_not_resolved` (lines 1085-1112). In their place:

```python
def test_the_library_directory_is_required_and_nothing_defaults_to_this_host():
    """No host path may ship in the compose file, and no default may stand in for one.

    The old test read the default straight out of the compose file and asked whether it
    was a symlink *on the machine running the suite*, skipping when it was not — so on
    any other machine it inspected nothing. That is a worse test than this one for the
    same property, because it can pass without having checked.

    The property is now a property of the file: the operator's library path lives in
    `.env`, and the file must therefore carry no host-specific value to fall back on.
    """
    raw = COMPOSE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("#")
    )
    assert "/run/media/" not in code, (
        "the mount source is udev's automount target, which disappears on unmount and "
        "changes when the volume is reformatted, so a file naming it silently rots"
    )
    for personal in ("/home/m/", "HDD_Music"):
        assert personal not in code, (
            f"{personal!r} is a path from the machine this was written on; the operator's "
            "library directory belongs in .env, and a default here is wrong everywhere else"
        )
    binds = [m for m in _mounts(_service(_compose(COMPOSE))) if m["target"] == "/library"]
    assert binds, "the library must be mounted, whatever host directory it comes from"
    assert binds[0]["create_host_path"] is False, (
        "without create_host_path: false, a directory that does not exist yet becomes one "
        "Docker creates on the host, and an empty root reads as healthy"
    )
    assert ":?" in binds[0]["source"], (
        f"AMD_LIBRARY_HOST must be required (:?) rather than defaulted: got {binds[0]['source']!r}"
    )
```

`_compose` is `yaml.safe_load` over the file's text (line 154) and never shells out to `docker compose`, so the `${AMD_LIBRARY_HOST:?...}` in `source` is never interpolated and the test needs no `monkeypatch` and no `AMD_LIBRARY_HOST` in the environment. Nothing else in this file resolves that interpolation either, so `:?` is safe to assert on directly.

- [ ] **Step 3: Run the deployment tests**

Run: `cd hub && uv run pytest tests/test_deployment.py -v`

Expected: **31 passed** — one fewer than Task 3's 32, because two tests were replaced by one.

- [ ] **Step 4: Verify the failure mode is the intended one, on a real compose run**

Run, in a scratch directory, so nothing in the repo is involved:

```bash
cd "$(mktemp -d)" && cp /home/m/amdl_extend/compose.yaml . && \
  printf 'AMD_PASSWORD=x\n' > .env && \
  docker compose config 2>&1 | head -3
```

Expected: a line containing `required variable AMD_LIBRARY_HOST is missing a value` and the message text from Step 1. If it prints something else, the `:?` did not take effect — stop and report rather than proceeding.

- [ ] **Step 5: Run the full suite and lint**

Run: `cd hub && uv run pytest -q && uv run ruff check .`

Expected: **644 passed** (645 minus the one net test removal in Step 3), and `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add compose.yaml hub/tests/test_deployment.py
git commit -m "fix: require AMD_LIBRARY_HOST rather than defaulting to this host's drive

A default here is a host-specific path baked into a file every deployment
reads, and it is wrong on every machine but this one. The message does not
repeat the variable name: compose already renders it as 'required variable
AMD_LIBRARY_HOST is missing a value: <message>'.

The test it replaces read the default out of the compose file and asked
whether it was a symlink on the machine running the suite, skipping when
it was not -- so elsewhere it inspected nothing. The replacement is a
property of the file: no host path, no default."
```

---

### Task 5: Rewrite the documentation that still describes the old deployment

`mutation_check.py:79-83` targets a string this task deletes, and a mutation whose target no longer exists is a mutation that applies nothing. It has already happened once — `docs/superpowers/findings/2026-09-27-deployment-report.md:727` records a mutation that "did not apply" for exactly that reason. Retarget it in the same change that removes its target.

**Files:**
- Modify: `.env.example` (lines 43-48, 58-69, 66)
- Modify: `README.md:39`, `README.md:96`
- Modify: `hub/deploy/build_gate.py` (comment only, lines 172-179)
- Modify: `hub/deploy/acceptance_check.py` (comment and docstring only, lines 10 and 87)
- Modify: `hub/tests/test_deployment.py` (docstrings only, lines 162-168, 180, 1499-1503)
- Modify: `hub/hub/library_scan.py` (comment only, line 193)
- Modify: `hub/hub/normalize.py` (comment only, line 14)
- Modify: `hub/tests/test_library_scan.py` (comments only, lines 258, 259, 347)
- Modify: `hub/tests/test_normalize.py` (comment only, line 189)
- Modify: `hub/deploy/mutation_check.py:79-83`
- Test: `hub/tests/test_deployment.py` (already covers the required strings), `hub/deploy/mutation_check.py`

**Interfaces:**
- Consumes: the compose file and `test_deployment.py` from Tasks 3 and 4. The string `TODO(spec \u00a78.1, Phase 2)` must survive this task verbatim — `test_deployment.py:589` asserts it is present and line 593 asserts the block names `contain`, `create_app`, `dirPathFormat`, and `AMD_LIBRARY_ROOTS`.
- Produces: no code names. Two new mutation entries in `mutation_check.py`, whose target strings are the ones `.env.example` and `compose.yaml` must then be free of.

- [ ] **Step 1: Delete the `/library/a` block from `.env.example`**

Delete lines 43-48, which are the `*** /library/a MUST BE IN THE LIST. ***` paragraph and its five continuation lines, leaving the `#` separator on line 49 in place so the TODO block that follows keeps its leading blank comment line.

- [ ] **Step 2: Replace the drive-must paragraph and the value**

Delete lines 58-69 — the `The library is the external drive, and it is the only root.` paragraph, the `This value should be /library/b and nothing else.` paragraph, the `AMD_LIBRARY_ROOTS=/library/b` line, and the `There is no second root to add.` comment — and replace them with:

```
# One entry: /library, the container path the client writes into and the hub scans. It is
# baked into the image as the client's `dirPathFormat` from the same build argument, so it
# and this list cannot disagree -- see AMD_DOWNLOAD_ROOT in compose.yaml. Order does not
# matter: `scan_roots` builds one index across every root.
#
# Do not add a second root casually. An unreadable root is reported as degraded, but a
# root that is a silently empty mount point is not, so an added root that is mounted and
# empty is invisible. The library page shows a per-root album count for exactly that case.
#
# The host side of this mount is `AMD_LIBRARY_HOST` below. The two are different kinds of path:
# this one is inside the container and is the same on every machine, and `AMD_LIBRARY_HOST` is on
# the host and is yours.
AMD_LIBRARY_ROOTS=/library

# The host directory holding your music library. Required, with no default: there is no
# path on your machine this file could guess, and a wrong guess is an empty library that
# reads as healthy. Any directory works -- it does not have to be a separate drive, and
# it does not have to be NTFS.
#
# Point it at a path you manage, not at /run/media/<UUID>/. That is udev's automount
# point: it disappears on unmount and changes when the volume is reformatted, so a
# compose file naming it is a file that silently rots. A symlink you own is the stable
# name, and Docker resolves it before the mount exists inside the container.
#
# compose refuses to create the host path (`create_host_path: false`), so an absent
# directory is a failed start with a message naming AMD_LIBRARY_HOST, rather than a directory
# Docker quietly created and mounted as an empty library.
#
# AMD_LIBRARY_HOST=/home/you/Music
```

`test_deployment.py:589-594` requires the word `contain` inside the TODO block, and Step 1 does not touch the TODO block, so that assertion still has a target. Run Step 6 to confirm rather than reasoning about it.

- [ ] **Step 3: Update `README.md`, including the local-run note**

Line 39's `ls -L /home/m/Music/HDD_Music >/dev/null && echo mounted   # check first; see below` becomes `ls -d "$AMD_LIBRARY_HOST" >/dev/null && echo "library found"   # check first; see below`.

Line 96's `Downloads go to /library/a, which is AppleMusicDecrypt/downloads/ on the host.` becomes `Downloads go to /library, which is your library directory (AMD_LIBRARY_HOST) on the host.`

`AMD_LIBRARY_ROOTS` being required also affects running the hub without compose, because the app does not read `.env` — that has always been true of `AMD_PASSWORD`, but there is now a second variable, so state it. Add one sentence to `README.md`'s development section, next to where `AMD_PASSWORD` is already mentioned for local runs:

```
Both AMD_PASSWORD and AMD_LIBRARY_ROOTS must be in the environment for a
local run; the app does not read .env, which compose alone consumes.
```

- [ ] **Step 4: Fix the remaining stale prose that names the old layout**

Four files carry comments and docstrings that describe a deployment that no longer exists. **No executable line changes in any of them** — only prose.

`hub/deploy/build_gate.py:172-179` — the comment claims it "compares against the *image's* fallback root, `/library/a`". The code beneath it is correct and stays byte-for-byte unchanged. Replace the comment lines that name `/library/a` or `/library/b` with text naming `AMD_DOWNLOAD_ROOT` and `/library`. **Keep** the sentence explaining that the check cannot see a runtime reorder — that reasoning is still exactly right, and it is why the TODO in `.env.example` exists.

`hub/deploy/acceptance_check.py:10` (module docstring) and `:87` (comment) — replace their `/library/a` and `/library/b` mentions with `/library`.

`hub/tests/test_deployment.py:1499-1503` — the docstring of the test at 1519 narrates the two-root overlay as current history. It is accurate as history, so **keep the narration and add one clause** marking that `/library/a` is gone; do not delete it, because it is the reason the assertion below it exists. Same treatment for `:180` inside the docstring of the test at 216, and for the `_default_of` docstring at 162-168, which uses `/library/a,/library/b` as its worked example. Note `_default_of` has **no callers** — the test that used one was replaced in Task 4 — so leave the function in place and only correct the example it names.

- [ ] **Step 4b: Generalise the machine-specific comments in four more files**

Step 8 below is a gate, and a gate that fails on the last task for a reason the plan did not name is a broken plan. These four files carry **this machine's** drive name and volume UUID in comments, and no earlier task touches them. Each is a comment or docstring line; **no executable line changes**.

| File | Line | What it says now |
|---|---|---|
| `hub/hub/library_scan.py` | 193 | ``path is the symlink `/home/m/Music/HDD_Music -> /run/media/m/.../Music`, so `relpath``` |
| `hub/hub/normalize.py` | 14 | `# Measured on /run/media/m/1A5E05A75E057D2F/Music (§7.2.1), plus the siblings the Apple` |
| `hub/tests/test_library_scan.py` | 258-259 | `# The user's own configuration is the symlink /home/m/Music/HDD_Music ->` and `# /run/media/.../Music, and §8.1 probes reachability from more than one call site, so` |
| `hub/tests/test_library_scan.py` | 347 | `# Review Focus #1: the real path is /home/m/Music/HDD_Music -> /run/media/...` |
| `hub/tests/test_normalize.py` | 189 | `# measured on /run/media/m/1A5E05A75E057D2F/Music` |

For each, keep the sentence and its reasoning, and replace only the concrete path with a general form:

- `/home/m/Music/HDD_Music` → `` a user-managed symlink `` (matching the phrasing compose.yaml and `library_scan` already use)
- `/run/media/m/1A5E05A75E057D2F/Music` → `/run/media/<volume-UUID>/Music`, or "the external library root" where the sentence does not need the path shape

`normalize.py:14` and `test_normalize.py:189` say the numbers were *measured* on a specific library. That is a real provenance claim and the §7.2.1 reference stays — only the path changes, because the path is not what makes the measurement reproducible.

- [ ] **Step 5: Retarget the mutation, which Step 1 turns red**

The entry at `hub/deploy/mutation_check.py:81-83` replaces the string `*** /library/a MUST BE IN THE LIST. ***`, which Step 1 deletes. The runner treats a mutation that does not change the file as a survivor:

```python
        if mutated == original:
            print(f"  SKIP (did not apply)  {label}")
            survivors.append(label)
```

so once Step 1 lands, this task's Step 6 fails with `SKIP (did not apply)` and a non-zero exit. That failure is correct and loud, not a silent gap. Replace that entry with two that target strings Steps 1 and 2 create. The module's path constants are `C = Path("compose.yaml")` and `ENV = Path(".env.example")` — note it is `C`, not `COMPOSE`:

```python
    "a host-specific library default reinstated in compose.yaml": (
        C, lambda t: t.replace(
            "source: ${AMD_LIBRARY_HOST:?set AMD_LIBRARY_HOST in .env to the host directory that holds your music library}",
            "source: ${AMD_LIBRARY_HOST:-/home/m/Music/HDD_Music}")),
    "the drive-is-required wording back in .env.example": (
        ENV, lambda t: t.replace(
            "it does not have to be a separate drive",
            "it must be a separate drive")),
```

- [ ] **Step 6: Run the deployment tests and the mutation check**

`mutation_check.py` holds **relative** paths and passes `cwd="hub"` to the `uv run pytest` it spawns, so it must be invoked with the repository root as the working directory:

```bash
cd /home/m/amdl_extend && hub/.venv/bin/python hub/deploy/mutation_check.py
```

Expected: a line per mutation, every one `CAUGHT`, and a final `all <N> mutations caught` with exit status 0. If any line reads `SKIP (did not apply)`, that mutation's target string does not exist in the file — fix the string, do not delete the mutation. Then:

Run: `cd hub && uv run pytest tests/test_deployment.py -v`

Expected: **31 passed**.

- [ ] **Step 7: Run the full suite and lint**

Run: `cd hub && uv run pytest -q && uv run ruff check .`

Expected: **644 passed**, and `All checks passed!`

- [ ] **Step 8: Confirm Review Focus 5 — nothing in the shipped tree names this machine**

Run:

```bash
cd /home/m/amdl_extend
git grep -nE '(/home/m/|HDD_Music|1A5E05A75E057D2F)' -- hub compose.yaml Dockerfile README.md .env.example
```

Expected: **no output**. Three patterns, not the `/run/media/` this step originally carried. `/run/media/` is a generic system path, not this machine's, and a gate that forbids it would fail on four occurrences that must stay:

- `compose.yaml:99` and `:105` explain *why* the mount source is a symlink rather than udev's automount point. That reasoning is the reason this whole change exists; deleting it to satisfy a grep would be the tail wagging the dog.
- the new test from Task 4 Step 2 asserts `"/run/media/" not in code`. An assertion has to name the string it forbids.

`/home/m/`, `HDD_Music` and `1A5E05A75E057D2F` are what actually identify a machine: a home directory, the personal name of a drive, and a volume UUID. If the command returns anything, that line is a miss. Fix it in this commit.

Three paths are excluded from the command, each a decision rather than an oversight:

- `AGENTS.md` keeps three `/home/m/Music/HDD_Music` occurrences, because they record this machine's drive as a fact for whoever operates it. The path list does not include that file.
- `docs/superpowers/**` holds verbatim transcripts and is outside the path list.
- `AppleMusicDecrypt/` and `wrapper/` are submodules and are not searched. `hub/.venv/` is untracked and therefore outside `git grep`.

- [ ] **Step 9: Commit**

```bash
git add .env.example README.md hub/deploy/build_gate.py hub/deploy/acceptance_check.py hub/tests/test_deployment.py hub/hub/library_scan.py hub/hub/normalize.py hub/tests/test_library_scan.py hub/tests/test_normalize.py hub/deploy/mutation_check.py
git commit -m "docs: drop the drive-required and /library/a rationale from the operator docs

.env.example told two contradictory things in fourteen lines: that
/library/a must be in the root list, and that the external drive is the
only root. The mount for /library/a was removed when the drive became the
single library, and only the prose survived it. A new operator reading
this file was told a drive was mandatory and that a root that did not
exist needed to be listed.

Its host-side companion is now AMD_LIBRARY_HOST, required, with no default: any
directory works, including one that is not a separate drive.

The mutation that targeted the deleted string is retargeted in the same
commit, because its runner counts a mutation that does not apply as a
survivor and exits non-zero -- the check goes red the moment the string
it targets is removed.

The four modules whose comments cited this machine's drive name or volume
UUID are generalised in the same commit. A gate that runs at the end of
the last task has to name every file it covers, or it fails for a reason
nobody planned for."
```

---

## Verification

Every task ends with `uv run pytest -q` and `uv run ruff check .` green. After Task 5, the following must also hold, and none of them is provable from the unit tests:

- [ ] `cd hub && uv run pytest -q` reports **644 passed**, 0 failed, 0 skipped
- [ ] `cd hub && uv run ruff check .` reports `All checks passed!`
- [ ] `docker compose build` reaches the Dockerfile's last `RUN` and prints `BUILD GATE OK`. This is the check that `load_settings()` inside the image still finds `AMD_LIBRARY_ROOTS` — it comes from `ENV AMD_LIBRARY_ROOTS=${AMD_DOWNLOAD_ROOT}`, and the spec requires it be measured rather than assumed.
- [ ] With `AMD_LIBRARY_HOST` unset, `docker compose config` fails with a message naming `AMD_LIBRARY_HOST` (Task 4 Step 4 already establishes this)
- [ ] With `AMD_LIBRARY_HOST` set in `.env` — **uncomment line 95 of your `.env`**, which is currently `# AMD_LIBRARY_HOST=/home/m/Music/HDD_Music` — `docker compose up -d` starts, and `/api/status` reports the expected per-root album counts. A plausible total is equally consistent with a full library and an empty mount, so read the per-root numbers, not the sum.
- [ ] `git status --short` shows the 15 pre-existing files still modified with 77 insertions and 77 deletions, and nothing else
