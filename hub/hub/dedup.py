"""Album-scoped duplicate detection for downloads (spec §7.3 Steps 2–3, §7.4).

The question this module answers is "is this track already on disk?", and the answer has
to be scoped to the album. Measured on the 341 GB external library (§7.2.1), `intro`,
`escapism`, `mu` and `yoake` each appear in **six** different albums, and a normalized
title is shared by 2+ album directories for 1,207 of 8,721 titles -- 13.8%. A global
title match would therefore skip real downloads at a rate no user would accept. The
scoping is not a refinement; it is the only reason this is safe.

What enforces it is one equality, not a container: a directory is a candidate only when
the album **name** it carries equals the album name being downloaded, and the title is
then tested only against those. `scan.by_name` is how that candidate set is obtained in
a single dict lookup, not what makes the answer right -- filtering `scan.albums` by the
same name equality would be equally correct and only slower. So the invariant worth
protecting is the name equality, and the tests here pin it through the observable answer
(a hit names directories of one album and never of another) rather than through which
container the candidates came from.

`find_duplicate` is **pure**: it reads a `LibraryScan` that has already been taken and
touches nothing else. No filesystem access, no config, no clock, no network. The caller
decides when to scan (§7.1: the filesystem is the only source of truth and a scan costs
0.09 s, so it is re-taken per request) and which `artist_scope` to apply, which is why
`artist_scope` is a keyword argument and is never read from settings here. Task 9 owns
that wiring; a config read inside this function would make the result untestable and
would put a policy decision in the middle of a pure comparison.
"""

from __future__ import annotations

from dataclasses import dataclass

from hub.library_scan import LibraryScan, album_key
from hub.normalize import normalize

# The two modes of §7.4, and the only two this function accepts. A closed set, not a
# truthy/falsy switch: an unrecognised value is a typo in a config literal, and silently
# reading it as `loose` would either skip tracks the user asked for (if the typo was meant
# to be `strict`) or fail to skip tracks that are already on disk. Raising puts the error
# where the value came from, which is the only place it can be fixed.
ARTIST_SCOPES: frozenset[str] = frozenset({"loose", "strict"})


@dataclass(frozen=True, slots=True)
class DuplicateHit:
    """The album directories that already hold this track, in two forms.

    **The two forms are not interchangeable, and the difference is not cosmetic.**
    `find_duplicate` builds its candidates out of `scan.by_name`, which spans *every*
    configured root. So `matched` -- bare `AlbumDir.relpath` values -- says which directory a
    track was found in but not *which library it is in*, and that is not a detail:

    - A relpath is not resolvable once there is more than one root. One album name filed
      under both roots yields `9Lana/x` and `new-dl/9Lana/x`, and neither string exists
      under both roots. §7.4's entire adjudication story is "a user can overrule a `loose`
      false positive by reading which directories were matched", and a user cannot open a
      path that does not exist. Measured on the real two-root library: 493 of 493 hits named
      paths that no single root resolved.
    - The same relpath under two roots is *indistinguishable* in `matched`. Two entries,
      identical strings, one directory each. The bare form cannot express the distinction at
      all; this is latent rather than present here (0 of 4,739 albums share a relpath across
      the two roots) but it is a real case -- one release filed in two places is 種別 A, the
      shape this library is full of.

    `matched` is therefore kept exactly as it was: bare relpaths, posix form, sorted. It is
    what equality and the tests here are written against, and changing its contents would
    break both for no gain.

    **`resolved` is the form to show a human and the form `skip_reason` carries.** Each entry
    is `str(roots[root_index] / relpath)` -- exactly the expression `library_scan` and the
    library listing already use, so it resolves, it is unambiguous for the same-relpath case,
    and nothing has to be resolved by hand to read it. Sorted like `matched`, and therefore
    stable across two scans of one unchanged tree.

    Neither field is defaulted. A hit whose `resolved` is empty would put the broken form
    back on the user, and there is exactly one place in this codebase that builds one
    (`find_duplicate`), so making the field required is what stops a future caller from
    quietly constructing the unresolvable one.
    """

    matched: tuple[str, ...]
    resolved: tuple[str, ...]


def find_duplicate(
    scan: LibraryScan,
    *,
    album_name: str,
    track_title: str,
    artist_name: str | None,
    artist_scope: str = "loose",
) -> DuplicateHit | None:
    """Whether this track already exists in any copy of this album. `None` = download it.

    `album_name` / `track_title` / `artist_name` are the values a download would be
    rendered from, i.e. what a *file* in the album would be called and who is credited on
    it -- not what the API reported as the title. The comparison basis is the filename
    (§7.5), so a caller that passes a tag instead of a rendered name will miss, and miss
    quietly, in the safe direction.

    `artist_scope` (§7.4):
      - `"loose"` (default) matches on the album name alone, so it catches both shapes of
        duplicate the real library actually contains: 種別 A, one release filed in two
        places, and 種別 B, a collab fanned out across every credited artist's folder. On
        the external drive all 223 duplicated album names are one of the two -- 164 種別 B
        and 59 種別 A -- which is what makes this the default. Its cost is that two
        genuinely different albums sharing a name would both be treated as one;
        `skip_reason` carries the paths so that is visible.
      - `"strict"` additionally requires the album's artist directory to equal
        `artist_name`. 種別 A has that property and 種別 B does not, so `strict` finds the
        59 and misses all 164: a collab's second and third placements are re-downloaded. A
        deliberate trade, and the reason this is not the default.

    Two refusals, and they are the same rule stated twice: **never skip on a key that
    carries no identifying information.** `normalize` answers `""` for a title or album
    name with no alphanumeric character in it, and `"" == ""`, so a lookup on it would
    match every unusable name in the library at once. Both sides are guarded, because the
    failure is not one-sided: the index really does hold a `""` group (the real library
    contains `ALAC/薄塩指数/!_`), so a punctuation-only *download* name would find it, and
    the real library holds 6 untitled tracks that a `""` *download* title would find
    across the whole library. Returning `None` is always the safe answer here, and a
    re-download is recoverable while a false skip is not.
    """
    if artist_scope not in ARTIST_SCOPES:
        raise ValueError(
            f"artist_scope must be one of {sorted(ARTIST_SCOPES)}, got {artist_scope!r}"
        )

    title_key = normalize(track_title)
    # The album side, guarded the same way and for the same reason. `album_key` is
    # `library_scan`'s own keying function, used here so that building the index and
    # looking it up cannot drift apart: a mismatch is silent and total, since every album
    # lookup would miss and nothing would ever be skipped.
    scope_key = album_key(album_name)
    if not title_key or not scope_key:
        return None

    candidates = scan.by_name.get(scope_key, ())
    if artist_scope == "strict":
        candidates = _by_artist(candidates, artist_name)
    # One pass, so `title_key in c.track_keys` is evaluated once per candidate: the two
    # output forms below have to be built from the *same* set, and a second generator that
    # re-tested the same predicate is a place for the two to drift -- which would be a hit
    # that claims a path it did not match, or omits one it did.
    hits = [c for c in candidates if title_key in c.track_keys]
    if not hits:
        # `None` rather than an empty hit. An empty `matched` used to have to mean "nothing
        # matched"; now the dataclass cannot express one at all, so this is the only way to
        # say it and there is no second spelling of the answer to get wrong.
        return None
    return DuplicateHit(
        matched=tuple(sorted(c.relpath for c in hits)),
        # `roots[root_index] / relpath`, the same expression the library listing uses, so a
        # `skip_reason` and a library row name the same directory by the same route. For a
        # root that is itself an album scope `relpath` is "." and pathlib collapses that, so
        # the entry is the root rather than a trailing "/.".
        resolved=tuple(sorted(str(scan.roots[c.root_index] / c.relpath) for c in hits)),
    )


def _by_artist(
    candidates: tuple, artist_name: str | None
) -> tuple:
    """The candidates whose album directory sits under a directory called `artist_name`.

    Three refusals, and every one of them can only make the match narrower -- each costs a
    re-download, never a false skip, which is the direction `strict` is allowed to fail in:

    - An empty `artist_name`, which is what a resolver that could not read the artist hands
      over.
    - An `artist_name` that `normalize` answers `""` for, i.e. one with no alphanumeric
      character. It would otherwise equal the key of *every* candidate whose artist
      directory is equally unusable. An empty artist directory name is not a match against
      an empty tag; it is a match against every unusable name in the library at once.
    - A candidate whose `artist` is `None` -- the album directory is the root, or sits
      directly in it, which the real library does 10 times. `strict` means the artist has to
      vouch for the match and an unknown artist vouches for nothing. The truthiness test is
      also what keeps `normalize(None)` from raising on that album, so it is load-bearing
      even where it looks decorative.

    Both sides go through the same `normalize()` call, which is the only reason the
    comparison is well defined: one string is a tag value the resolver read and the other
    is a directory basename read off the disk, so they agree only after folding. The real
    library needs that -- 1 of its 349 artist directories (`429 & nyankobrq`) does not
    equal its own `normalize()`, and 8 more are not already casefolded. It is also why the
    track-prefix strip is left on for this side: an artist directory written
    `429 & nyankobrq` must still answer to the tag `& nyankobrq`.
    """
    if not artist_name:
        return ()
    artist_key = normalize(artist_name)
    if not artist_key:
        return ()
    return tuple(c for c in candidates if c.artist and normalize(c.artist) == artist_key)
