#!/usr/bin/env bash
# Run the Go port and the Python original over one library and print them side by
# side.
#
# The tree is generated outside the repository (`tools/gen_bench_library.py`) and
# is 24,000 empty files: the question is how long a walk of this *shape* takes,
# and the shape is what the two implementations disagree about, not the contents.
#
# Both sides run the same three operations against the same directories:
#   scan        `scan_roots` / `library.ScanRoots` -- the whole walk plus the fold,
#               which is what a request costs, because the design re-walks every
#               time rather than caching.
#   normalize   the filename fold on its own, which is the part that is CPU rather
#               than I/O and the part the port had to reimplement.
#   dedup       one album-scoped lookup, with the scan taken once outside the timer.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE="$(dirname "$HERE")"
ROOT="${AMDHUB_BENCH_LIBRARY:-/tmp/amdhub-bench/library}"
PYTHON="${AMDHUB_BENCH_PYTHON:-/home/user/toolchain/pyenv/bin/python}"
GO="${GO:-/home/user/toolchain/current/bin/go}"
REPEATS="${AMDHUB_BENCH_REPEATS:-7}"

export CGO_ENABLED=1
export GOFLAGS=-mod=mod
export GOPROXY=off
export GOTOOLCHAIN=local
export AMDHUB_BENCH_LIBRARY="$ROOT"

if [ ! -d "$ROOT" ]; then
	echo "building the synthetic library at $ROOT" >&2
	python3 "$HERE/gen_bench_library.py" --root "$ROOT" >&2
fi

echo "== Go ==" >&2
# One invocation per benchmark, because they want very different sample counts:
# a scan is ~50 ms and three of them is already a stable median, while a fold is
# ~600 ns and seven of them measures the scheduler.
GO_OUT=""
for spec in "BenchmarkScanRoots 5" "BenchmarkNormalize 20000" "BenchmarkFindDuplicate 5000"; do
	set -- $spec
	part="$("$GO" test -bench "$1" -run XXX -benchtime "$2x" -count 1 "./bench" 2>&1)"
	GO_OUT="$GO_OUT
$part"
done
echo "$GO_OUT" >&2

echo "== Python ==" >&2
PY_JSON="$("$PYTHON" "$HERE/bench_python.py" --root "$ROOT" --repeats "$REPEATS")"
echo "$PY_JSON" >&2

PY_JSON="$PY_JSON" GO_OUT="$GO_OUT" python3 - <<'PY'
import json
import os
import re

go = {}
for match in re.finditer(r"^(Benchmark\w+?)-\d+\s+\d+\s+([\d.]+) ns/op", os.environ["GO_OUT"], re.M):
    go[match.group(1)] = float(match.group(2))

py = json.loads(os.environ["PY_JSON"])


def row(label, go_ns, py_ns, unit="ms"):
    scale = 1e6 if unit == "ms" else 1.0
    go_value = go_ns / scale
    py_value = py_ns / scale
    ratio = py_ns / go_ns if go_ns else float("nan")
    print(f"{label:<28} {go_value:>10.3f} {py_value:>10.3f} {ratio:>8.2f}x   ({unit})")


print()
print(f"{'operation':<28} {'go':>10} {'python':>10} {'speedup':>9}")
print("-" * 60)
row("scan (whole library)", go["BenchmarkScanRoots"], py["scan_seconds"] * 1e9)
row("normalize (per name)", go["BenchmarkNormalize"], py["normalize_ns_per_name"], unit="ns")
row("dedup (per lookup)", go["BenchmarkFindDuplicate"], py["dedup_ns_per_lookup"], unit="ns")
print()
print(f"{py['albums']} album scopes, {py['names']} names folded")
PY
