// Package normalize is a port of `hub/normalize.py`: filename folding for dedup
// matching.
//
// The filesystem is the only source of truth: a download is skipped when the
// track's rendered filename matches a file that is already on disk. `Normalize`
// is the single transform applied to both sides of that comparison, which is why
// it has to be identical on both sides and why it stays a pure function -- no
// config, no environment, no filesystem.
//
// **The port has to agree with the Python implementation character for
// character**, because the two are the same library's dedup index at different
// moments: a Go hub that folds a name differently from the Python hub it
// replaced would re-download (or, worse, falsely skip) files the other one had
// already indexed. Every step below therefore names the CPython call it is
// mirroring, and `internal/ucd` is generated from that same CPython.
package normalize

import (
	"strings"
	"unicode"
	"unicode/utf8"

	"amdhub/internal/ucd"
)

// AudioExts is the set of file extensions this library stores audio in, stored
// with the dot and all-lowercase so that membership can be tested against a
// casefolded extension -- see `isAudioBase`, which is the only place that may
// test it.
//
// Deliberately excludes ".jpg" (a cover-art-only directory must not count as an
// album), ".lrc", and ".part": the real library holds 160 of the latter from
// interrupted downloads, and counting them as existing tracks would wrongly skip
// a re-request.
var AudioExts = map[string]struct{}{
	".m4a": {}, ".mp4": {}, ".m4b": {}, ".flac": {}, ".aac": {},
	".ec3": {}, ".ac3": {}, ".wav": {}, ".ogg": {}, ".opus": {},
}

// isAudioBase splits the name the way `_audio_base` does and returns the stem
// and whether the extension is one this library stores audio in.
//
// `pathlib` must not be used here, and neither may `filepath`/`strings.LastIndex`
// on a *path*: NFKC folds the fullwidth solidus "／" (U+FF0F) to "/", so a path
// parser reads "01. A／B.m4a" as a directory separator and keeps only the last
// component, collapsing it to "b" -- which collides with "01. B.m4a". "／" is
// ordinary Japanese typography for a two-part title, so treating "/" as
// structural manufactures a false skip. `rpartition` looks only at the final dot
// and leaves every other character alone.
//
// A leading dot is not an extension: ".m4a" has no base, so it is returned
// as-is rather than reduced to the empty string.
func isAudioBase(filename string) (string, bool) {
	dot := strings.LastIndex(filename, ".")
	if dot < 0 {
		return filename, false
	}
	base, ext := filename[:dot], filename[dot:]
	if base == "" {
		return filename, false
	}
	if _, ok := AudioExts[ucd.CaseFold(ext)]; !ok {
		// `CaseFold` rather than `strings.ToLower`, and the difference is not
		// theoretical: the ligature "ﬂ" (U+FB02) casefolds to "fl", so
		// "Song.ﬂac" *is* an audio file to CPython while `ToLower` leaves it
		// alone. The port has to agree about that, because the two are the same
		// library's dedup index at different moments.
		return filename, false
	}
	return base, true
}

// IsAudioFile reports whether a *filename* is one this library stores audio in.
//
// Takes a filename rather than a bare extension, and that is deliberate rather
// than a quirk: `IsAudioFile(".m4a")` is false, because a leading dot marks a
// hidden file rather than an extension. Callers walking a directory have
// filenames, so the ambiguous input is answered conservatively -- and `StemOf`
// agrees, returning ".m4a" unchanged.
func IsAudioFile(name string) bool {
	_, ok := isAudioBase(name)
	return ok
}

// StemOf drops the extension, but only when it is one this library actually
// stores audio in: "1-01 Caribbean Blue.m4a" -> "1-01 Caribbean Blue".
//
// The membership test is the whole point. A "strip everything after the last
// dot" implementation cannot be used: it treats the final ".<anything>" as an
// extension, so the default playlist format "01. Artist - Title" collapses to
// "01" and a library downloaded from playlists keys every track to a bare index.
func StemOf(filename string) string {
	base, ok := isAudioBase(filename)
	if !ok {
		return filename
	}
	return base
}

// Normalize returns the comparison key for a track filename or an album
// directory name.
//
// Order is load-bearing: **all string folding first, then structural removal.**
// Stripping first misses structure written in fullwidth -- "Song．Ｍ４Ａ" would
// keep ".m4a" inside the key, and the fullwidth track number in
// "０１．Artist - Title" would never be recognised. So: NFKC, casefold,
// extension strip, track-prefix strip, whitespace squeeze, unusable-title check.
//
// stripTrackPrefix exists because the two sides need different treatment. Track
// files are rendered with their number ("1-01 Title.m4a"), so the number is noise
// on both sides and stripping it is what makes a re-download match its own
// existing file. Album *directories* must keep it: "4pi" and "1st EP" are real
// album names. So the index is built with the flag off and every lookup uses the
// flag off; a mismatch makes every album lookup miss and nothing is ever skipped.
//
// Returns "" for an unusable title -- empty, whitespace, or punctuation only,
// which the real library contains 6 of. "" equals every other "", so a caller
// must treat an empty key as "cannot decide" and refuse to skip on it; that
// guard lives at the dedup call site, not here.
func Normalize(name string, stripTrackPrefix bool) string {
	// The ASCII fast path. NFKC is the identity on ASCII, the casefold table has
	// no ASCII entry, and Python's whitespace and alnum sets restricted to ASCII
	// are three small tables -- so an all-ASCII name can be folded over bytes with
	// no table searches at all, which is what `ascii.go` does. It is the common
	// case even in a Japanese library: track numbers, separators, extensions and
	// the Latin half of a title are all ASCII.
	if isASCII(name) {
		return normalizeASCII(name, stripTrackPrefix)
	}
	// NFKC, not NFC. Both reconcile composed and decomposed forms, which is what
	// stops an NTFS-written track from failing to match itself. NFKC additionally
	// folds ideographic width, which matters here: a Japanese library routinely
	// holds both "ＡＢＣ Title" and "ABC Title" for what is one track.
	key := ucd.NFKC(name)
	// casefold, so the suffix reaching `StemOf` is lowercase and the `AudioExts`
	// membership test is case-insensitive without further folding.
	key = ucd.CaseFold(key)
	key = StemOf(key)
	if stripTrackPrefix {
		key = stripTrackPrefixMatch(key)
	}
	key = squeezeWhitespace(key)
	if !hasAlnum(key) {
		return ""
	}
	return key
}

// AlbumKey is the `by_name` key for an album, and the key a lookup must use.
//
// `stripTrackPrefix=false` is the whole point: album names keep their leading
// digits, because "4pi" and "1st EP" are real albums. Callers get this function
// rather than `Normalize` so that building the index and looking it up cannot
// drift apart -- if they did, every lookup would miss and nothing would ever be
// skipped, with no error anywhere.
func AlbumKey(name string) string { return Normalize(name, false) }

// digit is Python's `\d` in a str-mode pattern, which is the Nd category and not
// ASCII. NFKC folds the fullwidth digits to ASCII, so "０１" has already become
// "01" by the time this runs -- but Arabic-Indic "٣", Tamil "௦" and Ol Chiki
// "᱐" are Nd too and survive folding, and a transliteration with `[0-9]` would
// silently stop stripping a track number written with them. Go's `unicode.IsDigit`
// is exactly Nd, which is also exactly Python's `str.isdecimal()`.
func digit(r rune) bool { return unicode.IsDigit(r) }

func separator(r rune) bool { return r == '.' || r == '-' || r == '_' }

// stripTrackPrefixMatch removes `^(?:\d{1,3}(?:\s*[.\-_]\s*|\s+)){1,2}`.
//
// Hand-written rather than a `regexp.MustCompile` for two reasons, and the first
// one is the reason the pattern is not transliterated: `\s` in a *str*-mode
// Python regex is `str.isspace()`, and Go's `\s` is `[ \t\n\f\r]`. A transliterated
// pattern would therefore disagree with Python on U+00A0, U+3000 and a dozen
// others -- inside filenames a Japanese library is full of.
//
// The second is the backtracking. `{1,2}` prefers two repetitions, each
// repetition prefers three digits and then the separator form over the
// whitespace form, and the three choices are tried in that order -- which is what
// makes "01. Artist - Title" strip to "Artist - Title" rather than to
// "Artist - Title" by luck. The port reproduces the order rather than the
// shape: `matchPrefix` recurses over the repetition count and `matchRep`
// returns each alternative in the order a backtracking engine would.
//
// `\d{1,3}` deliberately does not match a 4-digit year, so "1979 - Song" keeps
// its number and matches the rendered "01-1979 - Song" rather than collapsing
// onto an unrelated "Song".
func stripTrackPrefixMatch(s string) string {
	if s == "" {
		return s
	}
	// The common case by a wide margin: a key that cannot start a repetition at
	// all. Decoded rather than indexed, because `s[0]` is a byte and the digits
	// this accepts include three-byte ones.
	first, _ := utf8.DecodeRuneInString(s)
	if first < utf8.RuneSelf && !digit(first) {
		return s
	}
	rs := []rune(s)
	if end, ok := matchPrefix(rs, 0, 2); ok {
		return strings.TrimLeftFunc(string(rs[end:]), ucd.IsSpace)
	}
	return s
}

// matchPrefix tries to consume `reps` repetitions at `pos`, preferring the
// largest repetition count, exactly as a greedy `{1,2}` does.
func matchPrefix(rs []rune, pos, reps int) (int, bool) {
	for n := reps; n >= 1; n-- {
		if end, ok := matchReps(rs, pos, n); ok {
			return end, true
		}
	}
	return 0, false
}

func matchReps(rs []rune, pos, n int) (int, bool) {
	if n == 0 {
		return pos, true
	}
	for _, end := range repEnds(rs, pos) {
		if rest, ok := matchReps(rs, end, n-1); ok {
			return rest, true
		}
	}
	return 0, false
}

// repEnds lists the ends of one repetition in the order the engine tries them:
// three digits before two before one, and `\s*[.\-_]\s*` before `\s+`.
func repEnds(rs []rune, pos int) []int {
	var ends []int
	for n := 3; n >= 1; n-- {
		if pos+n > len(rs) {
			continue
		}
		ok := true
		for i := 0; i < n; i++ {
			if !digit(rs[pos+i]) {
				ok = false
				break
			}
		}
		if !ok {
			continue
		}
		p := pos + n
		q := p
		for q < len(rs) && ucd.IsSpace(rs[q]) {
			q++
		}
		if q < len(rs) && separator(rs[q]) {
			r := q + 1
			for r < len(rs) && ucd.IsSpace(rs[r]) {
				r++
			}
			ends = append(ends, r)
			continue
		}
		if q > p {
			ends = append(ends, q)
		}
	}
	return ends
}

// squeezeWhitespace is `_WHITESPACE_RE.sub(" ", key).strip()`: every run of
// Python-whitespace becomes one space, and the ends are trimmed.
func squeezeWhitespace(s string) string {
	var sb strings.Builder
	sb.Grow(len(s))
	space := false
	started := false
	for _, r := range s {
		if ucd.IsSpace(r) {
			space = true
			continue
		}
		if space && started {
			sb.WriteByte(' ')
		}
		space = false
		started = true
		sb.WriteRune(r)
	}
	return sb.String()
}

// hasAlnum is Python's `any(char.isalnum() for char in key)`.
//
// A title with no alphanumeric character carries no identifying information, so
// it is reported as unusable rather than as a literal key. Returning "..." for
// it would make every dot-only track in an album match every other one, which is
// the same failure mode as an empty title but harder to notice.
func hasAlnum(s string) bool {
	for _, r := range s {
		if ucd.IsAlnum(r) {
			return true
		}
	}
	return false
}
