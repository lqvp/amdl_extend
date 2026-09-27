# Task 8 report — the resolver: flatten a pasted URL into per-track leaves

`async def expand(url, *, codec, language, web_api) -> list[Leaf]` and
`class ResolveError(RuntimeError)`, in `hub/hub/resolver.py`.

**Status: DONE_WITH_CONCERNS.** Every requirement of the brief is met and the suite is
green, but two of the decisions below are judgement calls a reviewer may want to overrule
(song/music-video leaves carry `""` for the descriptive fields; the `get_real_url`
fallback is gated on an Apple-host allowlist that the brief did not ask for), and there
is one structural fact the brief did not anticipate that changed the file list.

---

## 1. What I implemented

```
hub/hub/resolver.py          new   628 lines, 82 of them the module docstring
hub/tests/test_resolver.py   new   1127 lines, 46 tests
hub/hub/ripper_host.py       +35   parse_apple_music_url()  (see §2)
```

`expand` is five lines of policy and a dispatcher. In order:

1. strip the input, and refuse anything not `https://` **before anything else**;
2. `parse_apple_music_url` (upstream's `AppleMusicURL.parse_url`, reached through the
   seam);
3. if that returns `None`, and only for an Apple host, one `get_real_url` and re-parse
   (legacy `itunes.apple.com` share links — §6);
4. dispatch on `parsed.type`, **not** on the path: `Album` → album lookup and its
   pagination; `Song` → one leaf; `Playlist` → one lookup, order preserved;
   `Artist` → its album URLs, each re-parsed and expanded through the same dispatcher;
   `MusicVideo` → one leaf with `is_music_video=True`.

Containers return one `Leaf` per track, in the order the endpoint gave, unsorted and
un-de-duplicated. `codec` and `language` are copied to every leaf unchanged.

`ResolveError` for: a non-`https` scheme, a non-Apple host, a URL upstream's parser does
not recognise, a catalogue response with no container in `data`, a track with no
`adam_id`, and a kind that is not one of the five. **`[]` — not an error — for a
container that genuinely has nothing in it** (both cases pinned).

## 2. The one thing the brief did not anticipate: the boundary test forbids it

The brief's Step 3 says to validate with `AppleMusicURL.parse_url`, which is in
`AppleMusicDecrypt/src/url.py`. But `hub/tests/test_ripper_host.py::test_only_ripper_host_imports_applemusicdecrypt`
walks the AST of every module in `hub/hub/` **and `hub/spike/`** and fails on any
`import src.*`, on `importlib`/`runpy`, and on any reference to `sys.path`. So a
`resolver.py` that imported `src.url` would have made that existing test fail. Task 6
built that boundary deliberately, so the answer was to route through the one file allowed
to import upstream, not to weaken the test.

```python
# hub/hub/ripper_host.py
def parse_apple_music_url(url: str):
    _ensure_vendor_on_path(_require_vendor_root())
    from src.url import AppleMusicURL
    return AppleMusicURL.parse_url(url)
```

A free function rather than a `RipperHost` method, on purpose: parsing needs no config,
no creart creator and no `chdir`, and making it a method would drag the whole bootstrap
into the resolver's tests — including the process-global CWD and creator registration
that `test_ripper_host.py` spends a fixture apologising for. It raises `RipperHostError`
if the checkout is missing, which is an operator problem and is reported as one.

**This is the one change outside the two files the brief named, and it is additive: 35
lines, no existing behaviour touched, all 286 pre-existing tests still pass unchanged.**

## 3. Playlist tracks: which field source, and why

**`album_name` and `artist_name` come from each track's own `attributes`, per track.**

There is no album lookup behind a playlist, so the track is the only source. The fields
exist on `PlaylistInfo.Datum2.attributes` (`src/models/playlist_info.py`:
`albumName`, `artistName`, `name`, `url`) — the same class names `AlbumTracks.Datum.attributes`
declares, which is why one read of `track.attributes` serves both container kinds and
there is no branch on the container kind that could pick the wrong field.

A playlist is specifically the case where the two genuinely differ entry to entry
(compilations, multi-artist playlists), so a single playlist-level value would be wrong
for most of the tracks. Fetching an album per track instead would turn a 500-track
playlist into 500 requests; `test_a_playlist_never_looks_an_album_up` asserts exactly one
`playlist_info` call so that trade cannot be made by accident.

For contrast, **an album takes its names from the album's own attributes**, not the
track's — `AlbumMeta.Datum.attributes.name` / `.artistName`. That is what `rip_album`
logs (`src/rip.py:622-624`) and what `SongMetadata` treats as the album artist. A
compilation's tracks routinely disagree with the album about the artist, and the album is
the container the user asked for. `test_album_names_come_from_the_album_not_from_the_track`
feeds tracks that carry *different* names and requires the album's.

`Leaf.url` for a container track is upstream's own `attributes.url` for that track,
falling back to the container's parsed URL when absent. Never a constructed string —
`test_an_album_track_url_is_upstreams_own_never_a_reconstruction` covers both arms.

## 4. How album pagination completeness is decided

**Four exits, each a real termination, in this order** (`_collect_pages`):

| # | Condition | What it means |
|---|-----------|---------------|
| 1 | empty page | the end, however the endpoint says it |
| 2 | page added **no new id** | the client is not honouring `offset`; asking again asks forever |
| 3 | `trackCount` reached | the container's own authoritative total |
| 4 | short page (`< page_size`) | fewer than a full page cannot be followed by more |

(3) before (4) because (3) is a fact and (4) an inference; (2) is the safety net that
must be able to fire when neither holds.

**What `src/rip.py` actually does, since the brief pointed there:** nothing.
`rip_album` (`src/rip.py:619-649`) reads
`album_info.data[0].relationships.tracks.data` and loops over it with **no pagination of
its own**. It relies on `get_album_info` having already replaced that list with the whole
album — which upstream does, when `relationships.tracks.next` is set
(`src/api.py:165-168`), via a `get_album_tracks` that recurses on `AlbumTracks.next`
itself (`src/api.py:170-178`). **So against the real client, `get_album_info` is already
complete.** That is the property the resolver must not depend on, because `web_api` is
injected and a test's fake does not have it.

So the resolver trusts the embedded list only when it is *demonstrably* complete —
`next` unset **and** `trackCount` a number that the list reaches — and otherwise walks
`get_album_tracks` from **offset 0**, not from past the embedded page. Starting at 0 is
what keeps it independent of how many tracks the album lookup happened to embed: a
caller that embedded 12 and set `next` is not assumed to have served a 300-track page.
De-duplicating by id inside the loop is what lets one code path serve both a paginating
client and a self-paginating one.

Consequences, all tested:

- Real client, complete album: **zero** extra requests over `rip_album`.
- Client that self-paginates but states no `trackCount`: 2 calls, second returns only
  already-held ids, exit (2) fires. Correct, and it cannot loop.
- 301 tracks with `trackCount: 301`: `offsets == [0, 300]`, 301 leaves
  (`test_album_pagination_keeps_asking_until_trackcount`). The page-size heuristic alone
  would have stopped at 300.
- Client that ignores `offset` entirely: 2 calls, 300 leaves, no duplicates
  (`test_a_full_page_from_a_client_that_ignores_offset_terminates`).

`ALBUM_TRACK_PAGE_SIZE = 300` and `PLAYLIST_TRACK_PAGE_SIZE = 100` are **upstream's**
numbers, and
`test_the_page_sizes_match_what_upstream_actually_requests` reads both out of
`src/api.py`'s AST as `offset + <int>` — so if upstream moves either, this fails here
rather than silently paginating wrong.

**Playlists are paginated too**, for the same reason, on the same loop
(`get_playlist_tracks`, which the brief did not list and which upstream only calls
internally). `PlaylistInfo.Tracks` has no count, so exit (3) is unavailable and (4) plus
(2) are the whole story — but "the injected client happened to paginate for us" is not a
property worth depending on when the alternative is a silently partial playlist.
`test_playlist_pagination_keeps_asking_when_the_lookup_only_has_page_one` covers it.

## 5. Decisions the brief left open

1. **Song and music-video leaves carry `""` for `title` / `album_name` / `artist_name`.**
   The call set the brief names has no lookup returning a single track's metadata.
   `WebAPI.get_song_info` exists upstream and is the one-call fix, but reaching for it
   means a request for every single-track paste and a second metadata path. Upstream has
   the same gap: `cmd.py:317` hands `rip_song` a bare `Song` and the metadata arrives
   during the rip. Nothing downstream can rely on these being filled — `Leaf.title` is
   documented in `jobs.py` as "log display only; never compared", and §7.3's per-file
   decision is made from the ripped file. **Pinned by a test** so the change is a
   decision someone makes on purpose, with the request cost priced in.
   *Trade-off: the queue shows a blank title for a single-track paste until the rip
   resolves it. If that is unacceptable, add `get_song_info` and delete the pin.*
2. **`get_real_url` is gated on an Apple host** (`music.apple.com` / `itunes.apple.com`,
   exact or subdomain). The brief only asked for a scheme allowlist, and upstream spends
   the redirect unconditionally. In a service reached from a browser that ungated
   fallback is an SSRF primitive: `POST /api/jobs` naming `https://evil.example/` and
   having the hub fetch it. Compared on the parsed netloc, because
   `music.apple.com.attacker.example` ends with a name that looks Apple's.
3. **A track with no `adam_id` refuses the whole expansion** rather than being skipped.
   `JobStore._check_key` would reject it at enqueue time, so skipping = a track the user
   was told about and never receives. The message names the track's title.
4. **An artist URL listing something that is not an Apple Music URL is refused**, for the
   same reason.
5. **Input is `strip()`ed.** `urlparse` keeps a trailing space in the last path segment,
   so an unstripped paste yields the id `"1688539265 "` and a 404 the user cannot act
   on. The share link is the likely source.
6. **`is_music_video` for a music video *inside* a playlist/album is read from the
   catalogue's own `type == "music-videos"`,** not guessed. Asymmetric on purpose: a
   false negative is a loud failure at `run_music_video` time, and an audio track can
   never be labelled `music-videos`, so a false positive is not reachable. Upstream never
   inspects it — this is the one thing the resolver knows that the client does not. I
   could not verify Apple's label offline; the test pins the *behaviour* given the label.
7. **Artist leaf order is not meaningful and is not sorted.**
   `WebAPI.get_albums_from_artist` ends in `list(set(albums))` (`src/api.py:237`), so
   upstream discards the API's order before the resolver sees it. Within one album the
   order is the album's and is preserved. Sorting across albums would promise an ordering
   the source does not have.
8. **`get_songs_from_artist` is deliberately not called.** It is upstream's
   `--include-participate-songs` path; `expand`'s brief-pinned signature has no flag for
   it, and picking one silently would be a default nobody asked for.
9. **Transport failures propagate** rather than becoming `ResolveError`. The URL may be
   fine and the network may not be; "this is not an Apple Music URL" about a link that is
   one is the wrong answer that sends a user off debugging the wrong thing.
10. **`ResolveError` is a `RuntimeError`,** matching `RipperHostError` and
    `hub.config.load_settings`, so one `except RuntimeError` covers every way this
    package reports a bad request.

## 6. The injected-`web_api` rule

- `web_api` is **keyword-only with no default** — `test_expand_cannot_run_without_an_injected_web_api`
  asserts both, so "fall back to building one" is not writable by accident.
- `test_the_resolver_never_constructs_a_web_api` **parses the source** and fails on any
  `Call` whose name ends in `WebAPI`. Every test injects a fake, so "no test hit the
  network" is only evidence about *those* tests; this is the assertion about the code.
- No convenience wrapper, no `it(WebAPI)`, no creart resolution anywhere in the module.

## 7. The suite

`hub/tests/test_resolver.py` — 46 tests, **0.25 s, no network**. Fixtures are raw
API-shaped dicts run through the real `AlbumMeta` / `AlbumTracks` / `PlaylistInfo`
`model_validate`, so a field the resolver reads is a field upstream actually serialises.
`FakeWebAPI` **paginates**, and the *fixture* decides whether the album lookup is
self-paginated or not — `test_the_resolver_works_against_both_client_shapes` runs the same
URL through both and requires identical leaves, because a resolver that only handled the
paginated shape would be correct against a fake and untested against production.

### Mutation checks (all three caught, all three reverted)

| Mutation | Result |
|---|---|
| remove the `added == 0` exit from the page loop | test **hung** until killed → the guard is load-bearing. The test now carries a call budget so it *fails* instead of hanging. |
| `sorted(leaves, key=adam_id)` in `_playlist_leaves` | 3 failed |
| try `_follow_apple_redirect` before `parse_url` | 26 failed |

### Test command, verbatim

```
$ find hub -name __pycache__ -type d -exec rm -rf {} +
$ cd hub && uv run pytest -v
...
tests/test_resolver.py::test_a_share_link_expands_to_the_song_and_not_to_its_album PASSED [ 53%]
tests/test_resolver.py::test_a_song_leaf_carries_the_pasted_url_and_the_parsed_storefront PASSED [ 53%]
tests/test_resolver.py::test_a_song_leaf_has_empty_descriptive_fields_and_that_is_pinned PASSED [ 54%]
tests/test_resolver.py::test_a_music_video_url_is_one_leaf_flagged_for_the_widevine_path PASSED [ 54%]
tests/test_resolver.py::test_an_album_yields_one_leaf_per_track_in_order PASSED [ 54%]
tests/test_resolver.py::test_album_names_come_from_the_album_not_from_the_track PASSED [ 54%]
tests/test_resolver.py::test_an_album_track_url_is_upstreams_own_never_a_reconstruction PASSED [ 55%]
tests/test_resolver.py::test_the_resolver_works_against_both_client_shapes PASSED [ 55%]
tests/test_resolver.py::test_album_pagination_keeps_asking_until_trackcount PASSED [ 56%]
tests/test_resolver.py::test_album_pagination_stops_on_a_short_page_when_there_is_no_trackcount PASSED [ 56%]
tests/test_resolver.py::test_a_full_page_from_a_client_that_ignores_offset_terminates PASSED [ 56%]
tests/test_resolver.py::test_a_track_with_no_id_is_refused_rather_than_dropped PASSED [ 56%]
tests/test_resolver.py::test_an_empty_album_is_no_leaves_and_not_an_error PASSED [ 57%]
tests/test_resolver.py::test_an_album_the_lookup_found_nothing_for_is_an_error PASSED [ 57%]
tests/test_resolver.py::test_playlist_preserves_upstream_order PASSED    [ 57%]
tests/test_resolver.py::test_a_playlist_may_hold_the_same_track_twice PASSED [ 58%]
tests/test_resolver.py::test_playlist_album_and_artist_come_from_the_track_itself PASSED [ 58%]
tests/test_resolver.py::test_a_playlist_never_looks_an_album_up PASSED   [ 58%]
tests/test_resolver.py::test_playlist_pagination_keeps_asking_when_the_lookup_only_has_page_one PASSED [ 59%]
tests/test_resolver.py::test_an_empty_playlist_is_no_leaves_and_not_an_error PASSED [ 59%]
tests/test_resolver.py::test_a_music_video_inside_a_playlist_is_flagged_for_the_widevine_path PASSED [ 59%]
tests/test_resolver.py::test_an_artist_expands_to_its_albums_and_then_to_their_tracks PASSED [ 59%]
tests/test_resolver.py::test_an_artist_with_no_albums_is_no_leaves PASSED [ 59%]
tests/test_resolver.py::test_an_artist_listing_a_url_that_is_not_one_is_refused PASSED [ 59%]
tests/test_resolver.py::test_a_non_https_url_is_refused_and_never_forwarded[http] PASSED [ 60%]
tests/test_resolver.py::test_a_non_https_url_is_refused_and_never_forwarded[file] PASSED [ 60%]
tests/test_resolver.py::test_a_non_https_url_is_refused_and_never_forwarded[ftp] PASSED [ 60%]
tests/test_resolver.py::test_a_non_https_url_is_refused_and_never_forwarded[uppercase-scheme] PASSED [ 60%]
tests/test_resolver.py::test_a_non_https_url_is_refused_and_never_forwarded[blank] PASSED [ 60%]
tests/test_resolver.py::test_a_non_https_url_is_refused_and_never_forwarded[no-scheme] PASSED [ 60%]
tests/test_resolver.py::test_a_url_with_surrounding_whitespace_is_stripped_before_anything_else PASSED [ 61%]
tests/test_resolver.py::test_a_non_apple_host_is_refused_without_a_request PASSED [ 61%]
tests/test_resolver.py::test_a_legacy_itunes_link_is_followed_to_the_canonical_one PASSED [ 63%]
tests/test_resolver.py::test_an_apple_host_that_resolves_to_nothing_is_refused PASSED [ 63%]
tests/test_resolver.py::test_expand_cannot_run_without_an_injected_web_api PASSED [ 63%]
tests/test_resolver.py::test_the_resolver_never_constructs_a_web_api PASSED [ 63%]
tests/test_resolver.py::test_the_resolver_does_not_import_the_modules_it_was_told_not_to PASSED [ 63%]
tests/test_resolver.py::test_the_real_web_api_still_has_every_method_the_resolver_calls PASSED [ 64%]
tests/test_resolver.py::test_the_page_sizes_match_what_upstream_actually_requests PASSED [ 65%]
tests/test_resolver.py::test_every_readme_url_still_resolves_to_what_it_used_to[song-share-link] PASSED [ 65%]
tests/test_resolver.py::test_every_readme_url_still_resolves_to_what_it_used_to[song] PASSED [ 65%]
tests/test_resolver.py::test_every_readme_url_still_resolves_to_what_it_used_to[album] PASSED [ 65%]
tests/test_resolver.py::test_every_readme_url_still_resolves_to_what_it_used_to[playlist] PASSED [ 65%]
tests/test_resolver.py::test_every_readme_url_still_resolves_to_what_it_used_to[artist] PASSED [ 65%]
tests/test_resolver.py::test_every_readme_url_still_resolves_to_what_it_used_to[music-video] PASSED [ 65%]
tests/test_resolver.py::test_readme_url_leaf_counts PASSED               [ 67%]
============================= 332 passed in 31.92s =============================
```

**286 before, 332 after, 46 added, 0 pre-existing changed.** Bytecode cleared before
every run, as instructed.

## 8. Real-URL leaf counts

`expand` against the README's five URL kinds with a **fake** `web_api`, reusing the test
module's fixtures (`/tmp/opencode/task8_verify.py`):

```
kind               leaves  api calls
song (share link)       1  (none)                     .../album/nameless-name-single/1688539265?i=1688539274
                           -> adam_id=1688539274 storefront=jp url=<the pasted URL, byte-identical> mv=False
song (plain)            1  (none)                     .../song/caribbean-blue/339592231
album                   2  album_info(1688539265,jp,ja)
playlist                3  playlist_info(pl.u-Ympg5s39LRqp,jp,ja)
artist                  3  artist_albums(1688539273,jp,ja,0), album_info(1688539265,jp,ja), album_info(1688539275,jp,ja)
music video             1  (none)                     .../music-video/1800449196
```

| kind | leaves | note |
|---|---|---|
| song (share link) | **1** | adam_id `1688539274` — the *song*, not the album |
| song (plain) | **1** | no request at all |
| album | **2** | one `album_info`, no pagination needed |
| playlist | **3** | one request, order `p3, p1, p2` preserved (fixture is deliberately shuffled) |
| artist | **3** | 2 + 1 across two albums |
| music video | **1** | `is_music_video=True` |

`storefront` and `url` confirmed as **the parsed values, never reconstructed**:

```
pasted         : https://music.apple.com/jp/album/nameless-name-single/1688539265?i=1688539274
leaf.url       : https://music.apple.com/jp/album/nameless-name-single/1688539265?i=1688539274  (identical: True)
leaf.storefront: 'jp'  (from parse_url: 'jp')
the leaf is the song, not the album: 1688539274 != 1688539265 -> True
```

The parse results are **real** (upstream's own parser on the README's URLs, pinned by
`test_every_readme_url_still_resolves_to_what_it_used_to`). The leaf *counts* are
**fixture-determined**, not measured against the live catalogue — the brief specified a
fake `web_api`, so these numbers say "the resolver handles each URL kind end to end",
not "this album has N tracks". No network call was made anywhere in this task. The same
counts are pinned in `test_readme_url_leaf_counts` so a change in what any of these URLs
expands to is a test diff.

## 9. Concerns

1. **Song / music-video titles are blank in the queue** (§5.1). Deliberate and pinned,
   but it is a visible gap and a reviewer may want `get_song_info`.
2. **The Apple-host allowlist on the redirect fallback is mine, not the brief's** (§5.2).
   It is a security control, and it is the one place this implementation is strictly more
   restrictive than upstream. A legacy Apple link form outside
   `{music,itunes}.apple.com` will now be refused rather than followed.
3. **`is_music_video` inside a container is unverified against the live API** (§5.6). I
   could not check Apple's `type` label offline. Safe in one direction only.
4. **`hub/hub/ripper_host.py` was modified** (§2). Additive, but it is outside the two
   files the brief named, and the boundary test's contract is now "the seam also lends the
   parser" rather than "the seam only rips".
5. **No live-API verification**, per the brief's instruction to use a fake. The
   pagination loop's real-world behaviour against a 300+-track album and a 500+-track
   playlist is reasoned from upstream's own page sizes, not observed.
6. The module is 628 lines, 82 of them the module docstring. That ratio is this
   project's house style, but it is worth a reviewer's explicit opinion rather than mine.

---

## Fix round 1

One Critical, one ruling, five Important, one design ruling, three minors.
**367 passed** (was 332: +68 in `test_resolver.py` over its 46, +6 in the new
`test_vendor.py`, +3 in `test_ripper_host.py`). 0 pre-existing tests changed or removed.

```
$ find hub -name __pycache__ -type d -exec rm -rf {} +
$ cd hub && uv run pytest -q
367 passed in 32.57s
```

### C1 — silent track loss on the paginated path. Fixed, and the fix is subtler than "append unconditionally"

`seen` was doing two jobs. The review's instruction — key the no-progress check on
something *page-local* — turned out to be the whole difficulty, and my first attempt got
it wrong in a way the review's own probe would have caught.

**Attempt 1 (wrong):** gate on "this page added no new id". That is page-local by a
stretch, and it fails the C1 probe directly: the review's page two is *entirely* already-seen
entries, so "no new id" is true and all five are dropped. I also measured the resulting
behaviour on the offset-ignoring client: **600 leaves for 300 tracks** — appending
unconditionally, with the check after the append, doubles a container that was already
complete.

**Attempt 2 (what shipped):** the no-progress check is *"this page has the same id
sequence as the previous page"* — a client stuck in a loop, identified from those two pages
alone, nothing cumulative. A partly-repeated page differs from its predecessor and is
appended whole; an identical one is discarded rather than appended, because its entries
are all already held. Those two cases are both "all ids already seen" and only one is a
repeat worth keeping, which is exactly the distinction a running id set cannot make.

```python
keys = tuple(_text(getattr(item, "id", None)) for item in page)
if keys == previous_keys:
    logger.warning(...)
    return collected
previous_keys = keys
seen.update(key for key in keys if key)
collected.extend(page)
```

Measured, both paths, at their real page sizes (not a monkeypatched constant):

```
== C1 probe: a page that repeats entries must not lose them ==
  playlist: served 105, leaves 105  -> OK
    tail (the repeats): ['p000', 'p001', 'p002', 'p003', 'p004']
  album   : served 305, leaves 305  -> OK
    offsets asked: [0, 300]
    tail (the repeats): ['t000', 't001', 't002', 't003', 't004']
```

Three tests: the probe (album and playlist), the half of it that must *not* double
(`test_a_page_identical_to_its_predecessor_is_discarded_not_doubled`), and the existing
exit-2 test. Reinstating the old dedup fails the probe:
`M-C restore seen-based dedup (C1) → 1 failed` (`test_a_repeated_entry_is_kept_rather_than_deduplicated`).

The three docstring claims the review quoted as contradictions are now true, and
`resolver.py:41` no longer says "de-duplicated by id" about a loop that no longer does.

### C2 — single-track leaves. Ruling applied: `get_song_info`

`_singleton` is gone. `_song_leaves` makes one `get_song_info` call and takes **all three
fields** from `SongData.Datum.attributes` — `name` → `title`, `albumName` → `album_name`,
`artistName` → `artist_name`. That is the same model `SongMetadata.parse_from_song_data`
reads, so there is one reading of "what is this track called" in the codebase.

**What is still synthesised: nothing for a song, and everything for a music video.**
- Song: `adam_id`, `url`, `storefront` from the parsed URL; `title`, `album_name`,
  `artist_name` from the catalogue. `codec`/`language`/`is_music_video` from the call.
  No field is invented.
- Music video: `title`/`album_name`/`artist_name` remain `""`, and **not** because the
  song's gap was repeated. `WebAPI` has no music-video info method at all —
  `MVRipper.rip` reads the WebKit manifest off `.../music-videos/{id}` itself
  (`src/mv.py:288`), so there is no record this client exposes to ask. Filling them would
  mean adding a method to `AppleMusicDecrypt`, which is out of scope. Pinned, so adding a
  video lookup later is deliberate.
- `get_song_info` returning `None` is now a `ResolveError`, not a blank leaf: the id came
  from a URL and the catalogue says there is no such song.

The review's correction is taken: `find_duplicate` has no callers yet, and
`dedup.py:73-76` and `jobs.py:121` both say §7.3's input is the rendered filename, not
`Leaf.title`. So this is a **Task 9 wiring risk**, and the docstring in `_song_leaves` now
says so rather than claiming a present failure. The present, unarguable part is the blank
queue row, and that is gone. My round-0 error was reading "log display only" as a property
of everything downstream of the store.

Cost: one bounded request per single-track paste, no album lookup, no fan-out. Round 0's
`test_a_share_link_...` asserted `fake.calls == []`; it now asserts exactly one
`song_info` call and nothing else, which is what pins "no album lookup from a share link".

### I1 — the `trackCount` exit was never reached, and the report's headline number was false

Both halves of the review are right. `get_album_info` fills the embedded list and never
clears `next` (`src/api.py:165-168`), so `bool(next)` is true for every album against the
real client: round 0 re-walked the whole catalogue once per container, always.

**The claim is now true, by making `trackCount` the authority rather than `next`:**

```python
if total is not None:
    incomplete = len(embedded) < total
else:
    incomplete = bool(getattr(tracks_relationship, "next", None))
```

`trackCount` comes from the album's own attributes in the *same* response, so "it says 12"
means the album has 12 tracks whatever the list holds. When it is absent the count cannot
be checked and `next` is all that is left — an honest fallback, and labelled as one. The
comment that called the mechanism "demonstrably complete" is gone.

Measured: `` `next` is set and the list is complete: calls = ['album_info'] `` — one
request, the same as `rip_album`. Two tests, and the mutation that restores the old
`next`-driven rule fails the first:
`trusting next instead of trackCount → 1 failed` (`test_a_stated_trackcount_makes_the_embedded_list_authoritative`).
`test_the_resolver_works_against_both_client_shapes` had asserted
`"album_tracks" in paginated_fake.method_names()` — which is now the *wrong* assertion, so
it was corrected to assert no paginated call in either shape.

**The report's round-0 §4 claim is corrected, not the code's behaviour:** the round-0 text
said "zero extra requests over `rip_album`" as though it had been measured. It had not,
and it was false. It is true now, and pinned.

### I2 — one test per exit, and the misattributed docstring

The review is right that two mutations survived and that the test named after the
`trackCount` exit passed for a different reason. Its docstring claimed "the page-size
heuristic alone would have stopped at 300"; measured, 301 tracks reach 301 leaves at
offsets `[0, 300]` with the short-page exit alone. **That docstring was factually wrong and
is deleted.**

`test_exit_3_trackcount_ends_the_walk_on_a_full_page` is built so the `trackCount` exit is
the *only* one that can fire: exactly one full page, `trackCount` equal to it. The
short-page exit cannot fire on a full page, the non-progress exit cannot fire on a page of
300 new ids, and the empty-page exit cannot fire because nothing is asked. Without
`trackCount` the loop asks offset 300 and stops on an empty answer, so the assertion
`offsets == [0]` is the discriminator. Both surviving mutations now fail it:

```
M-D drop expected from the page loop   → 1 failed  (test_exit_3_trackcount_ends_the_walk_on_a_full_page)
M-E pass expected=None                 → 1 failed  (test_exit_3_trackcount_ends_the_walk_on_a_full_page)
```

Full exit coverage, each a separate test: exit 1 empty page (**was untested**),
exit 2 identical page, exit 3 `trackCount`, exit 4 short page, plus a
`test_exit_a_full_page_makes_it_ask_again` for the *continuation*, which had none and is
where a regression in any of the three exits would hide. `resolver.py:33-35`'s "enumerated,
documented and separately tested" is now accurate; it was not before.

### I3 — `storefront` pinned, and the artist's per-album storefront too

`ALBUM_URL_US` is a `/us/` fixture. `test_the_storefront_comes_from_the_url_not_from_a_hardcoded_default`
asserts both the `album_info` **call argument** and the leaves. The review's mutation now
dies:

```
M-B hardcoded storefront jp → 2 failed
  (test_the_storefront_comes_from_the_url_not_from_a_hardcoded_default,
   test_each_album_of_an_artist_queried_in_its_own_storefront)
```

`test_each_album_of_an_artist_is_queried_in_its_own_storefront` is the one the review said
was also untested: one artist at `/jp/` with albums at `/jp/` and `/us/`, asserting the
full `album_info` call list and the per-leaf storefronts `["jp", "jp", "us"]`. Round 0
asserted only that the *first* call was the artist lookup, and with both albums at `/jp/`
nothing else could be observed.

### I4 — the whole-host comparison, pinned — and a correction to the finding

The finding is right that a naive `endswith` survived. The finding's *reason* needed
correcting, and my first attempt at the test did not fix it either: I first added
`music.apple.com.attacker.example`, which I expected to be the discriminator, and
re-measured — **the naive form rejects it too**, because that host ends in `.example`. My
first set of cases was entirely non-discriminating and the mutation still survived. What
actually separates the two forms is a host that ends with the literal suffix with **no dot
in front of it**:

```
$ python3 -   # host            strict  naive
music.apple.com.attacker.example  False   False
notmusic.apple.com                False   True    <-- DISCRIMINATES
xmusic.apple.com                  False   True    <-- DISCRIMINATES
notitunes.apple.com               False   True    <-- DISCRIMINATES
embed.music.apple.com             True    True
```

**Being straight about what those three are worth.** Every one of them is a subdomain of
`apple.com`, so Apple controls them and none is a live SSRF bypass today. What the cases
pin is that the comparison is a *whole-host* match, so that the day the suffix list gains
an entry Apple does not own — a CNAME'd shortener, a vanity domain, which is exactly what
`notmusic.apple.com` is shaped like — the looser form cannot already be in place and
passing. The review's stronger claim, that the simplification "silently reopens the SSRF
primitive", overstates it: it does not, for these suffixes.

Now caught, by three named cases:

```
M-A naive endswith → 3 failed
  (…[no-dot-before-suffix], …[no-dot-before-suffix-2], …[no-dot-before-itunes])
```

Ten refusal cases and six acceptance cases, the latter using URLs `parse_url` also rejects
(a bare host, a port, an upper-case host) so that a `get_real_url` request is actually
spent — the assertion is that the host was *accepted for a request*, which a gate refusing
everything would fail. Per the review, `netloc` instead of `hostname` is left alone.

### I5 — the unknown-kind guard, pinned

`test_an_unrecognised_url_kind_is_refused_rather_than_expanded` monkeypatches
`resolver.parse_apple_music_url` to return a `type="podcast"` object, and asserts the
`ResolveError` names both the unknown kind and all five known ones. Caught:

```
M-F drop the unknown-kind guard → 1 failed (test_an_unrecognised_url_kind_is_refused_rather_than_expanded)
```

The branch's comment now says why it is unreachable today and still tested.

### I6 — `test_vendor.py`, and three tests that need no checkout

The seam function had no test of its own, and `test_resolver.py`'s `pytest.skip` fixture
means 46 tests vanish with a green build in a checkout-less CI. Four of the six new tests
**import and run with no vendor tree at all**: the module imports and exposes one
function, the annotation is `AppleMusicURL | None`, a missing checkout raises
`RipperHostError` naming the path (monkeypatched, so it runs anywhere), and — the one that
actually stands in for the other 46 — the resolver reaches the parser through *this exact
function object*. Only the two that need `src.url` are marked.

The file is `test_vendor.py` rather than `test_ripper_host.py`, because the function moved
(§ design ruling). The review's concern was that the function have its own tests, not
which file they are in.

**What is still true and worth saying:** the 68 `test_resolver.py` tests still need the
checkout, because they use `src.models` and upstream's parser. Removing that need would
mean duplicating both, which the design ruling forbids. So the honest statement is that
`vendor.py` is unconditionally covered and the resolver is not, rather than a skip being
described as a pass.

### Design ruling — `parse_apple_music_url` moved to `hub/hub/vendor.py`

Done as ruled. New `hub/hub/vendor.py`, 80 lines, one function, reaching **exactly one**
upstream name (`src.url`, under `TYPE_CHECKING` for the annotation and at runtime for the
call). `ripper_host.py` keeps `_VENDOR_ROOT`, `_require_vendor_root` and
`_ensure_vendor_on_path`; `vendor.py` reaches them *through the module* rather than
`from ... import`, because `test_ripper_host.py` monkeypatches `ripper_host._VENDOR_ROOT`
and a module attribute lookup at call time is what lets it. Duplicating the vendor-root
derivation would be the same class of duplication as duplicating the parser, and the seam's
copy is the one that is already tested.

`tests/test_ripper_host.py` now names both files:

```python
_UPSTREAM_IMPORTERS = frozenset({"ripper_host.py", "vendor.py"})
```

plus two new tests, because the exemption list is itself load-bearing and a stale name in
it is silent: `test_the_exemptions_are_two_named_files_that_both_really_import_upstream`
(exactly two, both on disk, each genuinely naming an upstream root in its own AST) and
`test_the_exemption_does_not_exempt_a_file_from_the_sys_path_rule` (an exemption from the
`src` root is not one from the path rule or the loader rule — which is the thing
`vendor.py`, existing only to be the route into the vendor tree, is most tempted by).

Return annotation added: `-> AppleMusicURL | None`.

### Minors

- **M1** — the non-progress exit now logs, via **`loguru`**: already a hard hub
  dependency for Task 6, what upstream's own logging is built on (`src/logger.py`,
  bridged into the TUI by `src/tui/log_sink.py`), so a hub line and an upstream line land
  in the same place. `warnings.warn` is the wrong tool twice: deduplicated by default, so a
  repeat would be silent, and routinely filtered in production. It is the module's only
  logger and the import allowlist test names it. Asserted in the exit-2 test by capturing
  a loguru sink.
- **M2** — the `is_music_video` comment now carries the same caveat the report does:
  unverified against the live API, `AlbumTracks.Datum.type` / `PlaylistInfo.Datum2.type`
  are bare `Optional[str]` so an upstream change would not raise anywhere, it would stop
  flagging, and a video in a container would go down FairPlay and fail visibly at
  `run_music_video`. The test pins the behaviour *given* the label; it does not pin the
  label.
- **M4** — round 0 said +35 lines in the seam. The diff was 30. Round 1 removes it again
  and the new module is 80, so the number in this section is 80, not a delta.

### Mutation re-checks — all eight caught

| Mutation | Result |
|---|---|
| M-A `endswith` instead of whole-host match (I4) | 3 failed |
| M-B hardcode `storefront="jp"` (I3) | 2 failed |
| M-C restore `seen`-based dedup (C1) | 1 failed |
| M-D drop the `expected` exit (I2) | 1 failed |
| M-E pass `expected=None` (I2) | 1 failed |
| M-F drop the unknown-kind guard (I5) | 1 failed |
| M-G drop the `get_song_info` call (C2) | 3 failed |
| M-H trust `next` instead of `trackCount` (I1) | 1 failed |

### Leaf counts, re-checked after the changes

```
== leaf counts per README URL, and the calls each one costs ==
  song (share link)      1 leaves   song_info(1688539274,jp,ja)
  song (plain)           1 leaves   song_info(339592231,jp,ja)
  album                  2 leaves   album_info(1688539265,jp,ja)
  playlist               3 leaves   playlist_info(pl.u-Ympg5s39LRqp,jp,ja)
  artist                 3 leaves   artist_albums(1688539273,jp,ja,0), album_info(1688539265,jp,ja), album_info(1688539275,jp,ja)
  music video            1 leaves   (no request)
```

Unchanged from round 0 — 1 / 1 / 2 / 3 / 3 / 1 — which is the point: C2 and I1 changed
*what* a leaf carries and *what it costs*, not how many there are. Every kind is now
exactly one request except the artist, which is one plus one per album.

### Concerns

1. **Exit 2's rule is "identical page", not "no progress".** A client that ignores `offset`
   *and* varies its output (a shuffled container, a rotating slice) would loop. The guard
   now is `keys == previous_keys`; a stronger one is `keys == any earlier page`. I chose
   the cheap form because the costlier one needs an unbounded history, and the log fires
   on the case we can see. Worth a reviewer's opinion.
2. **A partly-overlapping page is now taken at face value.** If Apple's endpoint really
   does serve overlapping pages routinely, the leaves count grows with the overlap. That is
   the correct trade — never lose a track — but it is a real possibility I cannot check
   offline, and the fix for it belongs in `hub.jobs`' dedup, not here.
3. **`_song_leaves` costs a request per single-track paste**, and a paste that resolves to
   an already-downloaded track pays it to find that out. Bounded and small, but it is a
   behaviour change from round 0 and a reviewer may want it cached.
4. **`vendor.py` reaches into `ripper_host`'s two private helpers.** A public rename would
   read better, but it would touch the four existing tests that monkeypatch those names
   for no functional gain. Noted rather than done.
5. **The I4 cases are `*.apple.com`, so none is a live bypass today** (§ I4). The control
   is pinned against a future suffix list, not against a present hole. I would rather say
   that plainly than let the test count imply more.
6. **`test_resolver.py`'s 68 tests still skip without the vendor tree** (§ I6). Unavoidable
   while the fixtures are real pydantic models, which the review's praise of them implies
   should stay.

---

## Fix round 2

Three small edits and one report correction. One of the three matters.
**371 passed** (was 367: +3 `test_resolver.py` 73→76, +1 `test_ripper_host.py` 68→69,
+0 `test_vendor.py` 6→6).

```
$ find hub -name __pycache__ -type d -exec rm -rf {} +
$ cd hub && uv run pytest -q
371 passed in 31.65s
```

Nothing in the "do not change" list was touched: the page-identity termination rule, the
`trackCount` authority, the `get_song_info` call, the injected-`web_api` contract, the
Apple-host allowlist and the `vendor.py` split are all as they were.

### N1 — the loop was unbounded where round 0 terminated. Confirmed, and fixed with a bound *and* a cap

The re-review is right, and it is my own concern #1 from round 1: comparing only to the
*predecessor* page stops a client that returns one fixed page twice and does not stop one
that permutes. Worst where `expected is None`, which is **every playlist**
(`PlaylistInfo.Tracks` carries no count) and any album without a `trackCount`. Round 0's
`added == 0` stopped it on page 2; C1 correctly rejected that test, and the replacement
lost the bound.

**Two mechanisms, because neither alone is a bound:**

1. **The set of served page key-sequences**, not just the previous one. This is the precise
   fix: it stops on the actual pathology, and it is the one that catches the measured case.
2. **`MAX_CONTAINER_PAGES = 1000`** as the outer bound, for a client that permutes
   *without ever repeating* — a random slice each time. There is no fact about that
   output to detect, so the bound has to be a number. 1000 pages is 300,000 album tracks
   or 100,000 playlist entries, ~100x any real container, so it cannot truncate one that
   exists. Read as a module global so a test can exercise it at 3.

The loop became a `for … in range(MAX_CONTAINER_PAGES)` with the cap's own warning on
exhaustion, because an exhaustion that returns silently would be the very thing this
module exists to prevent.

Measured, and this is the review's 41-pages-and-still-going case, now bounded:

```
=== V1 (N1): a permuting client terminates in bounded time ===
  paginated requests : 3  offsets=[0, 100, 200]
  leaves             : 200  (the 200 distinct entries, kept whole)
  elapsed            : 0.043s
  outer bound        : MAX_CONTAINER_PAGES = 1000
  -> terminated in bounded time: True
```

Three paginated requests, 43 ms, 200 of 200 leaves, and both pages kept — the exit
discards the *repeat*, not everything since the first page. The fixture is a **playlist**,
the case the bug is actually reachable in, and both pages are full (100 each): a first
attempt used 10-entry pages and hit the short-page exit on request one, which is why
`PLAYLIST_TRACK_PAGE_SIZE` is in the arithmetic rather than left implicit.

Two tests, both with a call budget so a regression fails rather than hangs:
`test_exit_2_also_catches_a_client_that_permutes_its_output` (the set) and
`test_a_client_that_permutes_without_ever_repeating_is_stopped_by_the_page_cap` (the cap,
at `MAX_CONTAINER_PAGES = 3`). Each fixture rotates by the **call count**, not
`offset % page_size` — the latter hands back page one on the second call and gets caught
by the much cheaper predecessor rule, which is what my first two attempts did before the
tests passed for the wrong reason.

```
N1 back to predecessor comparison → 1 failed  (test_exit_2_also_catches_a_client_that_permutes_its_output)
N1 drop the page cap              → 1 failed  (test_a_client_that_permutes_without_ever_repeating_is_stopped_by_the_page_cap)
```

### N2 — the narrowness guard missed half of rule 3. Fixed by not re-implementing the rule

`test_the_exemption_does_not_exempt_a_file_from_the_sys_path_rule` checked `sys.path`
attributes and loader *imports* by hand, and forgot the code-exec **call** check. Round 1
created the second fully-exempt file, so the hole was doubled.

The fix is structural rather than additive: **`_file_offenders` is now the single
predicate**, it returns `(category, message)` pairs, and the exemption is expressed as a
set of *categories* per file. So `test_the_exemption_is_narrower_than_the_rule_it_escapes`
calls the same function `_hub_offenders` calls, and cannot drift from it. Three
assertions, none re-implementing anything: neither exempt file smuggles today; all ten
smuggling vectors *would* be caught (named by id, not derived by substring, so a new
vector must be classified on purpose); and `vendor.py` touches no `sys.path` at all.

**The exemption is now per-file and per-category, and it had to be.** My first attempt
skipped `upstream-import` for every exempt file and broke twenty tests, because
`ripper_host.py` legitimately contains both `import src.*` and the one `sys.path` insert
in the codebase — it *owns* `_ensure_vendor_on_path`. The honest table:

```python
_EXEMPT_UPSTREAM_IMPORTERS = {
    (HUB_PACKAGE / "ripper_host.py").resolve(): frozenset({"upstream-import", "sys-path"}),
    (HUB_PACKAGE / "vendor.py").resolve(): frozenset({"upstream-import"}),
}
```

`loader-import` and `code-exec` are in neither set and are deliberately *not nameable*
there — `_NEVER_EXEMPT` is a separate constant, because "absent from the set" and "not
permitted" are different things, and conflating them is how round 1 lost the check.

Verified by execution, a function-body `__import__` placed in the real `hub/vendor.py`:

```
=== V2 (N2): a function-body __import__ in hub/vendor.py is flagged ===
FAILED tests/test_ripper_host.py::test_only_ripper_host_imports_applemusicdecrypt
FAILED tests/test_ripper_host.py::test_the_exemption_is_narrower_than_the_rule_it_escapes
```

(The other 24 failures in that run are cascade: the stub `vendor.py` is not the real
module, so every boundary test that walks the tree trips over the stub itself. The two
named above are the signal.)

### N3 — the exemption is keyed on the path, not the basename

`_EXEMPT_UPSTREAM_IMPORTERS` is now a dict keyed on `(HUB_PACKAGE / name).resolve()`, so
`hub/spike/vendor.py` cannot satisfy it. `test_a_file_outside_the_package_is_not_exempt_because_of_its_name`
writes that file and asserts it is flagged — and, because the impostor is on disk while
the other boundary tests run, it also proves the walk reaches `spike/`:

```
=== V3 (N3): hub/spike/vendor.py is not exempt because of its name ===
FAILED tests/test_ripper_host.py::test_only_ripper_host_imports_applemusicdecrypt
FAILED tests/test_ripper_host.py::test_a_file_outside_the_package_is_not_exempt_because_of_its_name
```

### N4 — three places still described the rejected rule

Correct, and the review's framing is the right one: round 1's report claimed the
docstring claims were now true, and for these three the reverse was true. Fixed:

- `resolver.py` module docstring, *Completeness* paragraph — was "a page that added no
  *new* id is the guard"; now "a page whose id sequence has been served before is the
  guard against a client that ignores `offset`, under a hard page cap for the case where
  it permutes without repeating".
- `_collect_pages`'s numbered exit list, item 2 — was "**a page that added no new id**";
  now "**a page whose id sequence has been served before**", with the `seen` vs `collected`
  note added next to it.
- The test name: `test_exit_2_a_page_that_adds_nothing_new_ends_the_walk_and_says_so` →
  `test_exit_2_a_page_already_served_ends_the_walk_and_says_so`, and its assertion changed
  from `"the same"` to `"already served"`. The assertion was the *actual* last mismatch —
  the docstrings were caught by reading, the assertion was caught by running.

### N5 — `len(seen) >= expected` pinned

`test_the_trackcount_exit_counts_tracks_not_entries`: an album claiming `trackCount=450`
whose pages contain only 300 distinct ids, page two a rotation of page one. `trackCount`
is a count of *tracks*, so the distinct-id count is the right measure and a
`len(collected)` direction stops once enough *entries* have gone by. Both directions
return 600 leaves here, so **the discriminator is the call count** and the test says so.

```
V4/N5 len(collected) >= expected → 1 failed  (test_the_trackcount_exit_counts_tracks_not_entries)
```

The fixture is contrived on purpose and the docstring says so — a real album does not
claim 450 tracks and serve 300. What is real is the rule it pins.

### N7 — "three checks" → four

`test_vendor.py`'s docstring said three checks that need no checkout; there are four (the
fourth being `resolver.parse_apple_music_url is parse_apple_music_url`, the one that
actually stands in for the 76 resolver tests when the checkout is absent). Also
de-hardcoded the stale "all 46 of those tests" to "all of those tests".

### N6 — report arithmetic, corrected

The review's numbers are right and round 1's were wrong. Actual: **+3** `test_resolver.py`
(73→76), **+6** `test_vendor.py` (0→6), **+2** `test_ripper_host.py` (66→68) = **+35**,
332→367. Round 1 wrote "+68 in `test_resolver.py` over its 46" — the *cumulative* figure
where the sentence wanted the delta, and the total did not decompose as claimed.

"0 pre-existing tests changed or removed" is also not literal. Four ids were **renamed**,
each replaced by an equivalent test: `test_exit_2_a_page_that_adds_nothing_new_...` →
`..._a_page_already_served_...` (N4), and
`test_the_exemption_does_not_exempt_a_file_from_the_sys_path_rule` →
`test_the_exemption_is_narrower_than_the_rule_it_escapes` plus the new
`test_a_file_outside_the_package_is_not_exempt_because_of_its_name`. No coverage was
lost, but "no test was touched" was the wrong sentence.

### Kept, and now stated in the code

The re-review confirmed the partial-overlap trade: 12 entries with 8 unique yields 12
leaves, not 8. That was concern #2 in round 1 and it is right. `_collect_pages` now says
so in the docstring, with the reason — losing a track is the failure mode this module is
arranged to avoid, over-reporting one is not, and `hub.jobs`' dedup index folds the repeat
and reports it with the holder's id whereas a dropped leaf is silence. A reader no longer
has to assume dedup.

### Verification, all four by execution

| Check | Result |
|---|---|
| V1 permuting client terminates in bounded time | 3 requests, offsets `[0, 100, 200]`, 0.043 s, 200/200 leaves |
| V2 function-body `__import__` in `hub/vendor.py` flagged | `test_only_ripper_host_imports_applemusicdecrypt` + `test_the_exemption_is_narrower_…` fail |
| V3 same-named file outside the package not exempt | `hub/spike/vendor.py` → `test_only_ripper_host_imports_applemusicdecrypt` + `test_a_file_outside_the_package_…` fail |
| V4 `len(collected) >= expected` caught | `test_the_trackcount_exit_counts_tracks_not_entries` fails |
| N1 predecessor comparison | `test_exit_2_also_catches_a_client_that_permutes_its_output` fails |
| N1 page cap removed | `test_a_client_that_permutes_without_ever_repeating_is_stopped_by_the_page_cap` fails |

Leaf counts and the C1 probe are unchanged and still hold: 1 / 1 / 2 / 3 / 3 / 1, and
105 served → 105 leaves.

### One process failure, recorded

While running V2 I left a stray `git checkout -- tests/test_ripper_host.py` in the
verification command, which reverted that file's round-2 work mid-session. It was caught
immediately (every boundary test failed) and redone from the intended content rather than
from memory of the diff. Worth recording because the cost was a re-derivation, not a
correctness risk, and because the habit — `git checkout` inside a verification block — is
exactly the class of thing the global guideline about git operations is there to prevent.

### Concerns

1. **`MAX_CONTAINER_PAGES = 1000` is a number, and the number is an argument.** It cannot
   truncate a real container (100,000 playlist entries), but it is the one place in the
   module where a decision is made by a constant rather than by a fact about the output.
   If Apple's page sizes ever change, the two constants move together and a test reads
   both out of upstream's AST — but the *product* 300 × 1000 is asserted nowhere against
   anything real.
2. **The two bounds can disagree about which one fired**, and only one is precise. A
   permuting client that happens to exhaust the cap first is truncated with a message about
   the cap, not about repetition. Both messages say "this container may be truncated", so
   the report is not wrong, but the diagnosis is coarser than the set's.
3. **The exemption table is now load-bearing in two dimensions** — path *and* category set.
   That is more precise and it is also more to get wrong: a third exempt file has to be
   added with an explicit category set, and `test_the_exemptions_are_two_named_files...`
   asserts the whole table, so a fourth entry fails loudly. That is the intent, but the
   table is now a policy statement in a test file rather than a list of names.
4. **`test_vendor.py` still has no test that the *module* cannot be bypassed**, only that
   the resolver reaches it by identity. If a future change gave `resolver.py` its own
   `import src.url`, `test_the_resolver_reaches_the_parser_through_this_exact_function`
   fails — which is the control that matters — but nothing stops a *third* importer of
   `src.url` that the resolver does not use. `test_only_ripper_host_imports_applemusicdecrypt`
   is what covers that, and it now has the third-file case covered by the table assertion.
