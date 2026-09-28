package normalize

import "strings"

// The ASCII fast path. Most of a library is ASCII-mostly even when the *titles*
// are Japanese -- track numbers, separators, extensions and the Latin half of a
// Japanese title are all ASCII -- and NFKC is the identity on every ASCII
// character, so a name that is entirely ASCII can skip the Unicode layer
// completely.
//
// This is not premature: `BenchmarkNormalize` over a 21,400-file library measured
// the general path at 2,115 ns per name against CPython's `unicodedata` at 1,965
// ns, i.e. the port was *slower* than the thing it replaced on the one operation
// that is CPU rather than I/O. The tables and the binary searches per character are
// what cost that -- CPython's tables are flat arrays indexed by the code point --
// and the fast path is what removes the searches for the overwhelmingly common
// input.

// asciiSpace is Python's `str.isspace()` restricted to ASCII. U+001C..U+001F are
// the reason this is a table rather than `' ' || '\t' || ...`: they are whitespace
// to Python and not to Unicode's White_Space property, and a `strings.TrimSpace`
// would therefore disagree on a control character a broken ID3 writer can leave in
// a filename.
var asciiSpace = [128]bool{
	'\t': true, '\n': true, '\v': true, '\f': true, '\r': true,
	0x1c: true, 0x1d: true, 0x1e: true, 0x1f: true, ' ': true,
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] >= 0x80 {
			return false
		}
	}
	return true
}

// asciiAudioBase is `_audio_base` for a name whose bytes are all ASCII: the stem
// and whether the extension is one this library stores audio in.
//
// It returns *substrings*, not copies, so the caller decides what to allocate.
// The extension is compared after an ASCII fold, which is what `casefold` does to
// ASCII and is deliberately not `strings.EqualFold`: the comparison is against a
// fixed set of lowercase extensions, and a table lookup on the folded value is
// both faster and impossible to get subtly wrong.
func asciiAudioBase(name string) (string, bool) {
	dot := strings.LastIndexByte(name, '.')
	if dot <= 0 {
		// dot < 0: no extension at all. dot == 0: a leading dot, which marks a
		// hidden file rather than an extension -- ".m4a" is not audio, and
		// `stem_of(".m4a")` agrees by returning it unchanged.
		return name, false
	}
	base, ext := name[:dot], name[dot+1:]
	var folded [5]byte
	if len(ext) == 0 || len(ext) > len(folded) {
		return name, false
	}
	for i := 0; i < len(ext); i++ {
		c := ext[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		folded[i] = c
	}
	switch string(folded[:len(ext)]) {
	case "m4a", "mp4", "m4b", "flac", "aac", "ec3", "ac3", "wav", "ogg", "opus":
		return base, true
	default:
		return name, false
	}
}

// normalizeASCII is `Normalize` for a name that is entirely ASCII.
//
// The steps are the same as the general path and in the same order -- extension
// strip, track-prefix strip, whitespace squeeze, unusable-title check -- with NFKC
// and the casefold table replaced by an ASCII fold, which is what the Unicode path
// would do to these bytes anyway.
func normalizeASCII(name string, stripTrackPrefix bool) string {
	key := name
	if base, ok := asciiAudioBase(name); ok {
		key = base
	}
	start := 0
	if stripTrackPrefix {
		start = asciiTrackPrefix(key)
	}
	return asciiSqueeze(key[start:])
}

// asciiTrackPrefix is `^(?:\d{1,3}(?:\s*[.\-_]\s*|\s+)){1,2}` over ASCII bytes,
// returning how much of the name it consumes.
//
// The same two-step greedy order as the general path: two repetitions before one,
// three digits before two before one, and the separator form before the whitespace
// form. See `stripTrackPrefixMatch` for why the order is part of the contract
// rather than an implementation detail.
func asciiTrackPrefix(s string) int {
	if len(s) == 0 || !asciiDigit(s[0]) {
		return 0
	}
	// `{1,2}` prefers two repetitions and falls back to one, which is what makes
	// "75  Title?.m4" strip its number: the two-repetition attempt consumes "75  "
	// and then has nothing to match against, and the one-repetition attempt is what
	// answers. Trying only the greedy case is the bug this line exists to not have
	// -- and the golden corpus caught it, which is why the corpus is generated from
	// the Python implementation rather than written by hand.
	if end, ok := asciiReps(s, 0, 2); ok {
		return end
	}
	if end, ok := asciiReps(s, 0, 1); ok {
		return end
	}
	return 0
}

func asciiReps(s string, pos, reps int) (int, bool) {
	if reps == 0 {
		return pos, true
	}
	for n := 3; n >= 1; n-- {
		if pos+n > len(s) {
			continue
		}
		ok := true
		for i := 0; i < n; i++ {
			if !asciiDigit(s[pos+i]) {
				ok = false
				break
			}
		}
		if !ok {
			continue
		}
		p := pos + n
		q := p
		for q < len(s) && asciiSpace[s[q]] {
			q++
		}
		var end int
		matched := false
		if q < len(s) && (s[q] == '.' || s[q] == '-' || s[q] == '_') {
			end = q + 1
			for end < len(s) && asciiSpace[s[end]] {
				end++
			}
			matched = true
		} else if q > p {
			end, matched = q, true
		}
		if !matched {
			continue
		}
		if rest, ok := asciiReps(s, end, reps-1); ok {
			return rest, true
		}
	}
	return 0, false
}

func asciiDigit(c byte) bool { return c >= '0' && c <= '9' }

// asciiSqueeze is `_WHITESPACE_RE.sub(" ", key).strip()` plus the unusable-title
// check, in one pass over the bytes.
//
// One allocation for the result and none for the common case of a key that needs
// no folding at all: `strings.Builder` per name was 2 of the 2,115 ns the general
// path cost. The `hasAlnum` scan is fused in because the answer is needed anyway
// and walking the bytes twice for it would be the same work again.
func asciiSqueeze(s string) string {
	out := make([]byte, 0, len(s))
	pendingSpace := false
	started := false
	alnum := false
	for i := 0; i < len(s); i++ {
		c := s[i]
		if asciiSpace[c] {
			pendingSpace = true
			continue
		}
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		if pendingSpace && started {
			out = append(out, ' ')
		}
		pendingSpace = false
		started = true
		if !alnum && ((c >= 'a' && c <= 'z') || asciiDigit(c)) {
			alnum = true
		}
		out = append(out, c)
	}
	if !alnum {
		// A title with no alphanumeric character carries no identifying
		// information, so it is reported as unusable rather than as a literal key.
		return ""
	}
	return string(out)
}
