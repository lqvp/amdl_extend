// Package dedup is a port of `hub/dedup.py`: album-scoped duplicate detection
// for downloads.
//
// The question this package answers is "is this track already on disk?", and the
// answer has to be scoped to the album. Measured on the 341 GB external library,
// `intro`, `escapism`, `mu` and `yoake` each appear in **six** different albums,
// and a normalized title is shared by 2+ album directories for 1,207 of 8,721
// titles -- 13.8%. A global title match would therefore skip real downloads at a
// rate no user would accept. The scoping is not a refinement; it is the only
// reason this is safe.
//
// What enforces it is one equality, not a container: a directory is a candidate
// only when the album **name** it carries equals the album name being downloaded,
// and the title is then tested only against those. `Scan.ByName` is how that
// candidate set is obtained in a single map lookup, not what makes the answer
// right -- filtering `Scan.Albums` by the same name equality would be equally
// correct and only slower. So the invariant worth protecting is the name
// equality, and the tests pin it through the observable answer (a hit names
// directories of one album and never of another) rather than through which
// container the candidates came from.
//
// `FindDuplicate` is **pure**: it reads a `Scan` that has already been taken and
// touches nothing else. No filesystem access, no config, no clock, no network.
// The caller decides when to scan and which `artist_scope` to apply, which is why
// the scope is a parameter and is never read from settings here.
package dedup

import (
	"fmt"
	"sort"
	"strings"

	"amdhub/internal/library"
	"amdhub/internal/normalize"
)

// ScopeLoose and ScopeStrict are the two modes `FindDuplicate` accepts, and the
// only two. A closed set, not a truthy switch: an unrecognised value is a typo in
// a config literal, and silently reading it as `loose` would either skip tracks
// the user asked for (if the typo was meant to be `strict`) or fail to skip
// tracks that are already on disk. Returning an error puts the failure where the
// value came from.
const (
	ScopeLoose  = "loose"
	ScopeStrict = "strict"
)

// Hit is the album directories that already hold this track, in two forms.
//
// **The two forms are not interchangeable, and the difference is not cosmetic.**
// `FindDuplicate` builds its candidates out of `Scan.ByName`, which spans *every*
// configured root. So `Matched` -- bare `AlbumDir.Relpath` values -- says which
// directory a track was found in but not *which library it is in*, and that is
// not a detail:
//
//   - A relpath is not resolvable once there is more than one root. One album
//     name filed under both roots yields `9Lana/x` and `new-dl/9Lana/x`, and
//     neither string exists under both roots. The entire adjudication story is "a
//     user can overrule a `loose` false positive by reading which directories
//     were matched", and a user cannot open a path that does not exist. Measured
//     on the real two-root library: 493 of 493 hits named paths that no single
//     root resolved.
//   - The same relpath under two roots is *indistinguishable* in `Matched`. Two
//     entries, identical strings, one directory each. The bare form cannot
//     express the distinction at all.
//
// `Matched` is therefore kept exactly as it was in Python: bare relpaths, posix
// form, sorted. It is what equality and the ported tests are written against.
// `Resolved` is the form to show a human and the form `skip_reason` carries:
// `JoinPath(Roots[rootIndex], Relpath)`, exactly the expression the library
// listing already uses, so it resolves, it is unambiguous for the same-relpath
// case, and nothing has to be resolved by hand to read it.
//
// Neither field is defaulted, and there is exactly one place in this codebase
// that builds a Hit, so a future caller cannot quietly construct the
// unresolvable form.
type Hit struct {
	Matched  []string
	Resolved []string
}

// FindDuplicate reports whether this track already exists in any copy of this
// album. A nil Hit means "download it".
//
// `albumName`, `trackTitle` and `artistName` are the values a download would be
// rendered from, i.e. what a *file* in the album would be called and who is
// credited on it -- not what the API reported as the title. The comparison basis
// is the filename, so a caller that passes a tag instead of a rendered name will
// miss, and miss quietly, in the safe direction.
//
// `artistScope` is "loose" or "strict":
//
//   - `loose` matches on the album name alone, so it catches both shapes of
//     duplicate the real library actually contains: one release filed in two
//     places, and a collab fanned out across every credited artist's folder. On
//     the external drive all 223 duplicated album names are one of the two --
//     164 of the second, 59 of the first -- which is what makes this the default.
//     Its cost is that two genuinely different albums sharing a name would both
//     be treated as one; `skip_reason` carries the paths so that is visible.
//   - `strict` additionally requires the album's artist directory to equal
//     `artistName`. The first shape has that property and the second does not, so
//     `strict` finds the 59 and misses all 164: a collab's second and third
//     placements are re-downloaded. A deliberate trade, and the reason this is
//     not the default.
//
// Two refusals, and they are the same rule stated twice: **never skip on a key
// that carries no identifying information.** `normalize` answers "" for a title
// or album name with no alphanumeric character in it, and "" == "", so a lookup
// on it would match every unusable name in the library at once. Both sides are
// guarded, because the failure is not one-sided: the index really does hold a ""
// group (the real library contains `ALAC/薄塩指数/!_`), so a punctuation-only
// *download* name would find it, and the real library holds 6 untitled tracks
// that a "" *download* title would find across the whole library. Returning nil
// is always the safe answer here, and a re-download is recoverable while a false
// skip is not.
func FindDuplicate(
	scan *library.Scan,
	albumName string,
	trackTitle string,
	artistName string,
	artistScope string,
) (*Hit, error) {
	if artistScope != ScopeLoose && artistScope != ScopeStrict {
		return nil, fmt.Errorf("artist_scope must be one of [loose strict], got %q", artistScope)
	}

	titleKey := normalize.Normalize(trackTitle, true)
	// The album side, guarded the same way and for the same reason. `AlbumKey`
	// is `library`'s own keying function, used here so that building the index
	// and looking it up cannot drift apart: a mismatch is silent and total, since
	// every album lookup would miss and nothing would ever be skipped.
	scopeKey := normalize.AlbumKey(albumName)
	if titleKey == "" || scopeKey == "" {
		return nil, nil
	}

	candidates := scan.ByName[scopeKey]
	if artistScope == ScopeStrict {
		candidates = byArtist(candidates, artistName)
	}
	// One pass, exactly as in Python, so the two output forms are built from the
	// *same* set: a second loop re-testing the predicate is a place for the two
	// to drift, which would be a hit that claims a path it did not match, or
	// omits one it did.
	var hits []*library.AlbumDir
	for _, candidate := range candidates {
		if _, ok := candidate.TrackKeys[titleKey]; ok {
			hits = append(hits, candidate)
		}
	}
	if len(hits) == 0 {
		// nil rather than an empty Hit: an empty `Matched` used to have to mean
		// "nothing matched", and this is the only way to say it.
		return nil, nil
	}
	matched := make([]string, 0, len(hits))
	resolved := make([]string, 0, len(hits))
	for _, hit := range hits {
		matched = append(matched, hit.Relpath)
		resolved = append(resolved, hit.Resolved(scan.Roots))
	}
	sort.Strings(matched)
	sort.Strings(resolved)
	return &Hit{Matched: matched, Resolved: resolved}, nil
}

// byArtist keeps the candidates whose album directory sits under a directory
// called `artistName`.
//
// Three refusals, and every one of them can only make the match narrower -- each
// costs a re-download, never a false skip, which is the direction `strict` is
// allowed to fail in:
//
//   - An empty `artistName`, which is what a resolver that could not read the
//     artist hands over.
//   - An `artistName` that `normalize` answers "" for, i.e. one with no
//     alphanumeric character. It would otherwise equal the key of *every*
//     candidate whose artist directory is equally unusable. An empty artist
//     directory name is not a match against an empty tag; it is a match against
//     every unusable name in the library at once.
//   - A candidate whose `Artist` is "" -- the album directory is the root, or
//     sits directly in it, which the real library does 10 times. `strict` means
//     the artist has to vouch for the match and an unknown artist vouches for
//     nothing.
//
// Both sides go through the same `Normalize` call, which is the only reason the
// comparison is well defined: one string is a tag value the resolver read and the
// other is a directory basename read off the disk, so they agree only after
// folding. The real library needs that -- 1 of its 349 artist directories
// (`429 & nyankobrq`) does not equal its own `normalize()`, and 8 more are not
// already casefolded. It is also why the track-prefix strip is left on for this
// side: an artist directory written `429 & nyankobrq` must still answer to the
// tag `& nyankobrq`.
func byArtist(candidates []*library.AlbumDir, artistName string) []*library.AlbumDir {
	if strings.TrimSpace(artistName) == "" {
		return nil
	}
	artistKey := normalize.Normalize(artistName, true)
	if artistKey == "" {
		return nil
	}
	var out []*library.AlbumDir
	for _, candidate := range candidates {
		if candidate.Artist == "" {
			continue
		}
		if normalize.Normalize(candidate.Artist, true) == artistKey {
			out = append(out, candidate)
		}
	}
	return out
}
