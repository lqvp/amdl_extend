#!/usr/bin/env python3
"""Generate the Go Unicode tables the port needs, from *this* Python's unicodedata.

Why generate rather than import `golang.org/x/text`: this sandbox can reach
github.com and pypi.org and nothing else, so `go mod download` cannot fetch a
dependency. Generating from CPython's own tables has the stronger property
anyway -- the Go port folds strings with the *same* Unicode data the Python
implementation folds them with, so `normalize()` agrees character for character
rather than approximately.

Everything here is derived by asking Python, not by trusting a spec:

  * decomposition    unicodedata.decomposition(); the `<tag>` prefix is what
                     distinguishes a compatibility mapping from a canonical one
  * combining class  unicodedata.combining()
  * composition      derived by NFC round-trip, so the composition exclusions
                     (and the non-starter rule) are honoured by construction
  * casefold         str.casefold() per character, which is C+F full folding
  * whitespace       str.isspace(), which is what both `str.strip()` and the
                     `\\s` in a str-mode regex use -- and which is *not* the
                     same set as Go's unicode.IsSpace (U+001C..U+001F are
                     whitespace to Python and not to Unicode)
  * alphanumeric     str.isalnum(), which is what `normalize()` tests a key with

Hangul syllables are excluded from both tables: the algorithm for them is fixed
and is implemented arithmetically in Go, which is also what Python does.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path

MAX = 0x110000
SBASE, LBASE, VBASE, TBASE = 0xAC00, 0x1100, 0x1161, 0x11A7
LCOUNT, VCOUNT, TCOUNT = 19, 21, 28
NCOUNT = VCOUNT * TCOUNT
SCOUNT = LCOUNT * NCOUNT
HANGUL = range(SBASE, SBASE + SCOUNT)

OUT = Path(__file__).resolve().parent.parent / "internal" / "ucd" / "tables.go"


def emit_rune_slice(fh, name: str, values: list[int], per_line: int = 12) -> None:
    fh.write(f"var {name} = [...]rune{{")
    for i, v in enumerate(values):
        if i % per_line == 0:
            fh.write("\n\t")
        fh.write(f"0x{v:x},")
    fh.write("\n}\n\n")


def emit_u32_slice(fh, name: str, values: list[int], per_line: int = 10) -> None:
    fh.write(f"var {name} = [...]uint32{{")
    for i, v in enumerate(values):
        if i % per_line == 0:
            fh.write("\n\t")
        fh.write(f"0x{v:x},")
    fh.write("\n}\n\n")


def emit_u64_slice(fh, name: str, values: list[int], per_line: int = 8) -> None:
    fh.write(f"var {name} = [...]uint64{{")
    for i, v in enumerate(values):
        if i % per_line == 0:
            fh.write("\n\t")
        fh.write(f"0x{v:x},")
    fh.write("\n}\n\n")


def emit_u8_slice(fh, name: str, values: list[int], per_line: int = 16) -> None:
    fh.write(f"var {name} = [...]uint8{{")
    for i, v in enumerate(values):
        if i % per_line == 0:
            fh.write("\n\t")
        fh.write(f"0x{v:x},")
    fh.write("\n}\n\n")


def ranges_of(predicate) -> list[int]:
    """Flat [start, end, start, end, ...] pairs, inclusive, ascending."""
    out: list[int] = []
    start = -1
    for cp in range(MAX):
        ok = predicate(cp)
        if ok and start < 0:
            start = cp
        elif not ok and start >= 0:
            out.extend((start, cp - 1))
            start = -1
    if start >= 0:
        out.extend((start, MAX - 1))
    return out


def main() -> int:
    decomp_keys: list[int] = []
    decomp_offsets: list[int] = [0]
    decomp_vals: list[int] = []
    decomp_compat: list[int] = []  # bitset over entries

    comp_pairs: list[tuple[int, int]] = []
    comp_vals: list[int] = []

    ccc_keys: list[int] = []
    ccc_vals: list[int] = []

    fold_keys: list[int] = []
    fold_offsets: list[int] = [0]
    fold_vals: list[int] = []

    for cp in range(MAX):
        if cp in HANGUL:
            continue
        ch = chr(cp)

        raw = unicodedata.decomposition(ch)
        if raw:
            compat = raw.startswith("<")
            body = raw.split(">", 1)[1].strip() if compat else raw.strip()
            seq = [int(part, 16) for part in body.split()]
            if seq:
                decomp_keys.append(cp)
                decomp_vals.extend(seq)
                decomp_offsets.append(len(decomp_vals))
                decomp_compat.append(1 if compat else 0)
                if not compat and len(seq) == 2:
                    # Round-trip through NFC: this composes back to `cp` only if the
                    # pair is a legal composition and the first element is a starter,
                    # which is exactly the table UAX #15 wants.
                    if unicodedata.normalize("NFC", "".join(map(chr, seq))) == ch:
                        comp_pairs.append((seq[0], seq[1]))
                        comp_vals.append(cp)

        comb = unicodedata.combining(ch)
        if comb:
            ccc_keys.append(cp)
            ccc_vals.append(comb)

        folded = ch.casefold()
        if folded != ch:
            fold_keys.append(cp)
            fold_vals.extend(ord(c) for c in folded)
            fold_offsets.append(len(fold_vals))

    # Paired *before* sorting. Sorting the keys and the values independently --
    # which is what this did first -- keeps the two lists in different orders and
    # silently maps `f` + `i` to the composition of some other pair entirely,
    # which is a wrong answer that looks like a plausible character.
    comp_pairs = sorted(zip(comp_pairs, comp_vals, strict=True))
    comp_keys = [(a << 32) | b for (a, b), _ in comp_pairs]
    comp_vals = [value for _, value in comp_pairs]

    compat_bits: list[int] = [0] * ((len(decomp_compat) + 63) // 64)
    for i, flag in enumerate(decomp_compat):
        if flag:
            compat_bits[i // 64] |= 1 << (i % 64)

    space_ranges = ranges_of(lambda cp: chr(cp).isspace())
    alnum_ranges = ranges_of(lambda cp: chr(cp).isalnum())

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w") as fh:
        fh.write(f'''// Code generated by tools/gen_unicode_tables.py from CPython
// {sys.version.split()[0]}'s unicodedata (Unicode {unicodedata.unidata_version}). DO NOT EDIT.

package ucd

// The version of the Unicode data these tables were derived from. Reported by
// `GET /api/status` so that an operator can see what a deployment folds with.
const UnicodeVersion = "{unicodedata.unidata_version}"

// Decompositions: `decompKeys[i]` decomposes to `decompVals[decompOffsets[i]:decompOffsets[i+1]]`,
// and is a *compatibility* decomposition when bit `i` is set in `decompCompat`.
// Hangul syllables are absent: NFD/NFKD expands them arithmetically.

''')
        emit_u32_slice(fh, "decompKeys", decomp_keys)
        emit_u32_slice(fh, "decompOffsets", decomp_offsets)
        emit_rune_slice(fh, "decompVals", decomp_vals)
        emit_u64_slice(fh, "decompCompat", compat_bits)

        fh.write("// Canonical compositions: `(a, b) -> compVals[i]`, keyed by `a<<32|b`.\n\n")
        emit_u64_slice(fh, "compKeys", comp_keys)
        emit_rune_slice(fh, "compVals", comp_vals)

        fh.write("// Non-zero canonical combining classes, ascending by codepoint.\n\n")
        emit_u32_slice(fh, "cccKeys", ccc_keys)
        emit_u8_slice(fh, "cccVals", ccc_vals)

        fh.write('''// Full case folding (CaseFolding.txt C+F), which is what `str.casefold()` is.
// `foldKeys[i]` folds to `foldVals[foldOffsets[i]:foldOffsets[i+1]]`.


''')
        emit_u32_slice(fh, "foldKeys", fold_keys)
        emit_u32_slice(fh, "foldOffsets", fold_offsets)
        emit_rune_slice(fh, "foldVals", fold_vals)

        fh.write("// Python's `str.isspace()` set, as inclusive [start, end] pairs.\n\n")
        emit_u32_slice(fh, "spaceRanges", space_ranges)

        fh.write("// Python's `str.isalnum()` set, as inclusive [start, end] pairs.\n\n")
        emit_u32_slice(fh, "alnumRanges", alnum_ranges)

    size = OUT.stat().st_size
    print(
        f"wrote {OUT} ({size / 1024:.0f} KiB): "
        f"{len(decomp_keys)} decompositions, {len(comp_keys)} compositions, "
        f"{len(ccc_keys)} combining classes, {len(fold_keys)} case foldings, "
        f"{len(space_ranges) // 2} whitespace ranges, {len(alnum_ranges) // 2} alnum ranges"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
