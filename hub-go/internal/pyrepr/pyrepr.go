// Package pyrepr renders values the way Python's `repr()` does.
//
// The port keeps the Python error messages *verbatim* -- they are the text a
// deployment's failure modes are documented in, and `TestLoadMatchesPython`
// compares them -- and those messages are written with `!r` substitutions. Go's
// `%q` is not a substitute: it quotes with `"` unconditionally, escapes non-ASCII
// runes as byte escapes, and would make "AMD_RIP_CONCURRENCY must be a whole
// number, got '四'" read
// "AMD_RIP_CONCURRENCY must be a whole number, got \"\\u56db\"" instead.
package pyrepr

import (
	"fmt"
	"sort"
	"strings"
)

// Str is Python's `repr()` for a str.
//
// Single quotes, unless the value contains a single quote and no double quote --
// which is exactly CPython's rule, and the reason a path with an apostrophe in it
// prints differently in the two spellings.
func Str(s string) string {
	quote := byte('\'')
	if strings.ContainsRune(s, '\'') && !strings.ContainsRune(s, '"') {
		quote = '"'
	}
	var sb strings.Builder
	sb.WriteByte(quote)
	for _, r := range s {
		switch r {
		case '\\':
			sb.WriteString(`\\`)
		case '\n':
			sb.WriteString(`\n`)
		case '\r':
			sb.WriteString(`\r`)
		case '\t':
			sb.WriteString(`\t`)
		case rune(quote):
			sb.WriteByte('\\')
			sb.WriteRune(r)
		default:
			if r < 0x20 || r == 0x7f {
				fmt.Fprintf(&sb, `\x%02x`, r)
				continue
			}
			// A printable non-ASCII character is kept as itself, which is what
			// `repr` does in a Python 3 interpreter with a UTF-8 locale.
			sb.WriteRune(r)
		}
	}
	sb.WriteByte(quote)
	return sb.String()
}

// StrList is Python's `repr()` for a list of str, sorted the way the messages
// that use it sort: `sorted(TERMINAL_STATUSES)` is `['cancelled', 'done', ...]`.
//
// Sorted here rather than at the call site so that the two cannot disagree --
// a message that lists the statuses in a different order is a message that no
// longer matches what the Python hub prints.
func StrList(values []string) string {
	sorted := append([]string(nil), values...)
	sort.Strings(sorted)
	parts := make([]string, len(sorted))
	for i, value := range sorted {
		parts[i] = Str(value)
	}
	return "[" + strings.Join(parts, ", ") + "]"
}

// Int is `repr()` for an int, which is just the decimal form -- here so that a
// message assembled from several substitutions does not mix quoting styles by
// accident.
func Int(value int64) string { return fmt.Sprintf("%d", value) }
