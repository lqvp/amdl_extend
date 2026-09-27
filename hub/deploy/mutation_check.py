"""Mutation-check the invariants added in fix round 1.

The round-1 lesson was that two of the deployment's assertions could not fail: `user:` in
compose and a `sed` with no `grep` after it. So every new invariant gets a mutation here, and
a mutation that survives is a gap in the test rather than a fact about the deployment.
"""
import subprocess
from pathlib import Path

D = Path("Dockerfile")
C = Path("compose.yaml")
DI = Path(".dockerignore")
ENV = Path(".env.example")
DEDUP = Path("hub/hub/dedup.py")
APP = Path("hub/hub/app.py")
SCAN = Path("hub/hub/library_scan.py")
LIBAPI = Path("hub/hub/api/library.py")
TMPL = Path("hub/hub/web/templates/library.html")

MUTATIONS = {
    # -- the assigned fix -----------------------------------------------------
    "resolved[] back to matched[] in skip_reason": (
        APP, lambda t: t.replace("'|'.join(hit.resolved)", "'|'.join(hit.matched)")),
    "resolved stops being populated": (
        DEDUP, lambda t: t.replace(
            '        resolved=tuple(sorted(str(scan.roots[c.root_index] / c.relpath) for c in hits)),',
            '        resolved=tuple(),')),
    "resolved drops the root (relpath only)": (
        DEDUP, lambda t: t.replace(
            'resolved=tuple(sorted(str(scan.roots[c.root_index] / c.relpath) for c in hits)),',
            'resolved=tuple(sorted(c.relpath for c in hits)),')),
    "resolved left unsorted": (
        DEDUP, lambda t: t.replace(
            'resolved=tuple(sorted(str(scan.roots[c.root_index] / c.relpath) for c in hits)),',
            'resolved=tuple(str(scan.roots[c.root_index] / c.relpath) for c in hits),')),
    # -- important 1: the dead patterns -------------------------------------
    "**/*.db back to *.db (root-anchored again)": (
        DI, lambda t: t.replace("**/*.db\n", "*.db\n").replace("**/*.db-wal\n", "*.db-wal\n")
                     .replace("**/*.db-shm\n", "*.db-shm\n")),
    "the **/data/ pattern reverted": (
        DI, lambda t: t.replace("**/data/\n", "data/\n")),
    # -- important 3: PYTHONPATH ---------------------------------------------
    "PYTHONPATH back to the plan's /app": (
        D, lambda t: t.replace("ENV PYTHONPATH=/app/hub", "ENV PYTHONPATH=/app")),
    "the package moved but PYTHONPATH did not": (
        D, lambda t: t.replace("COPY hub/hub /app/hub/hub", "COPY hub/hub /app/hub/hub/vendored")),
    # -- important 5: the from-source build cost -----------------------------
    "the fresh-clone warning removed from the COPY": (
        D, lambda t: t.replace("*** A FRESH CLONE CANNOT BUILD THIS IMAGE. ***", "n/a")),
    # -- important 6: the per-root count --------------------------------------
    # Labelled for the *file it touches*. An earlier version of this list called the first of
    # these "dropped from the status response" while mutating `api/library.py`, which is the
    # /api/library/scan response -- so a reader could conclude /api/status was never covered.
    # It is covered: `api/__init__.py::_library_summary` is a separate function, and now has
    # its own mutation.
    "per_root dropped from the /api/library/scan response": (
        LIBAPI, lambda t: t.replace('            "per_root": listing["per_root"], ', "")),
    "per_root dropped from /api/status (_library_summary)": (
        Path("hub/hub/api/__init__.py"),
        lambda t: t.replace('        "per_root": list(scan.per_root()),\n', "")),
    "per_root lost from the library listing (the page reads it)": (
        LIBAPI, lambda t: t.replace('        "per_root": list(scan.per_root()),\n', "")),
    "per_root loses the unreadable root's slot": (
        SCAN, lambda t: t.replace(
            "        counts = [0] * len(self.roots)",
            "        counts = [0] * len(self.reachable)\n        counts = [0] * (len(self.roots) - 1)")),
    "the empty-root warning removed from the page": (
        TMPL, lambda t: t.replace(
            '{%- elif listing.per_root[loop.index0] == 0 -%}\n'
            '              <span class="error-text" role="alert">empty &mdash; is this mounted?</span>\n',
            "")),
    "the per-root table removed from the page": (
        TMPL, lambda t: t.replace("album directories", "x")),
    # -- minor 9: the reordering hazard documented where it is read ----------
    # -- round 2 -------------------------------------------------------------
    "resolved given a default so a caller can omit it": (
        DEDUP, lambda t: t.replace(
            "    resolved: tuple[str, ...]\n", "    resolved: tuple[str, ...] = ()\n")),
    "the /library/a containment TODO removed from .env.example": (
        ENV, lambda t: t.replace("TODO(spec \u00a78.1, Phase 2)", "later")),
    "the false 'KEEP /library/a FIRST' rationale reinstated": (
        ENV, lambda t: t.replace(
            "*** /library/a MUST BE IN THE LIST. ***", "*** KEEP /library/a FIRST. ***")),
    "the album table's path column back to a bare relpath": (
        TMPL, lambda t: t.replace("<code>{{ album.path }}</code>", "<code>{{ album.relpath }}</code>")),
    "the crash-restart test's poll back to 'pid changed'": (
        Path("hub/tests/test_supervisor.py"), lambda t: t.replace(
            'if any("is serving again on" in line for line in lines):',
            "if sup.running and sup.pid not in (None, first):")),

    # Deliberately NOT here: "the boot range in the README collapsed to one run's number".
    # A measurement is not an invariant -- it is a fact about four runs on one host, it cannot
    # be asserted without re-measuring, and a test that pinned a number in a README would only
    # make the number harder to correct. The four measurements are in the report and the range
    # is what the README quotes. Writing a test for it would be the "test that asserts
    # nothing" pattern this file exists to catch.
}


def run_mutation_check() -> int:
    survivors = []
    for label, (path, mutate) in MUTATIONS.items():
        original = path.read_text(encoding="utf-8")
        mutated = mutate(original)
        if mutated == original:
            print(f"  SKIP (did not apply)  {label}")
            survivors.append(label)
            continue
        path.write_text(mutated, encoding="utf-8")
        try:
            result = subprocess.run(
                ["uv", "run", "pytest", "-q", "--no-header", "-x", "-p", "no:cacheprovider"],
                cwd="hub", capture_output=True, text=True, timeout=900,
            )
        finally:
            path.write_text(original, encoding="utf-8")
        caught = result.returncode != 0
        name = ""
        for line in result.stdout.splitlines():
            if line.startswith(("FAILED", "ERROR")):
                name = line.split("::")[-1][:56]
                break
        print(f"  {'CAUGHT  ' if caught else 'SURVIVED'}  {label:<52} {name}")
        if not caught:
            survivors.append(label)
    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) survived: {survivors}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_mutation_check())
