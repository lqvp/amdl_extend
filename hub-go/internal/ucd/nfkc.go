// Package ucd is the Unicode layer the port needs, and it is generated rather
// than depended on.
//
// `golang.org/x/text/unicode/norm` is the obvious import for NFKC and it is
// unavailable: this sandbox reaches github.com and pypi.org and nothing else,
// so `go mod download` cannot fetch anything. `tools/gen_unicode_tables.py`
// emits the tables in `tables.go` from CPython's own `unicodedata` instead.
//
// That turns out to be the better artefact for a port. `normalize()` in
// `hub/normalize.py` folds strings with CPython's Unicode data, and a port that
// uses a differently-versioned table would disagree with the thing it is
// replacing on exactly the characters nobody tests by hand. Deriving the Go
// tables from the same source makes the two agree character for character, and
// `TestNormalizeMatchesPython` pins that against a generated corpus.
//
// What is implemented here is the three transforms `normalize()` actually
// composes: NFKC, full case folding, and the two predicates (`str.isspace` and
// `str.isalnum`) whose Python definitions are *not* Go's `unicode.IsSpace` and
// `unicode.IsLetter` -- U+001C..U+001F are whitespace to Python and not to
// Unicode's White_Space property, and Python's `isalpha` includes the
// Other_Alphabetic combining marks that Go's `unicode.IsLetter` does not.
package ucd

import (
	"sort"
	"strings"
)

// Hangul, algorithmically. UAX #15 defines the syllable block arithmetically
// rather than by table, and the generated tables therefore omit it.
const (
	hangulSBase  = 0xAC00
	hangulLBase  = 0x1100
	hangulVBase  = 0x1161
	hangulTBase  = 0x11A7
	hangulLCount = 19
	hangulVCount = 21
	hangulTCount = 28
	hangulNCount = hangulVCount * hangulTCount // 588
	hangulSCount = hangulLCount * hangulNCount // 11172
)

// NFKC returns s normalized to Normalization Form KC -- the same transform as
// `unicodedata.normalize("NFKC", s)`.
//
// NFKC rather than NFC is deliberate in the caller and is not this function's
// business, but the reason is worth having next to the implementation: a
// Japanese library routinely holds both "ＡＢＣ Title" and "ABC Title" for what
// is one track, and the question `normalize()` answers is "are these the same
// title", not "is this string in its canonical form".
func NFKC(s string) string {
	if isASCII(s) {
		return s
	}
	buf := make([]rune, 0, len(s)+8)
	for _, r := range s {
		buf = appendDecomposed(buf, r)
	}
	canonicalOrder(buf)
	buf = composeInPlace(buf)
	return string(buf)
}

// isASCII is the fast path: NFKC is the identity on ASCII, and this is called
// once per filename and once per album directory on every scan.
func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

// appendDecomposed appends the full (canonical or compatibility) decomposition
// of r.
//
// Recursive, because the mappings in UnicodeData.txt are one level deep: U+01D5
// decomposes to U+00DC U+0304, and U+00DC decomposes again to U+0055 U+0308. A
// non-recursive expansion would leave the second level in place and NFKC would
// be wrong for exactly the precomposed Latin a library is most likely to hold.
func appendDecomposed(dst []rune, r rune) []rune {
	if s := r - hangulSBase; s >= 0 && s < hangulSCount {
		l := hangulLBase + s/hangulNCount
		v := hangulVBase + (s%hangulNCount)/hangulTCount
		t := hangulTBase + s%hangulTCount
		dst = append(dst, l, v)
		if t != hangulTBase {
			dst = append(dst, t)
		}
		return dst
	}
	i := sort.Search(len(decompKeys), func(i int) bool { return rune(decompKeys[i]) >= r })
	if i < len(decompKeys) && rune(decompKeys[i]) == r {
		for _, part := range decompVals[decompOffsets[i]:decompOffsets[i+1]] {
			dst = appendDecomposed(dst, part)
		}
		return dst
	}
	return append(dst, r)
}

// combiningClass is 0 for a starter, which is the value that decides whether a
// character can be reordered or composed in the first place.
func combiningClass(r rune) uint8 {
	i := sort.Search(len(cccKeys), func(i int) bool { return rune(cccKeys[i]) >= r })
	if i < len(cccKeys) && rune(cccKeys[i]) == r {
		return cccVals[i]
	}
	return 0
}

// canonicalOrder sorts each run of non-starters by combining class.
//
// Insertion sort, which is what UAX #15's "canonical ordering algorithm"
// describes: the sequences are short (a base and a couple of marks) and the sort
// has to be *stable* within one combining class, which insertion sort is and
// `sort.Slice` is not.
func canonicalOrder(buf []rune) {
	for i := 1; i < len(buf); i++ {
		cc := combiningClass(buf[i])
		if cc == 0 {
			continue
		}
		j := i
		for j > 0 {
			prev := combiningClass(buf[j-1])
			if prev == 0 || prev <= cc {
				break
			}
			buf[j], buf[j-1] = buf[j-1], buf[j]
			j--
		}
	}
}

// composePair is the canonical composition of a starter and a following
// character, Hangul included.
func composePair(a, b rune) (rune, bool) {
	if l := a - hangulLBase; l >= 0 && l < hangulLCount {
		if v := b - hangulVBase; v >= 0 && v < hangulVCount {
			return hangulSBase + (l*hangulVCount+v)*hangulTCount, true
		}
	}
	if s := a - hangulSBase; s >= 0 && s < hangulSCount && s%hangulTCount == 0 {
		if t := b - hangulTBase; t > 0 && t < hangulTCount {
			return a + t, true
		}
	}
	key := uint64(a)<<32 | uint64(uint32(b))
	i := sort.Search(len(compKeys), func(i int) bool { return compKeys[i] >= key })
	if i < len(compKeys) && compKeys[i] == key {
		return compVals[i], true
	}
	return 0, false
}

// composeInPlace applies the canonical composition algorithm, returning the
// (possibly shortened) slice.
//
// The structure is ICU's, which is the reference implementation of UAX #15's:
// walk the buffer, try to compose each character with the last starter, and
// treat the character as blocked when the character in between it and the
// starter has an equal or greater combining class. `prevCC == 0` is the other
// half of "not blocked" -- it means the previous character was itself a starter
// that failed to compose, so it is now the one being composed against.
func composeInPlace(buf []rune) []rune {
	if len(buf) == 0 {
		return buf
	}
	starterPos := 0
	starter := buf[0]
	out := 1
	var prevCC uint8
	for i := 1; i < len(buf); i++ {
		ch := buf[i]
		cc := combiningClass(ch)
		if prevCC < cc || prevCC == 0 {
			if composed, ok := composePair(starter, ch); ok {
				buf[starterPos] = composed
				starter = composed
				continue
			}
		}
		if cc == 0 {
			starterPos = out
			starter = ch
		}
		buf[out] = ch
		out++
		prevCC = cc
	}
	return buf[:out]
}

// CaseFold returns s folded with full case folding (CaseFolding.txt C+F), which
// is what Python's `str.casefold()` is -- and *not* what `strings.ToLower` is.
// The difference is not academic here: the port's whole job is to agree with
// `normalize()`, and "ß".casefold() is "ss" while strings.ToLower("ß") is "ß".
func CaseFold(s string) string {
	if isASCII(s) {
		return strings.Map(func(r rune) rune {
			if r >= 'A' && r <= 'Z' {
				return r + 32
			}
			return r
		}, s)
	}
	var sb strings.Builder
	sb.Grow(len(s))
	for _, r := range s {
		i := sort.Search(len(foldKeys), func(i int) bool { return rune(foldKeys[i]) >= r })
		if i < len(foldKeys) && rune(foldKeys[i]) == r {
			for _, f := range foldVals[foldOffsets[i]:foldOffsets[i+1]] {
				sb.WriteRune(f)
			}
			continue
		}
		sb.WriteRune(r)
	}
	return sb.String()
}

func inRanges(ranges []uint32, r rune) bool {
	if r < 0 {
		return false
	}
	cp := uint32(r)
	i := sort.Search(len(ranges)/2, func(i int) bool { return ranges[2*i+1] >= cp })
	return i < len(ranges)/2 && ranges[2*i] <= cp
}

// IsSpace reports whether r is whitespace *to Python* -- `str.isspace()`, which
// is also what `str.strip()` strips and what `\s` matches in a str-mode regex.
// Unicode's White_Space property is a slightly smaller set (it excludes
// U+001C..U+001F), so `unicode.IsSpace` is not a substitute.
func IsSpace(r rune) bool { return inRanges(spaceRanges[:], r) }

// IsAlnum reports whether r is alphanumeric to Python -- `str.isalnum()`, which
// is `str.isalpha() or str.isdecimal() or str.isdigit() or str.isnumeric()`.
// `normalize()` returns "" for a key with no alphanumeric character in it, and
// that empty key is what a caller must refuse to match on.
func IsAlnum(r rune) bool { return inRanges(alnumRanges[:], r) }
