# Task 10 report — container image, compose stack, and Phase 1 acceptance

**Status: DONE_WITH_CONCERNS.** All six acceptance items verified in a live container. Three
concerns, none of which is a deployment failure: one is a pre-existing defect in `dedup.py` that
only a two-root library exposes, one is a plan error I had to correct, and one is a limitation of
this host that no amount of testing here can remove.

**Date:** 2026-09-27 · **Branch:** `feat/phase-1-foundation` · **Suite:** 583 passed (561 baseline
+ 22 new), 0 regressions.

---

## 1. What was added

| Path | |
|---|---|
| `Dockerfile` | the runtime image, 13 instructions, one stage |
| `compose.yaml` | the stack, one service |
| `.env.example` | annotated operator settings |
| `README.md` | how to run it, the 2FA flow, the honest security section |
| `hub/deploy/build_gate.py` | the image's last `RUN`; asserts the layout from inside the image |
| `hub/deploy/acceptance_check.py` | the live library checks (items 5 and 6) |
| `hub/deploy/compose.ntfs.yaml` | "the drive is plugged in" overlay |
| `hub/tests/test_deployment.py` | 22 tests holding the deployment's static invariants |
| `AGENTS.md` | the `amd-hub` section, replacing "The only tests in the workspace" |
| `.dockerignore` | the runtime image's exclusions |
| `hub/pyproject.toml`, `hub/uv.lock` | `pyyaml` as a dev dependency, for the compose tests |

**Where the files went, and one deviation from the dispatch.** The dispatch asked for
`hub/Dockerfile` and `hub/compose.yaml`; they are at the **workspace root**, which is what the
brief, spec §4 and the root `.dockerignore` all assume. The `.dockerignore` is decisive: it says
"The build context is the workspace root", and a context of `hub/` would contain neither
`AppleMusicDecrypt/` nor `wrapper/` at all, making the file itself pointless. The brief's Step 6
also commits `Dockerfile compose.yaml` from the root. `hub/tests/test_deployment.py` is where the
dispatch said, and the harnesses are in `hub/deploy/`.

## 2. Dockerfile decisions

**One stage, not two.** The plan's stage 1 built `wrapper-lite-rootless` from source. The binary
and its 121 MB rootfs are already built in this workspace, and the build cannot be reproduced
offline — CMake `FetchContent` pulls cJSON v1.7.19 and Dobby, so a cold configure needs the
network. Copying the artifacts is also what spec §3.2 is really asking for: a layer that does not
move when the Python changes. The exact rebuild command, **including the two flags the spike had
to add** (`-DCMAKE_POLICY_VERSION_MINIMUM=3.5` or CMake ≥ 4 refuses cJSON;
`-DCURL_SHARED_LIB=$PWD/rootfs/system/lib64/libcurl.so` or `find_library` picks up a host
`libcurl.so` and the Android payload will not start), is recorded at the top of the Dockerfile so
the build is not lost.

**The vendor tree is at `/app/AppleMusicDecrypt`, not `/opt`.** Not a preference:
`ripper_host._VENDOR_ROOT` and `app.vendor_config_path` are both
`Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"`. There is no environment variable and
no argument. The plan's `COPY AppleMusicDecrypt /opt/AppleMusicDecrypt` would produce a
`RipperHostError` naming a path that exists and is not the one the derivation wants, at every
boot, after a build that reported success.

**`PYTHONPATH` is `/app/hub`: the directory that *contains* the package.** `import hub` needs a
sys.path entry holding `hub/__init__.py`; the package is COPYed to `/app/hub/hub`, so that entry
is `/app/hub`. The plan's `/app` is the *project* directory — it holds `hub/`, but as a plain
directory with no `__init__.py`, so it can only ever contribute a **namespace** package.

> **Correction, added in fix round 1.** I originally wrote that the plan's value "makes the
> container die at `CMD` with `No module named 'hub.app'`". **That is false for this image**, and
> the review measured it. Under `-m`, `sys.path[0]` is the working directory, and
> `WORKDIR /app/hub` already *is* the package root, so the CWD supplies the real package before
> `PYTHONPATH` is consulted. Measured in the shipped image:
>
> ```
> PYTHONPATH   CWD         hub.__path__             result
> /app/hub     /app/hub    ['/app/hub/hub']         regular    <- shipped
> /app         /app/hub    ['/app/hub/hub']         regular    <- the plan's; works by accident
> (unset)      /app/hub    ['/app/hub/hub']         regular    <- also by accident
> /app/hub     /           ['/app/hub/hub']         regular    <- shipped, CWD moved
> /app         /           ['/app/hub']             NAMESPACE
> (unset)      /           import fails outright
> /app         /app        ['/app/hub', '/app/hub'] NAMESPACE, two portions
> ```
>
> The shipped value is still right — it is the only one correct *independently of the working
> directory* — but the reason I gave was the weakest of the three available, and it predicted a
> failure that does not occur while omitting the one that does. A `working_dir:` in compose, a
> different base image, or running a script from anywhere else turns `/app` into a namespace
> package, and "it currently works" is not a property.

The vendor tree is deliberately absent from `PYTHONPATH` so `src.*` keeps resolving through the
seam's own `sys.path` insert.

**The vendor config is built at `<vendor>/config.toml` from upstream's
`config.example.toml`**, by three `sed`s, rather than forking a copy of ~150 settings into this
repo. One source of truth, and the operator's host `config.toml` (gitignored, excluded by
`.dockerignore`) never enters the context. Each `sed` is followed by a `grep -qx` and a
`tomllib` re-read, so a pattern that stops matching is a **build failure** rather than a shipped
config that is wrong silently.

* `dirPathFormat` / `playlistDirPathFormat` → absolute, `/library/a`. The seam `chdir`s into the
  vendor root, so upstream's relative `downloads/{album_artist}/{album}` would write to
  `/app/AppleMusicDecrypt/downloads/` while the library scan reads the bind mount: the client
  fills a tree nothing dedups against and every track re-downloads forever, with nothing red.
* `region.language` → a build `ARG` (`ja`, matching the operator's own config.toml; upstream's
  example ships `zh-Hant-HK`). A per-account preference is not a build input, so it is settable
  from `.env` without editing the Dockerfile.
* `localInstance.enable` and `instance.url` are **not** rewritten. They are already right for this
  topology, and both are asserted. An image that "fixes" them by `sed` is a second source of truth.

**`AMD_WRAPPER_BINARY=/opt/wrapper/wrapper-lite-rootless`**, not the QEMU launcher: the QEMU one
has no host rootfs and passes `--base-dir` *into the guest*, so the 2FA code the hub writes lands
in a namespace the hub cannot see. `config.py`'s `DEFAULT_WRAPPER_BINARY` is left as the QEMU one
— it is correct for the upstream desktop deployment this client was cloned from, so the *image*
overrides it.

**`CMD ["/opt/venv/bin/python", "-m", "hub.app"]`.** `main()` reads the environment and passes
`workers=1` to uvicorn, so the single-process rule is enforced in code and cannot be raised from
compose.

**`hub/deploy/build_gate.py` runs as the last `RUN`.** It asserts the layout through
`app.vendor_config_path()`, `ripper_host._require_vendor_root()` and `load_settings()` — the
code's own functions, not a second derivation. It earned its keep on its first run: it failed the
build on a bug of my own making inside the gate. It is a committed script rather than a
`RUN python -c <<PY` heredoc because a here-document's failure mode on a frontend without
heredoc support is a parse error that reads like a typo.

## 3. Compose decisions

- **Both `security_opt` values, `cap_add` absent.** The measured table, not a guess (spike §1,
  spec §14.1). `systempaths=unconfined` is a real hardening reduction and the file says so at
  length: writable `/proc/sys` for a root process, and `/proc/sys/kernel/sysrq` together with the
  now-writable `/proc/sysrq-trigger` giving `reboot`/`crash`. `/proc/kcore` is **not** a live leak
  (`CapEff` has no `CAP_SYS_RAWIO`); the risk is conditional on that being added later. The trade
  is against not being `privileged`, no added capabilities, and one published port.
- **No `user:`** — load-bearing, and the reason is a single-uid map. The launcher
  `unshare(CLONE_NEWUSER)`s with `"0 0 1"` before touching the filesystem, which leaves a
  rootfs owned by any other uid unmapped and unwritable even under `CAP_DAC_OVERRIDE` (spike §5.1
  F2). The rootfs is `COPY`ed, so it is root-owned and the container must run as root to match.
- **No `network_mode: host`** — the launcher does not unshare a *network* namespace, so `host`
  would put its bind on the host's loopback where the operator's own wrapper QEMU already listens,
  and a failed bind makes the payload signal *itself*, logging a line identical to an external
  kill (spike §6.5). The default bridge netns closes the whole class.
- **One port, 8080.** The wrapper's 12340 is an unauthenticated API serving decrypted audio and is
  bound to container loopback.
- **`start_period: 120s` is arithmetic.** Uvicorn does not bind its socket until the lifespan's
  startup finishes, and the lifespan's first step blocks on the wrapper's 60 s `startup_timeout`.
  Measured over four runs: **65, 65, 66, 73 s** from `up` to the first `200` -- so 65-75 s, not one run's figure. A lower value turns a correct boot into a crash
  loop under `restart: unless-stopped`.
- **`stop_grace_period: 330s`** = `DRAIN_TIMEOUT_SECONDS` (300) + the supervisor's `STOP_TIMEOUT`
  (15). Docker's default 10 s would SIGKILL a real download ten seconds in, and a SIGKILLed job
  leaves a row in `running` that nothing reaps, because `claim_next` only ever claims `queued`.
  Grace costs nothing when nothing is running.

**Two things I corrected, both found in the live run:**

1. **The token database is not on `/data`.** The plan's comment says
   `hub-data:/data  # Apple token DB + hub.db` and that is wrong: the launcher `chroot`s *before*
   resolving `--base-dir` (`wrapper-lite-rootless.c:131-138` chroots, then `mkdir` on line
   142 and `perror`s on 143), so `/data/wrapper` means
   `<rootfs>/data/wrapper` inside the chroot — a different namespace from the container's `/data`.
   With nothing at `<rootfs>/data`, the first run logged **`mkdir base_dir_arg failed: No such
   file or directory`** and `mkdir mpl_db failed`. `lite` recovered by creating the tree itself, so
   the wrapper worked and the perrors were cosmetic *on that run* — but the tree was in the
   image's writable layer, so `down`/`up` would have thrown the Apple account away. Fixed with a
   second volume, `wrapper-data:/opt/wrapper/rootfs/data`. Verified: a sentinel file and the
   `mpl_db` survived `docker compose down` (without `-v`) and `up`, and the two `mkdir` errors are
   gone from the log.
2. **The NTFS root moved from a commented line to an overlay file**, `hub/deploy/compose.ntfs.yaml`.
   The brief asked for a commented-out override and it is still commented out in `compose.yaml`,
   but the *reason* it should not be uncommented by hand is worth a file: the mount and
   `AMD_LIBRARY_ROOTS` must move together, and a mount that is attached but not in the roots is a
   directory nothing scans, with no symptom. `docker compose -f compose.yaml -f
   hub/deploy/compose.ntfs.yaml up -d` moves both, or neither.

## 4. Acceptance results

Docker 29.8.1 / Compose 5.5.1 were available, so everything below is a **live container**, not an
inference. The image was rebuilt with `--no-cache` after `docker builder prune -af` and
`down -v` (66 s) and the whole run repeated on it.

| # | Item | Result |
|---|---|---|
| 1 | Builds, starts, `GET /api/health` unauthenticated | **PASS.** `200 {"status":"ok"}` at **t=65-75 s**. (Fix round 1: four runs measured 65, 65, 66, 73 s; the 65 s here was one of them.) `healthy` per docker's own probe. |
| 2 | Every other route 401 without a session | **PASS.** All 12 JSON routes `401` (GET/POST/DELETE, incl. `POST /api/wrapper/{start,stop,restart,login,login/2fa}`, `POST /api/library/scan`). Pages `303 → /login`. `/openapi.json` and `/docs` `404`. |
| 3 | Login works; cookie `HttpOnly` + `SameSite=Lax` | **PASS.** Wrong password `401` with **no** `Set-Cookie`. Correct password `200`; `amd_hub_session=…; HttpOnly; Max-Age=43200; Path=/; SameSite=lax`. |
| 4 | Wrapper ready, **or** the specific no-account message | **PASS — the second outcome, which is the correct one here.** The real `wrapper-lite-rootless` came up (`wrapper-lite listening on 127.0.0.1:12340`) and the supervisor raised the specific message, not a timeout: *"no account is logged in on the wrapper at http://127.0.0.1:12340/status: it is up and answering /status, but regions is empty… Nothing needs to be waited for — the wrapper itself is ready."* `/api/status` reports `problem: no-account`. |
| 5 | Real counts over the mounted library, not silently empty | **PASS.** 4,739 album directories, `degraded_roots: []`, **per root**: `/library/a` 1,069 + `/library/b` 3,670. A host-side run of the same code over the host paths gives the identical 1,069 / 3,670 / 4,739, which is what proves the **symlinked** `/home/m/Music/HDD_Music` source is delivering the real NTFS tree and not an empty mount point. A relpath rejoins onto its root. |
| 6 | A title in two different albums does not skip | **PASS.** 2,809 track keys appear in more than one album directory; 824 of those are genuinely different albums (種別 B). 436 real (album, track) pairs asked about their own album, and none was wrongly skipped for belonging to another. **With a positive control**, because `find_duplicate` returning `None` would pass this vacuously: 493 of 500 sampled albums reported their own track, and every named path exists on disk. |

`hub/deploy/acceptance_check.py` reproduces items 5 and 6 and exits non-zero on any failure.

## 5. Concerns

**1. A real defect in `dedup.py`, found by the two-root library and not fixed here.**

`find_duplicate` builds `candidates` out of `scan.by_name`, which spans **every** root, and
`DuplicateHit.matched` keeps only `AlbumDir.relpath` — the root is discarded. So a match found
under `/library/b` is reported as a bare relpath, and with two roots that string resolves under
none of them. **493 of 493** sampled hits were like this:

```
'let me battle (feat. つぐ, わかばやし & みょみょ)'
  matched=['9Lana/Let me battle (…) - Single', 'new-dl/9Lana/Let me battle (…) - Single']
  # the first is under /library/a, the second under /library/b
```

The skip decision itself is correct — the track really is in the library, and the whole point is
to skip it. What breaks is §7.4's escape hatch: `app.py::_skip_reason` documents that "the paths
are the whole of the evidence… the only way a user can overrule it is by reading which
directories were matched", and with a second root those paths are not adjudicable. A
`loose` false skip and a real duplicate are indistinguishable in the UI.

This is task 4's code and its shape is pinned by its own tests and by task 9's consumers, and
fixing it is a design decision (qualify the strings with the root? add a field? render the root
in the queue page?) rather than a packaging one, so I have **not** touched it. `acceptance_check.py`
counts and reports it so it stays reproducible, and the assertion I originally wrote for it was
wrong — it joined every matched relpath against the *queried* album's root and so reported the
correct behaviour as a failure. Corrected to "resolves under *some* root" plus the cross-root
counter.

**2. The plan's Dockerfile is wrong in two places**, and I followed the code rather than the plan:
`/opt/AppleMusicDecrypt` (§2) and `PYTHONPATH=/app` (§2). Both are recorded in `AGENTS.md` and
asserted by `test_deployment.py`. A reviewer comparing against the plan should read §2 first.

**3. Could not be verified on this host, stated plainly:**

- **The 2FA exchange end to end.** There is no Apple account here, so no login child was ever
  spawned and no code was submitted. Spike §7 records the same gap. What I *did* verify is
  everything short of it: the launcher is the real `wrapper-lite-rootless`, the handoff path
  resolves to `/opt/wrapper/rootfs/data/wrapper/2fa.txt` inside its chroot with its parent present,
  and the argv the supervisor would build is
  `['/opt/wrapper/wrapper-lite-rootless', '--login', <user:pass>, '--code-from-file', '--base-dir', '/data/wrapper']`
  — with the credentials absent from the log line the supervisor emits. I did not attempt a
  fabricated login against Apple's service to probe further; that is an outbound authentication
  attempt against a third party and it was not asked for. Verifying this needs a real account.
- **Items 3–6 of the plan's own Step 4 acceptance** (download an album, re-request it and see
  every leaf `skipped` with a real `skip_reason`, request a same-titled track from a different
  album, unplug the drive and restart). These need a logged-in Apple account. The dispatch's six
  items are all covered above.
- **`seccomp:unconfined` and `cap_add` as separate controls.** Taken from the spike's transcripts
  rather than re-run; only the working configuration was exercised. The negative rows are not
  cheap to re-derive and re-running them would prove nothing the recorded transcripts do not.
- **`/data` and `<rootfs>/data` are separate volumes, not one mounted twice.** Deliberate: they
  share nothing and giving each an owner stops a `docker volume rm` aimed at the wrong one. The
  cost is a second `docker volume rm` to forget the whole stack.

**4. A pre-existing gap worth flagging for phase 2, not fixed here.** `library_scan` reports a
root as degraded when it cannot be *read*. A root that is a **silently empty mount point** — the
shape Docker's bind-mount autocreate produces from a dangling symlink — reads as a healthy empty
library, so nothing warns that a drive is missing. `compose.yaml` mitigates it by keeping the
drive off by default and documenting `ls -L` before attaching it, and the acceptance harness
reports per-root counts for exactly this reason. Teaching the scan to distinguish the two needs a
decision about what "empty" means (a marker file? a recorded high-water mark?), which is not
packaging.

## 6. On the tests

22 new tests, and they were **mutation-checked** rather than trusted: 16 mutations to the
Dockerfile, both compose files and `.dockerignore`, one at a time, each reverted. The first pass
caught 14 and two survived, both of which were real gaps in what I had just written:

- `user: 1000` in compose — a hard requirement (the single-uid map) that I had documented in a
  comment and never asserted. Now `test_the_container_runs_as_root_because_the_uid_map_requires_it`.
- deleting a `grep -qx` after a `sed` — i.e. removing the assertion that a rewrite actually
  applied. Now `test_every_rewritten_config_value_is_asserted_after_it_is_rewritten`, checked
  generally so the next override inherits it.

All 16 are now caught. Two further "survivors" in the first pass were my `sed` anchors being
wrong, not the tests; both were re-run correctly and caught.

`hub/tests/test_ripper_host.py` was extended by one line — `hub/deploy/` added to
`_ENFORCED_ROOTS`. The boundary test's own docstring says an unenforced directory "is a boundary
that exists only until someone uses it", and Task 10 added the third hub-owned Python directory
containing the file that runs before the app does. Its 73 tests still pass.

Full suite: **583 passed**, 42 s, after clearing `__pycache__`.

---

*This is a durable copy. The report the task brief asked for was written to
`.superpowers/sdd/2026-09-26-amd-hub-phase1/task-10-report.md`, which `.gitignore` excludes
(superpowers SDD scratch). This copy is here because §5.1 documents a live defect in
`hub/hub/dedup.py` — a cross-root `skip_reason` that names paths resolving under no configured
library root — and a report that only exists in gitignored scratch will not be found by whoever
fixes it.*

---

## Fix round 1

**Suite: 598 passed** (583 at the end of round 0, +15). Docker 29.8.1 / Compose 5.5.1 available, so
every claim below was executed against a live container on a rebuilt image.

### The assigned fix — `matched` carried paths that resolve under no root

`DuplicateHit` gains `resolved` alongside `matched`. `matched` is **unchanged** — bare relpaths,
posix form, sorted — so task 4's equality semantics and its pinned tests survive; `resolved` is
`str(roots[root_index] / relpath)`, the same expression `library.py::_album_dict` already used, and
`skip_reason` now carries it.

Both fields are required rather than defaulted. A hit with an empty `resolved` would put the
broken form straight back on the user, and `find_duplicate` is the only construction site, so
requiring the field is what stops a future caller from quietly building the unresolvable one. The
`if not matched` check also became `if not hits: return None` over one filtered list, so the two
output forms are built from the *same* set — a second generator re-testing the same predicate
would be a place for them to drift, and a hit that claims a path it did not match is worse than
none.

`find_duplicate` computed both forms in one pass for the reason above:

```python
hits = [c for c in candidates if title_key in c.track_keys]
if not hits:
    return None
return DuplicateHit(
    matched=tuple(sorted(c.relpath for c in hits)),
    resolved=tuple(sorted(str(scan.roots[c.root_index] / c.relpath) for c in hits)),
)
```

**Verification 1 — the cross-root case, on the real library.** The review's example, album name in
both roots:

```
matched  (bare relpaths):
  '9Lana/Let me battle (feat. つぐ, わかばやし & みょみょ) - Single'
      resolves under 1 root(s): ['/home/m/apple-dl_extend/AppleMusicDecrypt/downloads']
  'new-dl/9Lana/Let me battle (feat. つぐ, わかばやし & みょみょ) - Single'
      resolves under 1 root(s): ['/home/m/Music/HDD_Music']       <-- does not say WHICH
resolved (root-qualified, what skip_reason now carries):
  /home/m/Music/HDD_Music/new-dl/Let me battle … - Single                is_dir=True
  /home/m/apple-dl_extend/AppleMusicDecrypt/downloads/9Lana/… - Single    is_dir=True
```

Swept over the whole library — every album, every key — not just the example:

```
hits=12129   matched entries=17509   resolved entries=17509
resolved entries that resolve under their own root: 17509/17509  (100%)
matched entries that are a *distinct* string per hit: 1 per hit on average
```

And the harness now separates the two ways the bare form loses information, which are not the same
defect: **493/493 hits span roots** (no single root explains the whole hit — the common case, and
the one that breaks adjudication) and **0/493 contain a relpath resolving under more than one
root** (indistinguishable entries — the 種別 A shape, latent here exactly as the review said, since
0 of 4,739 albums share a relpath across the two roots). `resolved` fixes both.

The 493/493 matches the review's figure. The 0/493 is why `resolved` is "alongside" rather than
"replace": the bare form cannot express that second case at all.

**Two `skip_reason` assertions in task 9's tests changed**, because the format changed by design:
`test_a_job_already_on_disk_is_skipped_with_the_matched_paths` and
`test_a_skipped_jobs_matched_paths_reach_a_reconnecting_tab` now expect the root-qualified string,
and the 種別 A test (`two roots holding the same relpath`, previously
`"duplicate:toe/4pi|toe/4pi"` — two identical strings, one directory each) expects both
directories and asserts each one `is_dir()`. `test_dedup.py`'s two hand-built `DuplicateHit`s were
updated for the new field.

### Important 1 — the dead `.dockerignore` patterns

`*.db` matched the context root and nothing else, so `hub/hub/web/worker-check.db` shipped in the
image. Confirmed before fixing:

```
$ docker run --rm --entrypoint sh apple-dl_extend-amd-hub -c 'ls -la /app/hub/hub/web/'
-rw-r--r-- 1 root root 12288 Sep 26 23:46 worker-check.db
```

Every pattern in the secrets block is now `**/`-prefixed (`**/*.db`, `**/*.db-wal`, `**/*.db-shm`,
`**/data/`, `**/tmp/`, `**/.env`, `**/.env.*`), plus `**/*.sqlite`/`**/*.sqlite3` for a queue
database under another name. The reason is written into the file rather than left implicit, because
the value is not about the 12 KB of empty sqlite that was there: a `hub.db` created *in the tree*
by any mundane slip is queue rows — requested URLs, `adam_id`s, `skip_reason` strings.

The Apple-account exclusion moved next to those patterns with its own reason, rather than sitting
in the wrapper block where it read as an unrelated path exclusion. The hazard was already handled
by that explicit `wrapper/rootfs/data/` line, so nothing leaked; the review confirmed no
`accounts.sqlitedb`, `cookies.sqlitedb` or `token_cache.json` anywhere in the image and I confirmed
the same after the change.

**Verification 2 — the image carries no database, and it is asserted:**

```
$ docker build -q -t amd-hub:check . && docker run --rm --entrypoint sh amd-hub:check -c 'find / -name "*.db" -o -name "*.sqlite*"'
/usr/share/mime/application/vnd.sqlite3.xml          <- an XML mime map, not a database
$ docker run --rm --entrypoint sh amd-hub:check -c 'ls /app/hub/hub/web/'
static
templates                                              <- worker-check.db gone
```

### Important 2 — the test that "held" the exclusion could not see it

`_excluded_from_context` is rewritten to model Docker's actual matching. It previously treated a
`*.ext` glob as a no-op via a bare `continue` while its docstring promised such patterns "are
reported rather than silently treated" — a documented promise it did not keep, and the case that
mattered was the one it skipped. The review's measurements (`worker-check.db` → `False`, `hub.db` →
`False`, `x/y/z/.env` → `False`) are exactly what a `continue` produces.

The rewrite is a real matcher: `fnmatch` per segment, `**` spanning any number of segments, a
trailing `/` dropped, a path excluded when it *or any ancestor* is matched (Docker prunes the
directory), last match wins with `!` negating. The docstring now states the thing that caused the
bug — **Docker's matching is path-based and is not gitignore's**, where a pattern with no `/`
matches the basename at any depth.

**Verification 3 — the helper now answers for nested paths:**

```python
assert _excluded_from_context("hub/hub/web/worker-check.db")   # True  (was False)
assert _excluded_from_context("a/b/c/d/e/deep.db-wal")         # True
assert _excluded_from_context("x.db")                          # True
assert _excluded_from_context("hub/tests/conftest.py")         # True  (directory prunes)
assert not _excluded_from_context("hub/testsx/thing.py")       # False (prefix, not segment)
assert not _excluded_from_context("wrapper/rootfs")            # False (sibling of the exclusion)
assert not _excluded_from_context("hub/hub/app.py")            # False (the tree must ship)
```

Plus `test_the_deep_exclusions_are_written_with_a_leading_globstar`, which reads the *spelling* off
the file. That is deliberate: the matcher and the `.dockerignore` agreed while both were wrong, and
no amount of self-consistent matching can catch that — which is how a nested database shipped.

### Important 3 — the `PYTHONPATH` reason was falsified

I claimed `/app` "makes `hub.app` never resolve and the container dies at `CMD`". **It does not**,
and I re-measured rather than taking the review's table:

```
PYTHONPATH CWD       | hub.__path__             | import hub.app
---------------------+--------------------------+------------------
/app/hub   /app/hub  | ['/app/hub/hub']         | regular
/app       /app/hub  | ['/app/hub/hub']         | regular
(unset)    /app/hub  | ['/app/hub/hub']         | regular
/app/hub   /         | ['/app/hub/hub']         | regular
/app       /         | ['/app/hub']             | NAMESPACE
(unset)    /         | import hub failed outright
/app       /app      | ['/app/hub', '/app/hub'] NAMESPACE, two portions
```

The mechanism is `sys.path[0]`: under `-m` the CWD precedes `PYTHONPATH`, and `WORKDIR /app/hub`
already *is* the package root, so the plan's value works — and so does no `PYTHONPATH` at all. The
shipped value is still the only correct one, because it is the only one correct *independently of
CWD*.

Corrected in all four places: the `Dockerfile` comment (now with the table inline and an explicit
"do not simplify this back to `/app` on the grounds that it currently works"), `AGENTS.md` (the
table, and "do not overstate the failure mode, which I did"), the report, and the test — which is
renamed `test_pythonpath_is_the_directory_that_contains_the_package` and now asserts
`Path(PYTHONPATH) == Path(package).parent`, the CWD-independent property, rather than a claim about
`CMD`.

Worth noting: my first measurement script in this round printed `RUNS` for all seven cells, because
the success branch was formatted before the import that was supposed to fail. The real result was
in the third run. A probe that cannot report its own failure is worse than no probe.

### Important 4 — the positive control's denominator

The harness now reports **asked / found / missed / not-asked**, and asserts `missed == 0` over
`asked` rather than `found > 0` over the sample size:

```
albums sampled 500 | asked 493 | found 493 | missed 0 | not asked: unusable key 6, non-fixed-point 1
[NOT ASKED] '1 a.m. (feat. shinoだす。) - Single' key='1 a.m. (feat. shinoだす。)' (normalize is not a fixed point here)
[PASS] every track the control asked about was found  493/493 found, 0 missed
```

The 1 miss is gone, and it was the harness's bug exactly as diagnosed. `reconstructible(key)`
returns the key only when `normalize(key) == key`, and `None` otherwise. Measured on the library:

```
total distinct keys : 8721
empty ("")          : 1
fixed points        : 8714
NOT fixed points    : 6
   '1 a.m. (feat. shinoだす。)'  ->  normalize() = 'a.m. (feat. shinoだす。)'
   '00 [instrumental]'           ->  normalize() = '[instrumental]'
   '1-ch channel id'             ->  normalize() = 'ch channel id'
```

Each of the 6 had **two** prefix tokens stripped, so there is no single string the harness can
construct that is guaranteed to normalise back. My first attempt guessed a `1-01 ` prefix; that is
wrong for `00 am` (which needs `1-01 00 am`) and I replaced it rather than ship a plausible answer.
They are counted and named instead. The 6 unusable keys are now counted too, where the original
`continue`d past them — that `continue` is what turned 6 correct refusals into apparent failures.

### Important 5 — the from-source build cost

`wrapper/wrapper-lite-rootless` is gitignored and `wrapper/rootfs/` is ignored except for 101 of
118 files, so a fresh clone stops at the `COPY` with a message naming neither cause nor fix. The
`Dockerfile` now says so directly above the failing instruction, with the literal error and a
pointer to the recipe, and the README's development section points at the same. The new test
asserts the gitignore fact by asking git rather than by reading a comment, and separately that the
recipe and both required cmake flags are present — labelled as a documentation assertion, because
it can check that an operator sent to the recipe will find something, not that the recipe is
correct.

### Important 6 — a silently-empty mount now fails visibly, in the product

`LibraryScan.per_root()` returns a positional count per root, keeping the slot of a root that could
not be read, exactly as `roots` and `reachable` do. It is on `GET /api/status`,
`POST /api/library/scan`, `/api/library/albums`, and the library page, which now renders a per-root
table and warns `empty — is this mounted?` on a root that is readable but has nothing in it.

**Verification 7 — the gap demonstrated, then closed.** With a third, deliberately empty root
mounted:

```
=== an empty, READABLE root: the state a not-plugged-in drive lands in ===
  degraded_roots: []
  per_root      : [1069, 3670, 0]
  albums        : 4739

  /library/a         1069  ''
  /library/b         3670  ''
  /library/empty        0  'empty — is this mounted?'
```

`degraded_roots` is empty and the total is plausible — exactly the shape that produced a silent
re-download — and the zero is now both in the API and on the page. The populated case renders the
counts and no warning, and a test asserts the warning is *absent* when unwarranted, since a row
that cries wolf about 3,670 albums is worse than no row.

### Minors

- **7** `docker compose cp` in `README.md` and the harness docstring. Compose v2 names the container
  `<project>-<service>-<index>`, so `docker cp … amd-hub:` fails with `No such container`; verified
  the replacement works and printed the copy.
- **8** Boot is **65–75 s**, not 65 s. Four runs: 65, 65, 66, 73. `AGENTS.md` and the README now
  quote the range, and `AGENTS.md` records the four measurements. `start_period: 120s` is unchanged
  and still derived from `startup_timeout` by the test rather than transcribed.
- **9** The build gate's module-scope imports are now defended in its docstring as a deliberate
  choice — a gate cannot report a layout problem if reaching its own code is the first thing that
  breaks. The `dirPathFormat` vs `library_roots[0]` coupling: the gate documents that it compares
  against the *image's* fallback and cannot see a runtime reorder, and the "keep `/library/a` first"
  warning is now in `.env.example` and compose.yaml where somebody reordering a list is reading. A
  new test pins the shipped defaults so the warning documents a *checked* rule rather than a hope.
- **11** `wrapper-lite-rootless.c:143` is the `perror`; line 142 is the `mkdir` it reports.
  Corrected in `AGENTS.md` and both report copies. `wrapper_supervisor.py`'s two `:142` citations
  name the `mkdir` and are correct, so they are untouched.

### On the tests

`hub/deploy/mutation_check.py` is committed: 15 mutations of the Dockerfile, both compose files,
`.dockerignore`, `.env.example`, the README, `dedup.py`, `app.py`, `library_scan.py`, the library
API and `library.html`, each applied and reverted. **The first pass left four survivors and three
of them were real gaps in tests I had just written:**

| survivor | why it survived | fix |
|---|---|---|
| `resolved` left unsorted | the two-root test's natural order is already sorted — the same trap `test_hit_paths_are_sorted_for_stable_display` documents | `test_resolved_is_sorted_…` asserts the *property* of the result and that reversing the root order does not change the string |
| the fresh-clone warning removed | a comment, and no test asserted comments | asserts the gitignore fact via `git check-ignore` and the recipe's presence, labelled as a doc assertion |
| the `/library/a`-first warning removed | a comment again | `test_the_clients_write_root_is_the_first_scanned_root_in_both_defaults` pins the shipped defaults and requires the warning to document a checked rule |

The fourth — collapsing the boot range in the README to one run's number — I deliberately did
**not** write a test for, and removed it from the mutation list with the reason recorded there. A
measurement is not an invariant: it cannot be asserted without re-measuring, and a test pinning a
number in a README would only make the number harder to correct. Writing one would be the "test
that asserts nothing" pattern this file exists to catch.

All 15 are caught now. `test_a_crashed_wrapper_is_restarted` failed once while the mutation check
was running subprocesses in parallel and passes in isolation and in a clean full run — a load
flake in a timing-sensitive supervisor test, not a regression; re-verified below.

### Not changed

The gate's six diagnostics, the `config.toml` construction, the healthcheck and `start_period`,
secret hygiene, the one-stage image, both `security_opt` values with `cap_add` absent, no `user:`,
no `network_mode: host`, `--workers 1`, the NTFS overlay approach, and the
`systempaths=unconfined` honesty. All verified by the review and all still in place.

---

## Fix round 2

**Suite: 602 passed** (598 at the end of round 1, +4). Docker available, so the item-5 fix was
verified in a live container as well as in a test.

### 1 — `test_a_crashed_wrapper_is_restarted` was a real race

The review's diagnosis holds exactly. `_restart_after_crash` calls `_ensure_child()` — which spawns
and sets `pid` — and only *then* awaits `_wait_ready()` (`wrapper_supervisor.py:1029-1030`). The
test polled on `sup.pid not in (None, first)`, so it broke out while the new child was still in
`bind()` and the HTTP GET landed on a port that was not listening. The supervisor's own definition
of a successful restart is the `is serving again on` line it emits at `:1034`, *after* readiness;
the test observed "spawned" instead.

I ran a control rather than trusting either of us. `hub/deploy/oversubscribe_check.py` is
committed: it derives the load from `os.cpu_count()` (2×, so 32 busy loops on this 16-CPU host) and
re-runs one test N times. **Same load, the pre-fix poll restored verbatim:**

```
########## CONTROL: the pre-fix poll, 2x CPU oversubscription ##########
host cpus   : 16
load        : 32 busy loops (~2x oversubscription)
  run 14/20: FAIL    oversubscription, failing with `ConnectError('All connection attempts failed')`.
  ...
  run 20/20: FAIL    oversubscription, failing with `ConnectError('All connection attempts failed')`.
0/20 passed under oversubscription
```

**And after the three-line change:**

```
########## AFTER the fix, 2x CPU oversubscription ##########
  run  1/20: pass    1 passed in 1.54s
  ...
  run 20/20: pass    1 passed in 1.42s
20/20 passed under oversubscription
```

The test now polls the serving line, and its docstring states what is actually being observed —
including why "a new pid" is the bug rather than the specification, with the measured contrast.

`test_the_restart_budget_stops_the_loop` was already polling a log line and is untouched.

**My first harness reported 0/20 *after* the fix, which was wrong.** It ran with a bad `cwd` and a
target missing the `tests/` prefix, so pytest exited non-zero on a collection error and I counted
that as a failing test. The fix looked broken and it was not. The harness now detects "no tests
ran" / "file or directory not found" and exits 3 with `HARNESS FAULT` rather than printing a
percentage — that is the third time in two rounds I have written a probe that could not report its
own failure, and the check for it belongs in the probe.

### 2 — `resolved`'s requiredness

`test_resolved_may_not_be_defaulted_away` pins it: constructing `DuplicateHit(("a/b",))` raises
`TypeError` naming `resolved`. The mutation is killed:

```
$ sed -i 's|^    resolved: tuple\[str, \.\.\.\]$|    resolved: tuple[str, ...] = ()|' hub/hub/dedup.py
$ (cd hub && uv run pytest -q)
FAILED tests/test_dedup.py::test_resolved_may_not_be_defaulted_away - Failed:...
1 failed, 601 passed in 40.93s
```

A default is the only thing between a caller and a `skip_reason` naming nothing, so a docstring
reasoning about it is not a guard — a docstring does not raise.

### 3 — the `AMD_LIBRARY_ROOTS` warning's stated mechanism was false

Confirmed by measurement, not by argument. `scan_roots` receives the whole list and builds one
index across every root, so `library_roots[0]` is **never read at runtime** — `grep` finds it in
`hub/deploy/build_gate.py:170,183` and nowhere else — and both orders answer identically on the
real two-root library:

```
a-first  matched: ('9Lana/Let me battle (…) - Single', 'new-dl/9Lana/Let me battle (…) - Single')
b-first  matched: ('9Lana/Let me battle (…) - Single', 'new-dl/9Lana/Let me battle (…) - Single')
identical matched? True        identical resolved? True
```

**Trigger fixed in both files.** The hazard is *omitting* `/library/a`, not reordering. `.env.example`
and `compose.yaml` now say that every root is scanned, that order changes no answer, and that the
one mistake with no diagnostic is dropping the root the client writes to. compose.yaml also records
that the earlier version asserted the opposite.

**The test: re-grounded, not removed.** `test_the_clients_write_root_is_contained_in_the_scanned_roots`
now asserts **containment** — `/library/a` must be a *member* of every shipped default — which is the
real invariant. I kept it because the rule is true and worth pinning even though the runtime
configuration cannot be checked; what I removed was the false *rationale*, and the docstring now
says why the ordering version was wrong, so the false belief is not rediscovered either. A test
built on a false premise is worse than no test, and that was the state it was in.

**The owed check is written down, in the test as well as the docs.** Per the ruling, the
containment assertion is deferred to Phase 2 and recorded as a TODO in `.env.example` naming the
check, the file it belongs in (`create_app`'s lifespan, beside the three startup steps), the two
values an error must mention, and the reference to read the root out of the vendor config the way
`build_gate.py` does. `test_the_containment_check_is_owed_and_its_todo_names_where_it_belongs`
asserts the TODO exists and carries all four, so it cannot rot into "add validation somewhere", and
also asserts the false `KEEP /library/a FIRST` wording is absent from both files — by exact string,
because a check for the word "reorder" would pass on a rewording that kept the same wrong claim.

### 4 — the two stale format docs

`app.js` and `job_row.html` both documented a format the code no longer emits. Rendering is
format-agnostic, so nothing was functionally broken, which is why it went unnoticed. Both now
describe the literal `duplicate:` prefix followed by root-qualified absolute paths joined with `|`,
and both say why the bare relpath went.

**A first attempt made this worse and I caught it in the verification:** I "fixed" `app.js` by
*quoting* the old format literally in the history, so the file still contained
`duplicate:<relpath>|<relpath>` and anyone searching for the format would find two answers. Reworded
so the file states exactly one format and refers to the old one in prose.

`test_the_documented_skip_reason_format_is_the_one_the_code_emits` asserts the positive twice: the
code emits `duplicate:/library/a/artist/album`, and both docs name the prefix, say the paths are
root-qualified, and mention the `|` join and the `relpath` expression. It deliberately does not
assert the *absence* of the old string — that is a test which breaks on harmless rewording, and
which I had already tripped over.

### 5 — `library.html` rendered a bare relpath

The album object already carried enough: `_album_dict` builds `path` as `roots[root_index] /
relpath` — the same expression `DuplicateHit.resolved` is built from — so the template was showing
the worse of two values it was already holding. One token, `{{ album.relpath }}` →
`{{ album.path }}`, with the reasoning in a template comment.

Verified live, on all 4,739 rows, inside the container:

```
  album rows carrying a path: 4739
    is_dir=True  /library/a/9Lana/Let me battle (feat. つぐ, わかばやし & つぐ… ) - Single
    is_dir=True  /library/a/Addpico & 鹿あるく/溜息を触って (feat. 現実みろ) - Single
    is_dir=True  /library/a/Aiobahn +81/Galge - Single
  every album row path resolves? True
```

(The `&` renders as `&amp;` in the HTML, which is Jinja's autoescaping and correct; the check
unescapes before testing.)

### 6 — two accuracy items

- **The gitignore docstring understated the problem.** "a partial rootfs … 101 of 118 files" is the
  *wrapper clone's* fact. This repository tracks **0 of 118**, because `/wrapper/` is ignored
  wholesale at `.gitignore:5` — verified: `git check-ignore -v` attributes it to the root
  `.gitignore`, not `wrapper/.gitignore`, and `git ls-files wrapper/rootfs` returns nothing. The
  docstring now says so, explains why the two clones differ, and the test now *counts* it with
  `git ls-files` rather than describing it.
- **A mislabelled mutation.** "per_root dropped from the status response" mutated
  `api/library.py`, which is `/api/library/scan` — so a reader could conclude `/api/status` was
  never covered. It is, via `api/__init__.py::_library_summary`, which is a separate function and
  now has its own mutation. All three endpoints are mutated separately.

### On the tests

`mutation_check.py` is now 20 mutations (from 15), all caught. Five were added this round: the
defaulted `resolved`, the containment TODO, the reinstated false rationale, the album table's path
column, and the crash-restart poll.

Two of the new ones found real gaps, one of them in work done minutes earlier:

| mutation | why it survived | fix |
|---|---|---|
| album path column back to `relpath` | the existing page test asserts `"4pi" in body`, which a bare relpath also satisfies | `test_the_library_page_shows_a_path_that_resolves` names the exact qualified string |
| the stale `KEEP /library/a FIRST` mutation | its target string no longer existed, so the mutation did not apply | removed, and replaced by the mutation that *reinstates* the false wording — which is caught |

`hub/deploy/oversubscribe_check.py` is new and committed. It exists because "the test passed once"
is not evidence that a timing-sensitive test is sound, and the only way to tell a real race from a
slow machine is to make the machine slow deliberately and see the failure appear. The first
harness bug it found was in itself.

### Not changed

`matched` (byte-identical), `resolved` as a sibling field, the gate's six diagnostics,
`config.toml` construction, the healthcheck and `start_period`, secret hygiene, the `**/`-prefixed
`.dockerignore` family, `_excluded_from_context`, the `PYTHONPATH` table, the control's four
counters, `LibraryScan.per_root()` and its three-endpoint wiring, the two distinguishable zero
states on the library page, and the `docker compose cp` / boot-range / `perror` corrections.
