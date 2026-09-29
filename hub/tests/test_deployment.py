"""The deployment's invariants, as tests.

**Why these are tests and not a review checklist.** Every one of them is a fact about the
Dockerfile and the compose file that *looks* right and produces a wrong deployment with no
error at any point. That is the whole category: the config mounted somewhere the client
never looks, the relative `dirPathFormat` that writes outside every scanned tree, the
`/app` on `PYTHONPATH` that turns the package into a namespace package, the healthcheck
that crash-loops a correct boot. None of them fail a build or a test, and all of them are
one keystroke away from being wrong.

**What they are not.** They do not build the image, start a container or assert that the
wrapper comes up. That is `hub/deploy/acceptance_check.py` plus a `docker compose up`, and
a test that shells out to a daemon is a test that is skipped on the machine where somebody
needs it most. So the split is: these hold the *static* contract, and the acceptance run
holds the dynamic one. The one thing these do reach outside this repository is
`Dockerfile`/`compose.yaml`/`hub/deploy/*.py` -- all of them committed, all of them
reviewed in the same diff as the code that would break them.

**The derivations are imported, not transcribed.** The vendor-root rule is
`parents[2]`, the environment names are whatever `load_settings` reads, and the
healthcheck budget has to exceed the supervisor's own `startup_timeout`. All three are
taken from the code rather than restated, because a copy is a second source of truth that
goes stale silently -- and each of the three has already been wrong once in this task's own
history, which is why they are worth a test at all.
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"
COMPOSE = REPO_ROOT / "compose.yaml"
# There is no overlay any more. The external drive used to be an optional second root added
# by `hub/deploy/compose.ntfs.yaml`, and the overlay file was deleted when the drive became
# the only library root: one file states the mount and the roots, so there is no second
# statement of the same value to keep in step -- which is the bug the overlay's `${VAR:-default}`
# interpolation was, since `.env` wins over a default and the drive ended up mounted and
# unscanned.
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
BUILD_GATE = REPO_ROOT / "hub" / "deploy" / "build_gate.py"
ACCEPTANCE = REPO_ROOT / "hub" / "deploy" / "acceptance_check.py"
# The workflow that publishes the image. Named as a path rather than assembled inside a
# test because it is read as plain text, not parsed: what is being asserted is which
# words appear in it, and a YAML parse of somebody else's schema would only add a second
# thing that can be right for the wrong reason.
CI_BUILD_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build.yml"


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #
def _dockerfile() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


def _dockerfile_instructions() -> list[tuple[str, str]]:
    """`[(VERB, joined-argument-text)]`, with continuations folded and comments dropped.

    A hand-rolled reader rather than one on the Dockerfile grammar, because the grammar is
    not the subject: the subject is a handful of `COPY`/`ENV` lines, and folding `\\` is all
    the structure needed to see them. Comment lines inside a continuation are dropped, which
    is what lets the Dockerfile carry its reasoning inline -- a test that broke on a comment
    would push that reasoning into a wiki, which is where it stops being read.
    """
    out: list[tuple[str, str]] = []
    verb: str | None = None
    parts: list[str] = []
    for raw in _dockerfile().splitlines():
        line = raw.strip()
        if verb is not None:
            line = line.lstrip("\\").strip()
            if line.startswith("#"):
                continue
            parts.append(line)
            if not raw.rstrip().endswith("\\"):
                out.append((verb, " ".join(parts).strip()))
                verb, parts = None, []
            continue
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        if rest.rstrip().endswith("\\"):
            verb, parts = head, [rest.rstrip().rstrip("\\").strip()]
        else:
            out.append((head, rest.strip()))
    assert verb is None, f"the Dockerfile ends inside a continuation: {' '.join(parts)!r}"
    return out


def _stage_copy_destinations() -> dict[str, str]:
    """`{source: destination}` for every `COPY --from=<stage>`, keyed by `<stage>:<source>`.

    The mirror of `_copy_destinations`, and separate rather than merged on purpose: the two
    answer different questions. A local `COPY`'s source is a path in this repository and can be
    checked for existence; a stage `COPY`'s source is a path in a *builder*, which cannot be,
    and asserting it anyway would only pass by accident. The `<stage>:` prefix keeps a
    `rootfs` in each from colliding, which is the collision `_copy_destinations`' docstring
    already warned about.
    """
    found: dict[str, str] = {}
    for verb, args in _dockerfile_instructions():
        if verb != "COPY" or "--from=" not in args:
            continue
        parts = args.split()
        if len(parts) < 3:
            continue
        stage = next(p.split("=", 1)[1] for p in parts if p.startswith("--from="))
        *sources, destination = [p for p in parts if not p.startswith("--from=")]
        for source in sources:
            found[f"{stage}:{source}"] = destination
    return found


def _copy_destinations() -> dict[str, str]:
    """`{source: destination}` for every `COPY` whose source exists in this repository.

    `--from=` stages are excluded: their source is another image, not a path, and a
    `wrapper/rootfs` appearing in both a `--from` and a local `COPY` must not collide.
    """
    found: dict[str, str] = {}
    for verb, args in _dockerfile_instructions():
        if verb != "COPY" or "--from=" in args:
            continue
        parts = args.split()
        if len(parts) < 2:
            continue
        *sources, destination = parts
        for source in sources:
            found[source] = destination
    return found


def _cloned_vendor_root() -> str:
    """The absolute path the Dockerfile clones the vendor client into.

    Read from the instruction that does it rather than transcribed, because that path is a
    *derivation* and not a preference: `ripper_host._VENDOR_ROOT` and
    `app.vendor_config_path` both compute `Path(__file__).resolve().parents[2] /
    "AppleMusicDecrypt"`, so wherever the image puts the client, the code decides.

    It used to be read off a `COPY` destination, because a `COPY` is how the client used to
    get there. The client is cloned at build time now, so there is no `COPY` to read and the
    clone target is the only place the path is stated at all. **The invariant did not change --
    only the spelling of it moved**, and a test still looking for the old spelling would fail
    on a correct image while passing on a broken one.
    """
    found: list[str] = []
    for verb, args in _dockerfile_instructions():
        if verb != "RUN":
            continue
        # A RUN is a sequence of `;`-separated commands, and the clone is one of them.
        for segment in args.split(";"):
            tokens = segment.split()
            if "clone" not in tokens or not tokens[-1].startswith("/"):
                continue
            # The basename is fixed by the code's own derivation, so matching it selects the
            # right clone without transcribing it. What the caller then asserts is the part
            # that is *not* fixed: that the full path agrees with `parents[2]`.
            if Path(tokens[-1]).name == "AppleMusicDecrypt":
                found.append(tokens[-1])
    assert len(found) == 1, (
        f"expected exactly one instruction cloning the vendor client to an absolute path named "
        f"AppleMusicDecrypt, found {found}. The image has to put the client where the code "
        f"derives that it will be, so this has to be readable here rather than assumed."
    )
    return found[0]


def _env_pairs() -> dict[str, str]:
    """Every `ENV key=value` in the Dockerfile, last one winning as Docker does."""
    found: dict[str, str] = {}
    for verb, args in _dockerfile_instructions():
        if verb != "ENV":
            continue
        if "=" not in args.split()[0]:
            # The `ENV k v` form, which is not used here and is not worth supporting: a
            # reader that silently mis-parses it would make this test worse than nothing.
            pytest.fail(f"Dockerfile uses the `ENV key value` form: {args!r}")
        key, _, value = args.partition("=")
        found[key.strip()] = value.strip().strip('"')
    return found


def _compose(*paths: Path) -> dict:
    return yaml.safe_load("\n".join(p.read_text(encoding="utf-8") for p in paths))


def _service(compose: dict, name: str = "amd-hub") -> dict:
    return compose["services"][name]


def _default_of(spec: str) -> str:
    """The default out of a compose interpolation, `${VAR:-default}`.

    A test that reads the whole spec when it means the default is a test that passes for
    the wrong reason: `"${AMD_LIBRARY_ROOTS:-/library/a,/library/b}".endswith("/library/b")`
    is False, so a real mismatch here shows up as a confusing failure rather than as the
    missing root it is.
    """
    match = re.search(r":-([^}]*)}", spec)
    assert match, f"{spec!r} is not a ${{VAR:-default}} interpolation"
    return match.group(1)


def _split_outside_braces(entry: str) -> list[str]:
    """Split on `:` at brace depth zero.

    Both halves of a compose mapping can be interpolated, and `${VAR:-default}` contains a
    colon of its own. `str.split(":")` therefore reads
    `${AMD_LIBRARY_HOST:-/some/host/path}:/library` as three fields and hands back
    `-/some/host/path}` as the target -- which is how this file's first draft ended
    up reporting that a correct overlay "had not attached the drive", and how an earlier
    draft read the published port as `${AMD_HTTP_PORT`.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in entry:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        if char == ":" and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _port_split(entry: str) -> tuple[str, str]:
    """`"host:container"` -> `(host, container)`."""
    parts = _split_outside_braces(entry)
    assert len(parts) == 2, f"{entry!r} is not a host:container port mapping"
    return parts[0], parts[1]


def _volume_split(entry: str) -> tuple[str, str]:
    """`"source:target[:mode]"` -> `(source, target)`."""
    parts = _split_outside_braces(entry)
    assert len(parts) >= 2, f"{entry!r} is not a source:target volume mapping"
    return parts[0], parts[1]


def _mounts(service: dict) -> list[dict]:
    """`volumes:` as `{source, target, create_host_path}`, for either bind syntax.

    Compose accepts `"src:/dst"` and the long `{type: bind, source:, target:,
    bind: {create_host_path:}}`. Reading the list as strings -- which every test here used to
    do -- made the switch to the long form break six tests at once, none of which was about
    the thing that changed. `create_host_path` is only present in the long form, and its
    absence from the short form is the whole hazard, so the key is normalised to `True` there
    rather than omitted.
    """
    out: list[dict] = []
    for entry in service.get("volumes", []):
        if isinstance(entry, dict):
            out.append(
                {
                    "source": entry.get("source", ""),
                    "target": entry.get("target", ""),
                    # Absent means True: compose's short form creates the host path.
                    "create_host_path": entry.get("bind", {}).get("create_host_path", True),
                }
            )
            continue
        source, target = _volume_split(entry)
        out.append({"source": source, "target": target, "create_host_path": True})
    return out


def _excluded_from_context(path: str) -> bool:
    """Whether `.dockerignore` keeps `path` out of the build context.

    **This models Docker's matching, which is path-based and is NOT gitignore's.** That
    distinction is the whole reason this function had to be rewritten, and the reason the
    first version of it was useless: in a `.gitignore`, a pattern with no `/` matches the
    basename at any depth, so `*.db` reads as "any database anywhere". In a `.dockerignore`
    it matches **the context root only**. A pattern is compared against the whole
    context-relative path, so `*.db` excludes `/x.db` and nothing under `hub/`.

    Which is how `hub/hub/web/worker-check.db` shipped inside the image for the whole of
    task 10: `.dockerignore` said `*.db`, the file said `**/*.db`, and every test that read
    the ignore file asked about root-anchored paths and so agreed with itself.

    Three rules, and they are Docker's:

    - `**` matches any number of path segments, so `**/*.db` covers every depth and `*.db`
      covers none below the root.
    - a trailing `/` is dropped, and a path is excluded when it *or any directory above it*
      is matched -- Docker prunes the directory, so `hub/tests/` covers
      `hub/tests/conftest.py`.
    - patterns are applied in order and the last match wins, `!` negating.

    Segment matching is `fnmatch`, so `*`, `?` and `[...]` all work within a segment. The
    remaining judgement is `**`, which `fnmatch` does not have.
    """
    excluded = False
    for pattern, negated in _dockerignore_patterns():
        if _pattern_covers(pattern, path):
            excluded = not negated
    return excluded


def _dockerignore_patterns() -> list[tuple[str, bool]]:
    """`[(pattern, negated)]` in file order, trailing slashes dropped."""
    patterns: list[tuple[str, bool]] = []
    for raw in DOCKERIGNORE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        assert line, f".dockerignore has a bare '!' line: {raw!r}"
        patterns.append((line.rstrip("/"), negated))
    return patterns


def _pattern_covers(pattern: str, path: str) -> bool:
    """Whether `pattern` matches `path` or any directory above it."""
    segments = [path]
    parts = path.split("/")
    for cut in range(1, len(parts)):
        segments.append("/".join(parts[:cut]))
    return any(_segments_match(pattern.split("/"), s.split("/")) for s in segments)


def _segments_match(pattern: list[str], path: list[str]) -> bool:
    """`fnmatch` per segment, with `**` spanning any number of segments."""
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        if len(pattern) == 1:
            return True
        return any(
            _segments_match(pattern[1:], path[index:]) for index in range(len(path) + 1)
        )
    if not path:
        return False
    if not fnmatch.fnmatchcase(path[0], head):
        return False
    return _segments_match(pattern[1:], path[1:])


def test_the_exclusion_helper_answers_for_a_nested_path_and_says_so():
    """The helper's own regression test, and the reason it was rewritten.

    The cases that matter are the nested ones. A helper that could not answer for them is
    worse than no helper: every assertion built on it looks like it checked something, and
    this one agreed with itself about `*.db` for the whole of task 10.
    """
    # A `**/` pattern reaches any depth...
    assert _excluded_from_context("hub/hub/web/worker-check.db")
    assert _excluded_from_context("a/b/c/d/e/deep.db-wal")
    # ...and including zero segments, so the context root is covered by the same pattern.
    assert _excluded_from_context("x.db")
    # A directory pattern covers its contents, because Docker prunes the directory...
    assert _excluded_from_context("hub/tests/conftest.py")
    # ...but not a sibling whose name merely starts the same way.
    assert not _excluded_from_context("hub/testsx/thing.py")
    # This used to assert that `wrapper/rootfs` and `wrapper/rootfs/system` survive, back when
    # the context carried a submodule and only `wrapper/rootfs/data/` could be excluded. Both
    # upstream trees are now cloned at build time and excluded whole, so the case it guarded
    # cannot arise: there is no context shape in which the rootfs ships. The `hub/tests`
    # pattern above still carries the "a directory pattern prunes its contents" half, which is
    # the part of the matcher this test exists to pin.
    assert _excluded_from_context("wrapper/rootfs/system/lib64/libcurl.so")
    # The negation is for the example file.
    assert not _excluded_from_context(".env.example")
    # And the tree still has to be in the context, or there is no image to ship.
    assert not _excluded_from_context("hub/hub/app.py")


def test_the_deep_exclusions_are_written_with_a_leading_globstar():
    """Read off the file rather than inferred from the matcher.

    The matcher and the `.dockerignore` could agree while both were wrong, and did: `*.db`
    plus a helper that only ever asked about root-anchored paths agreed perfectly, and
    `hub/hub/web/worker-check.db` shipped in the image. So what is asserted here is the
    *spelling* of the patterns that have to reach any depth, which no amount of
    self-consistent matching can establish.
    """
    patterns = {pattern for pattern, _ in _dockerignore_patterns()}
    # `rstrip("/")` because the reader strips it, so `**/data/` is spelled `**/data` here.
    for suffix in ("*.db", "*.db-wal", "*.db-shm", ".env", "data", "tmp"):
        matching = [p for p in patterns if p == suffix or p == f"**/{suffix}"]
        assert matching, f"nothing in .dockerignore covers {suffix!r}"
        assert all(p.startswith("**/") for p in matching), (
            f"{sorted(matching)} must be written with a leading '**/' or they match the "
            f"context root only, which is how a nested database ended up in the image"
        )


# --------------------------------------------------------------------------- #
# The vendor-root derivation -- the one an earlier layout got wrong
# --------------------------------------------------------------------------- #
def test_the_client_lands_where_parents_2_of_the_package_points():
    """`<package>/../../AppleMusicDecrypt` is where the client has to be, not /opt.

    `ripper_host._VENDOR_ROOT` and `app.vendor_config_path` both compute
    `Path(__file__).resolve().parents[2] / "AppleMusicDecrypt"`, so the location is
    *derived*, not configured -- there is no environment variable for it and no argument.
    An earlier layout said `COPY AppleMusicDecrypt /opt/AppleMusicDecrypt`; with the package
    at `/app/hub/hub` that puts the client where the derivation will never look, and the
    failure is a `RipperHostError` naming a path that exists and is not the one it wanted,
    at every boot, after a build that reported success.

    **The client is cloned here, not COPYed**, so the path is read off the clone rather than
    off a `COPY` destination. Same invariant, later spelling; see `_cloned_vendor_root`.
    """
    client = _cloned_vendor_root()
    assert client == "/app/AppleMusicDecrypt", (
        f"the client is cloned to {client}, but parents[2] of the installed package is "
        f"/app, so the derivation looks in /app/AppleMusicDecrypt. Move the client to match "
        f"the derivation, or move the package and the PYTHONPATH together -- what must not "
        f"happen is the two disagreeing."
    )

    package = _copy_destinations()["hub/hub"]
    assert package == "/app/hub/hub", f"the package is at {package}, not /app/hub/hub"
    # The arithmetic itself, in the code's terms: `Path(__file__).parents[2]` where
    # `__file__` is /app/hub/hub/<module>.py is parents[1] of the package *directory*.
    assert Path("/app/hub/hub").parents[1] / "AppleMusicDecrypt" == Path(client), (
        "the arithmetic itself: parents[2] of a module inside /app/hub/hub has to be the "
        "directory holding the client"
    )


def test_pythonpath_is_the_directory_that_contains_the_package():
    """`/app/hub` contains the package; `/app` merely contains the project.

    `import hub` needs a sys.path entry that holds `hub/__init__.py`. The package is COPYed to
    `/app/hub/hub`, so that entry is `/app/hub`. `/app` holds `hub/` too, but as a plain
    directory with no `__init__.py`, so it can only ever contribute a **namespace** package
    whose `hub.app` does not resolve -- and, in the two-portion case, whose `__path__` holds
    the same directory twice.

    **What this deliberately does not claim.** The first version of this test asserted that
    `PYTHONPATH=/app` "makes the container die at `CMD`", and that is false for this image.
    Measured inside it, `import hub.app`:

        PYTHONPATH   CWD         hub.__path__             result
        /app/hub     /app/hub    ['/app/hub/hub']         regular
        /app         /app/hub    ['/app/hub/hub']         regular   <- rejected
        (unset)      /app/hub    ['/app/hub/hub']         regular
        /app/hub     /           ['/app/hub/hub']         regular
        /app         /           ['/app/hub']             NAMESPACE
        (unset)      /           import fails outright

    Under `-m`, `sys.path[0]` is the working directory, and `WORKDIR /app/hub` already *is* the
    package root -- so `/app` and even no `PYTHONPATH` at all work, by accident of one
    `WORKDIR`. The property that actually matters, and what is asserted here, is that the
    value is correct **independently of the working directory**: a `working_dir:` in compose, a
    different base image, or running a script from anywhere else turns `/app` into a namespace
    package. That failure lands in the build gate first, which is a better place than the first
    request, but it is a latent break either way and "it currently works" is not a property.
    """
    env = _env_pairs()
    package = _copy_destinations()["hub/hub"]
    assert Path(env.get("PYTHONPATH", "")) == Path(package).parent, (
        f"PYTHONPATH is {env.get('PYTHONPATH')!r} and the package is at {package}, so "
        f"PYTHONPATH has to be {Path(package).parent} -- the directory that CONTAINS the "
        f"package. Its parent ({Path(package).parent.parent}) is the project directory, which "
        f"holds 'hub/' as a plain directory and can only ever yield a namespace package."
    )
    # The falsified alternative, stated as an assertion so the value cannot drift back to it
    # with the old (wrong) reasoning still in the file.
    project = Path(package).parent.parent
    assert (project / "hub" / "__init__.py").parent != package, (
        "if the package and the project directory ever coincide, the namespace-package "
        "distinction this test rests on no longer applies and the reasoning needs revisiting"
    )
    # One import mechanism. An editable install of the project would put /app/hub on the path
    # as well, and `import hub` would then resolve by whichever entry sys.path yields first.
    instructions = _dockerfile_instructions()
    assert not any(
        verb == "RUN" and "uv sync" in args and "--no-install-project" not in args
        for verb, args in instructions
    ), "the project is installed as well as being on PYTHONPATH; keep one import mechanism"
    # And the vendor tree stays off it, so `src.*` keeps resolving through the seam.
    assert "AppleMusicDecrypt" not in env.get("PYTHONPATH", ""), (
        "the vendor tree must not be on PYTHONPATH: `src.*` has to resolve through the "
        "seam's own sys.path insert, which test_ripper_host.py enforces"
    )


def test_the_vendor_config_is_built_at_the_only_path_the_client_ever_opens():
    """`ConfigCreator` calls `load_from_config()` with its own default: `"config.toml"`.

    There is no seam and no creart hook to pass a path through, so the only file the client
    will read is `<CWD>/config.toml` -- and `RipperHost` holds the CWD at the vendor root to
    make that true. A config at any other path is not a different config, it is a config
    that is silently ignored while the image's own copy is used instead.

    So two things have to hold: the image builds its config *into* the vendor root, and the
    operator's host `config.toml` -- which is gitignored and host-specific -- never enters
    the context that would overwrite it.
    """
    assert "config.example.toml config.toml" in _dockerfile(), (
        "the image should build <vendor>/config.toml from upstream's config.example.toml, so "
        "there is one source of truth for the ~150 settings instead of a forked copy"
    )
    # The file is created *inside* the directory the client was cloned into, rather than being
    # COPYed to a path of its own -- so the test follows the RUN and takes the client path from
    # the same helper the vendor-root test uses, instead of looking for a COPY destination that
    # the build no longer has.
    vendor = _cloned_vendor_root()
    making = [
        args
        for verb, args in _dockerfile_instructions()
        if verb == "RUN" and "cp config.example.toml config.toml" in args
    ]
    assert making, "no instruction builds the vendor config"
    assert f"cd {vendor}" in making[0], (
        f"the config has to be written into {vendor}, because that is the only path the "
        f"client opens; this instruction is: {making[0][:120]}"
    )
    assert _excluded_from_context("AppleMusicDecrypt/config.toml"), (
        "the operator's real config.toml must stay out of the build context, or it silently "
        "becomes the image's config"
    )


def test_download_paths_are_absolute_and_inside_the_first_library_root():
    """A relative `dirPathFormat` writes to a tree the hub never scans.

    The seam `chdir`s into the vendor root, so the example's
    `downloads/{album_artist}/{album}` resolves to `/app/AppleMusicDecrypt/downloads/...`
    while the library scan reads the bind mount at `/library/a`. The client then fills a
    tree nothing dedups against and every track downloads again, forever, with no error at
    any point -- the worst of the failure modes in this file, because nothing is red.
    """
    dockerfile = _dockerfile()
    for key in ("dirPathFormat", "playlistDirPathFormat"):
        line = next(
            (
                ln
                for ln in dockerfile.splitlines()
                if "sed -i" in ln and f"^{key} = " in ln
            ),
            None,
        )
        assert line is not None, f"no sed for [download].{key} in the Dockerfile"
        assert "${AMD_DOWNLOAD_ROOT}" in line, (
            f"[download].{key} is not rewritten from AMD_DOWNLOAD_ROOT; it has to be absolute "
            f"and it has to be the same tree the library scan reads, or downloads land outside "
            f"every root the hub deduplicates against"
        )
    # The image's own roots, with no AMD_LIBRARY_ROOTS in the environment. There is one
    # library now, so "the first root" and "the only root" are the same statement.
    assert _env_pairs()["AMD_LIBRARY_ROOTS"] == "${AMD_DOWNLOAD_ROOT}", (
        "the image's library roots come from the same argument as the write root; setting "
        "them independently is what let .env move the scan away from where the client writes"
    )


def test_the_clients_write_root_is_contained_in_the_scanned_roots():
    """`dirPathFormat` is baked absolute, so the list has to *contain* its root.

    The image hard-codes `dirPathFormat` from `AMD_DOWNLOAD_ROOT` because a relative value
    resolves against the vendor root the seam chdirs into. So the client **always** writes
    to that one directory, whatever the environment says.

    **Containment, not order.** An earlier version of this test asserted that the root had to
    be *first*, on the stated grounds that reordering would make dedup read a tree the client
    does not write to. That is false, and it was measured rather than argued: `scan_roots` is
    handed the whole list and builds one index across every root, so `library_roots[0]` is
    never read at runtime at all -- it appears in `hub/deploy/build_gate.py` and nowhere else.
    A test built on a false premise is worse than no test, because it makes the wrong belief
    load-bearing.

    With one library there is no order left to get wrong, and the real rule is stronger than
    before: the write root and the scan roots are the *same literal* in two files, so a
    disagreement is a build error rather than a runtime silence.
    """
    dockerfile = _dockerfile()
    # The baked path is now written as `${AMD_DOWNLOAD_ROOT}/...`, so the root is the
    # *argument's* value rather than a literal on the sed line. Reading the literal is what
    # this test used to do, and it is the reason the two could drift.
    image_roots = re.search(r"AMD_DOWNLOAD_ROOT=(\S+)", dockerfile).group(1)
    write_root = image_roots
    assert write_root == "/library", write_root
    # And the sed has to actually use the argument, not a path that merely looks right.
    for key in ("dirPathFormat", "playlistDirPathFormat"):
        sed_line = next(
            (ln for ln in dockerfile.splitlines() if f"s|^{key} = .*|" in ln), None
        )
        assert sed_line and "${AMD_DOWNLOAD_ROOT}" in sed_line, (
            f"[download].{key} is not rewritten from AMD_DOWNLOAD_ROOT, so the root the image "
            f"scans and the path the client writes into are two independent literals again"
        )

    shipped = [
        [image_roots],
        _service(_compose(COMPOSE))["environment"]["AMD_LIBRARY_ROOTS"].split(","),
    ]
    for roots in shipped:
        assert write_root in roots, (
            f"{roots} does not contain {write_root}, so the client would write to a tree "
            f"nothing scans -- every track re-downloads for ever, with no error anywhere"
        )
    # And the build argument compose passes has to be the same value, or the image bakes one
    # root while the runtime scans another.
    assert re.search(
        r"AMD_DOWNLOAD_ROOT:\s*(\S+)", COMPOSE.read_text(encoding="utf-8")
    ).group(1) == write_root

def test_runtime_write_root_validation_replaces_the_documented_todo():
    """The operator-edited roots are guarded in the actual startup path now."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "TODO(spec §8.1, Phase 2)" not in text
    source = (REPO_ROOT / "hub" / "hub" / "app.py").read_text(encoding="utf-8")
    assert "validate_download_root(state.ripper_config_path, state.settings.library_roots)" in source
    assert source.index("validate_download_root(state.ripper_config_path") < source.index(
        "await state.supervisor.start()"
    ), "the root check must happen before starting the wrapper"

    # The false rationale must remain gone from both files that carried it.
    for path in (COMPOSE, ENV_EXAMPLE):
        body = path.read_text(encoding="utf-8")
        assert "KEEP /library/a FIRST" not in body
        assert "so /library comes first" not in body


def test_vendor_download_root_uses_only_the_static_format_prefix():
    from hub.app import download_root_from_format

    assert download_root_from_format("/library/{artist}/{album}") == Path("/library")
    assert download_root_from_format("/library/music") == Path("/library/music")
    with pytest.raises(ValueError, match="absolute"):
        download_root_from_format("library/{artist}/{album}")
    with pytest.raises(ValueError, match="contain"):
        download_root_from_format("/library/{artist}/../outside")


def test_vendor_download_root_must_be_inside_one_scan_root(tmp_path):
    from hub.app import validate_download_root

    config = tmp_path / "config.toml"
    config.write_text('[download]\ndirPathFormat = "/library/{artist}/{album}"\n', encoding="utf-8")
    validate_download_root(config, (Path("/archive"), Path("/library")))

    config.write_text('[download]\ndirPathFormat = "/elsewhere/{artist}/{album}"\n', encoding="utf-8")
    with pytest.raises(RuntimeError) as error:
        validate_download_root(config, (Path("/library"),))
    assert "/elsewhere/{artist}/{album}" in str(error.value)
    assert "/library" in str(error.value)


def test_vendor_download_root_comparison_does_not_resolve_symlinks(tmp_path):
    from hub.app import validate_download_root

    real = tmp_path / "real"
    (real / "music").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    config = tmp_path / "config.toml"
    value = f"{alias}/music/{{album}}"
    config.write_text(f'[download]\ndirPathFormat = "{value}"\n', encoding="utf-8")
    with pytest.raises(RuntimeError, match="alias"):
        validate_download_root(config, (real / "music",))


def test_the_operator_is_told_a_separate_drive_is_not_required():
    """The portability claim, pinned, because it is the one this change makes.

    The deployment no longer needs a second volume: `AMD_LIBRARY_HOST` names any directory
    and the only thing compose does with it is bind it. `.env.example` is what a new
    operator reads before anything else, and a sentence telling them a separate drive is
    required is the exact wrong thing to leave behind in it -- a machine with no external
    drive would conclude the stack cannot be run there at all.

    Asserted as a positive claim rather than a banned phrase, because the wrong wording is
    unbounded: a reworded "you will need an external disk" would slip past a test that only
    banned the one sentence that was there.
    """
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for claimed in (
        "it does not have to be a separate drive",
        "it does not have to be NTFS",
    ):
        assert claimed in text, (
            f".env.example should tell the operator {claimed!r} explicitly. The deployment "
            f"binds whatever directory AMD_LIBRARY_HOST names, so a machine with no external "
            f"volume can run it, and this is the file that has to say so."
        )
    for wrong in ("must be a separate drive", "must be NTFS", "external drive is required"):
        assert wrong not in text, (
            f".env.example still says {wrong!r}, which is false: the library is any "
            f"directory the operator names"
        )


# --------------------------------------------------------------------------- #
# Environment names
# --------------------------------------------------------------------------- #
def _names_load_settings_reads() -> set[str]:
    """The `AMD_*` names `load_settings` actually reads, from the source.

    Parsed out of `config.py` rather than listed, so a new setting cannot be added without
    this noticing and cannot be misspelled without this noticing. The helper calls are the
    only ways a value is read; anything else in the function is a constant.
    """
    source = (REPO_ROOT / "hub" / "hub" / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names: set[str] = set()
    # `_required_paths` is here because AMD_LIBRARY_ROOTS is read through it and has no
    # default, which is the whole point of the helper. Leaving it out would make the
    # deployment look like it sets a variable `load_settings` never reads -- which is
    # exactly the misspelling this reader exists to catch, aimed the other way.
    helpers = {"_text", "_port", "_paths", "_required_paths", "_scope"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in helpers:
            # The helpers take the environment mapping first and the variable name second,
            # but the mapping is bound to `source` in `load_settings` and to `env` in the
            # default-parameter tests -- so it is matched by position and type, not by
            # identifier. Matching the identifier found exactly one of the eleven names,
            # and the test passed for a whole task on the strength of it.
            if len(node.args) >= 2 and isinstance(node.args[0], ast.Name):
                second = node.args[1]
                if isinstance(second, ast.Constant) and isinstance(second.value, str):
                    names.add(second.value)
        elif isinstance(func, ast.Attribute) and func.attr in ("get", "pop"):
            # AMD_PASSWORD and AMD_SESSION_SECRET are read with a bare `.get` rather than
            # through a helper, so a reader that only understood the helpers would miss
            # them -- and would then report the deployment for setting one.
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.startswith("AMD_")
            ):
                names.add(node.args[0].value)
    return names


# The `AMD_` names the *deployment* consumes rather than the application. Each is here
# because it starts with the app's prefix and would otherwise read as a typo, and each is
# listed with what actually consumes it -- an unexplained list of three exceptions is just a
# test that has stopped testing.
_DEPLOYMENT_LEVEL_NAMES = {
    # A compose variable: the host-side half of the published port. Never reaches the
    # container, so `load_settings` has no business reading it.
    "AMD_HTTP_PORT",
    # A Dockerfile build ARG, consumed by the `sed` that writes [region].language into the
    # image's config. It has to be a build input, not an environment variable, because
    # upstream's config loader has no environment variable for anything.
    "AMD_VENDOR_LANGUAGE",
    # A compose variable: the host directory behind the library bind. Compose consumes it to
    # build the bind's `source` and it never reaches the container, which is told `/library`
    # instead. The same shape as AMD_HTTP_PORT -- a host-side half of a container-side
    # constant -- and `load_settings` has no business reading either.
    "AMD_LIBRARY_HOST",
}


def test_the_deployment_level_amd_names_are_the_ones_this_file_explains():
    """The exception list is itself under test, because it is the easiest thing to grow.

    A new `AMD_` name added to the deployment has to be *either* read by `load_settings`
    *or* listed above with a reason. Anything else fails `test_no_amd_variable_is_invented`,
    and the fix for that failure is to understand which of the two it is -- not to add it
    here, which is why this asserts the list has not quietly become a dumping ground.
    """
    used: set[str] = set(_env_pairs())
    for compose_path in (COMPOSE,):
        used.update(
            name
            for name in _service(_compose(compose_path)).get("environment", {})
            if name.startswith("AMD_")
        )
    used.update(
        re.findall(r"^#?\s*(AMD_[A-Z_]+)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)
    )
    # Every entry in the exception list has to still be used, so a retired knob is removed
    # from the list rather than left behind as an allowance for a name nobody sets.
    stale = _DEPLOYMENT_LEVEL_NAMES - used
    assert not stale, f"{sorted(stale)} are excused but nothing sets them any more"


def test_no_amd_variable_is_invented():
    """Every `AMD_*` the deployment sets is one `load_settings` reads.

    A misspelled variable is the quietest possible misconfiguration: `load_settings` does not
    validate the environment, it reads the names it wants and ignores the rest, so
    `AMD_WRAPPER_BINRARY` yields a hub that silently runs the default binary and reports
    nothing wrong. This is the test that says "the names are the code's, not ours".
    """
    known = _names_load_settings_reads()
    assert known, "failed to read any AMD_* names out of config.py; the reader is broken"

    used: set[str] = set()
    used.update(k for k in _env_pairs() if k.startswith("AMD_"))
    for compose_path in (COMPOSE,):
        compose = _compose(compose_path)
        for env in _service(compose).get("environment", {}):
            if env.startswith("AMD_"):
                used.add(env)
    used.update(re.findall(r"^#?\s*(AMD_[A-Z_]+)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M))

    invented = used - known - _DEPLOYMENT_LEVEL_NAMES
    assert not invented, (
        f"{sorted(invented)} are set by the deployment but read by nothing in config.py, "
        f"and are not listed in _DEPLOYMENT_LEVEL_NAMES. load_settings only reads "
        f"{sorted(known)}, so these would be silently ignored -- a misspelled "
        f"AMD_WRAPPER_BINRARY yields a hub that quietly runs the default binary."
    )


def test_every_setting_load_settings_reads_is_somewhere_in_the_deployment():
    """The other direction, and the one that catches a knob nobody wired up.

    A variable `load_settings` reads but neither the image nor compose nor `.env.example`
    mentions is a setting an operator cannot discover. `AMD_SESSION_SECRET` was the one that
    mattered: it is safe to leave unset, but the cost is a logout on every restart, and that
    is only knowable from a line in a file the operator is expected to read.
    """
    documented = set(_env_pairs())
    for compose_path in (COMPOSE,):
        documented.update(_service(_compose(compose_path)).get("environment", {}))
    documented.update(
        re.findall(r"^#?\s*(AMD_[A-Z_]+)=", ENV_EXAMPLE.read_text(encoding="utf-8"), re.M)
    )
    # The image fallback for AMD_LIBRARY_ROOTS and compose's own setting both count.
    undocumented = _names_load_settings_reads() - documented
    assert not undocumented, (
        f"{sorted(undocumented)} are read by load_settings and mentioned by neither the "
        f"Dockerfile, compose, nor .env.example. An operator cannot discover a setting they "
        f"cannot find."
    )


def test_the_wrapper_binary_is_a_rootfs_launcher_and_not_the_qemu_one():
    """`wrapper-lite-qemu` cannot do 2FA from the host, at all.

    It has no `rootfs` on the host and passes `--base-dir` *into the guest*, so the file the
    hub writes for a 2FA code lands in a namespace the hub cannot see --
    `WrapperSupervisor.submit_2fa` refuses by design rather than writing a file nobody will
    read. It also needs KVM, a guest image and a boot, none of which this image has.

    `config.py`'s `DEFAULT_WRAPPER_BINARY` is the QEMU launcher, and it is correct for the
    upstream desktop deployment, so the image has to override it rather than the default
    changing.
    """
    binary = _env_pairs()["AMD_WRAPPER_BINARY"]
    assert binary == "/opt/wrapper/wrapper-lite-rootless", binary
    assert "qemu" not in binary, (
        f"{binary} is the QEMU launcher: it has no host rootfs, so the 2FA handoff cannot "
        f"work, and it needs a guest this image does not carry"
    )
    # The layout the launcher actually requires: <dir>/launcher + <dir>/rootfs as siblings.
    assert binary.rsplit("/", 1)[0] == "/opt/wrapper"
    # From the builder stage, since that is where the payload is compiled now. The assertion
    # is the destination, not the source: the rootfs has to be a sibling of the launcher,
    # because it chroots into ./rootfs relative to its own working directory and the
    # supervisor reproduces that mapping on the host side to place the 2FA file. A build
    # stage can move; `/opt/wrapper/rootfs` next to `/opt/wrapper/wrapper-lite-rootless`
    # cannot.
    stage_copies = _stage_copy_destinations()
    assert stage_copies.get("wrapper-build:/src/wrapper/rootfs") == "/opt/wrapper/rootfs", (
        f"the rootfs has to be a sibling of the launcher, and the stage COPYs are "
        f"{stage_copies}"
    )
    assert stage_copies.get("wrapper-build:/src/wrapper/wrapper-lite-rootless") == (
        "/opt/wrapper/wrapper-lite-rootless"
    ), f"the launcher is not being taken from the builder stage: {stage_copies}"
    # And no *local* COPY lands in /opt/wrapper, which is what would make the image depend on
    # the host having run a build first. Scoped by destination rather than by source: the
    # builder stage's own `COPY wrapper/ /src/wrapper/` is a local COPY and entirely correct,
    # and a source-prefix rule would flag it as the thing it is not.
    leaked = {
        src: dst for src, dst in _copy_destinations().items()
        if dst == "/opt/wrapper" or dst.startswith("/opt/wrapper/")
    }
    assert not leaked, (
        f"{leaked} come from the build context, so the image needs a host build after all; the "
        f"launcher and the rootfs must come from the builder stage"
    )


def test_no_apple_credentials_are_baked_into_the_image_or_the_compose_file():
    """Apple credentials reach the wrapper on a child process's argv, and nowhere else.

    That is a *deliberate* exposure -- argv is the binary's only input -- and it is
    bounded by the topology: one service process, one uid, in one container. That
    argument only holds if nothing else put a credential where `docker inspect` or
    `docker history` would show it, so the image and the compose file have to be clean.

    `AMD_PASSWORD` is the hub's own password and belongs in `.env`, which is gitignored and
    excluded from the context. The one `AMD_PASSWORD` the Dockerfile may set is the build
    gate's throwaway, which is asserted here rather than trusted.
    """
    for compose_path in (COMPOSE,):
        raw = compose_path.read_text(encoding="utf-8")
        for variable in _service(_compose(compose_path)).get("environment", {}):
            match = re.search(rf"^\s*{variable}:\s*(.+?)\s*$", raw, re.M)
            if match and variable == "AMD_PASSWORD":
                assert "${AMD_PASSWORD" in match.group(1), (
                    f"{compose_path.name} sets {variable} to a literal; it must come from the "
                    f"environment so no password is ever in a tracked file"
                )
    # The build gate is the only place the Dockerfile may name a password, and it must be
    # obviously not one.
    gate_lines = [
        line
        for line in _dockerfile().splitlines()
        if "AMD_PASSWORD" in line and not line.strip().startswith("#")
    ]
    assert len(gate_lines) == 1, (
        f"the Dockerfile names AMD_PASSWORD on {len(gate_lines)} non-comment lines; only the "
        f"build gate's throwaway is allowed and it must be visibly a throwaway"
    )
    assert "build-time-check-only" in gate_lines[0], gate_lines[0]
    assert "${" not in gate_lines[0] and "=" not in gate_lines[0].split("AMD_PASSWORD")[0]


# --------------------------------------------------------------------------- #
# The container's security posture
# --------------------------------------------------------------------------- #
def test_the_container_runs_as_root_because_the_uid_map_requires_it():
    """No `user:`, and that is a hard requirement rather than a default.

    The launcher `unshare(CLONE_NEWUSER)`s with a single-uid map -- `0 0 1`,
    `wrapper-lite-rootless.c:49` -- *before* it touches the filesystem. A map of one uid
    leaves every file owned by any other uid unmapped, so a rootfs owned by uid 1000 is
    unwritable to the child even under `CAP_DAC_OVERRIDE`. The observed failure is
    `open ./rootfs/dev/urandom failed: Permission denied`.

    The rootfs is root-owned in the image because it is `COPY`ed rather than bind-mounted, so
    the container has to run as root to match. Setting `user:` to anything else produces
    `Permission denied` from inside a chroot, which reads like a namespace problem and sends
    the reader to `security_opt` instead.

    The trade is stated rather than hidden: root inside an unprivileged container is not
    `privileged`, and the capability set is still Docker's default. It is the price of the
    rootfs launcher, and the alternative is not a `user:` line but a two-container
    topology.
    """
    for compose_path in (COMPOSE,):
        service = _service(_compose(compose_path))
        assert "user" not in service, (
            f"{compose_path.name} sets `user:`, which breaks the launcher's single-uid map: "
            f"the rootfs is root-owned in the image and would become unmapped and unwritable"
        )


def test_every_rewritten_config_value_is_asserted_after_it_is_rewritten():
    """Each `sed` has to be followed by a `grep` that pins its result, in the same layer.

    A `sed` whose pattern stops matching -- upstream renames the key, or reformats the
    default -- is not a build failure. It is a shipped config that is quietly wrong, and for
    `dirPathFormat` the quiet failure is the worst one in this repository: the client writes
    to `/app/AppleMusicDecrypt/downloads/...` while the hub scans `/library/a`, so every
    track downloads again, forever, with nothing red anywhere.

    The pairing is checked generally rather than per key, so the next override added to the
    image inherits the same protection instead of needing to be remembered.
    """
    making = [
        args
        for verb, args in _dockerfile_instructions()
        if verb == "RUN" and "cp config.example.toml config.toml" in args
    ]
    assert making, "no instruction builds the vendor config"
    instruction = making[0]
    rewritten = set(re.findall(r"s\|\^(\w+) = \.\*\|", instruction))
    # The keys the RUN pins by name: `grep -qx 'key = ...'` or `grep -qx "key = ..."`, with or
    # without an interpolated value on the right.
    asserted = set(re.findall(r"grep -qx ['\"](\w+) = ", instruction))
    missing = rewritten - asserted
    assert rewritten, f"no config value is rewritten in: {instruction[:200]}"
    assert not missing, (
        f"{sorted(missing)} are rewritten by sed with nothing asserting the result. A sed "
        f"whose pattern stops matching ships a wrong config silently. The rewritten keys are "
        f"{sorted(rewritten)}; the asserted ones {sorted(asserted)}."
    )
    # And the keys the gate then re-reads through tomllib, so a value that parses but is
    # wrong in a way the grep would not catch is still covered.
    assert "tomllib" in instruction, (
        "the RUN should re-read the config it just wrote, rather than trusting four greps"
    )


def test_both_security_opts_are_present_and_no_capability_is_added():
    """The measured answer, not a preference.

        seccomp only                -> mount proc failed: EPERM   FAILS
        systempaths only            -> unshare: EPERM             FAILS
        seccomp + systempaths       -> works
        seccomp + cap_add SYS_ADMIN -> mount proc failed: EPERM   FAILS

    The control that matters is the last one: it is a *visibility* judgement
    (`mount_too_revealing()`), not a privilege one, which is why `cap_add` cannot fix it and
    why adding one would only increase the surface.
    """
    service = _service(_compose(COMPOSE))
    opts = service["security_opt"]
    assert "seccomp:unconfined" in opts, f"the launcher cannot unshare without it: {opts}"
    assert "systempaths=unconfined" in opts, (
        f"systempaths=unconfined is not optional: Docker over-mounts 12 paths under /proc and "
        f"the kernel refuses the launcher's own procfs mount. {opts}"
    )
    assert "cap_add" not in service, (
        "cap_add was proven useless against this failure and increases the attack surface "
        "for nothing"
    )
    assert service.get("privileged") is not True, "the launcher needs no privileged container"
    assert "network_mode" not in service, (
        "the launcher does not unshare a network namespace, so `host` would put its bind on "
        "the host's loopback -- where the operator's own wrapper QEMU already listens, and "
        "where a failed bind makes the payload signal itself in a way that reads like an "
        "external kill"
    )


def test_only_8080_is_published_and_the_wrapper_never_is():
    """The wrapper's 12340 is an unauthenticated HTTP API serving decrypted audio.

    Publishing it would be a data leak rather than a feature, so this is worth a test rather
    than a comment: a `ports:` line is one line, and the one line that matters here is the
    one nobody thinks about when they are adding a port for debugging.

    The *container-side* port is the invariant, not the host-side one: `AMD_HTTP_PORT` may
    move the left-hand number so an operator can free 8080, and that is fine. What must not
    move is the target, because 12340 being a target is the leak.
    """
    service = _service(_compose(COMPOSE))
    entries = [str(entry) for entry in service["ports"]]
    assert entries, "the hub has to be reachable"
    targets = []
    for entry in entries:
        _, target = _port_split(entry)
        targets.append(target)
    assert targets == ["8080"], (
        f"exactly one port is published and it maps to 8080; found {list(zip(entries, targets, strict=True))}"
    )
    assert not any("12340" in entry for entry in entries), (
        f"the wrapper's port must never be published: {entries}"
    )
    assert _env_pairs()["AMD_WRAPPER_HOST"] == "127.0.0.1", (
        "the wrapper binds container loopback; that is the first half of not publishing it"
    )


def test_one_process_and_the_setting_is_not_reachable_from_compose():
    """`--workers 1` lives in `hub.app.main()`, and that is deliberate.

    Everything the app owns is on `app.state` -- the WebSocket broker, the job store, the leaf
    registry, the scheduler, the session generation. Two workers would be two of each: two
    schedulers racing `claim_next` (atomic, so no double rip, but two leaf registries, so a
    job could be claimed by a worker that never expanded it) and two session generations, so
    a logout on one would not revoke a session minted by the other.

    So the requirement is two-sided: `CMD` must go through `main()` rather than a bare
    `uvicorn` invocation that could grow a flag, and no compose file may offer a way to ask
    for more.
    """
    raw_cmd = next(args for verb, args in _dockerfile_instructions() if verb == "CMD")
    cmd = json.loads(raw_cmd)  # exec form, so the arguments are a real list
    assert cmd[-3:] == ["-m", "hub.app"] or cmd[-2:] == ["-m", "hub.app"], (
        f"CMD should be `python -m hub.app`, not {cmd!r}"
    )
    assert "hub.app" in cmd, (
        f"CMD should run the module's own entry point, not {cmd!r}"
    )
    assert "uvicorn" not in " ".join(cmd), (
        "`uvicorn hub.app:app` would take --workers from the command line, which is a knob "
        "this deployment must not have"
    )
    assert "workers=1" in (REPO_ROOT / "hub" / "hub" / "app.py").read_text(encoding="utf-8"), (
        "main() has to pass workers=1; that is where the single-process rule is enforced"
    )
    for compose_path in (COMPOSE,):
        raw = compose_path.read_text(encoding="utf-8")
        for forbidden in ("replicas", "--scale", "workers"):
            code = "\n".join(
                line
                for line in raw.splitlines()
                if forbidden in line and not line.strip().startswith("#")
            )
            assert not code.strip(), (
                f"{compose_path.name} sets {forbidden} outside a comment: the app is "
                f"single-process by construction and this would turn that into a suggestion"
            )


def test_the_healthcheck_budget_covers_the_wrapper_startup_timeout():
    """Uvicorn does not bind its socket until the lifespan's startup has finished.

    The lifespan's first step is `await supervisor.start()`, which polls the wrapper for up to
    `startup_timeout` seconds -- and a wrapper with no Apple account correctly never becomes
    "ready", because readiness requires non-empty `regions`. So on a fresh install the port
    is not open for roughly the whole budget. A `start_period` below it turns a correct boot
    into a reported-unhealthy container, and `restart: unless-stopped` turns that into a
    crash loop. The number is taken from the supervisor's own default rather than written
    here, because the two have to move together if either moves.
    """
    source = (REPO_ROOT / "hub" / "hub" / "wrapper_supervisor.py").read_text(encoding="utf-8")
    default = re.search(r"startup_timeout: float = ([\d.]+)", source)
    assert default, "WrapperSupervisor no longer has a startup_timeout default to read"
    budget = float(default.group(1))
    health = _service(_compose(COMPOSE))["healthcheck"]
    start_period = int(str(health["start_period"]).rstrip("s"))
    assert start_period >= budget, (
        f"start_period is {start_period}s but the lifespan blocks for up to {budget}s on a "
        f"host with no Apple account, so the container would be reported unhealthy while it "
        f"is booting correctly"
    )
    test_command = " ".join(health["test"])
    assert "/api/health" in test_command, test_command
    assert _service(_compose(COMPOSE)).get("restart"), (
        "the container is unattended, so it has to come back on its own"
    )


# --------------------------------------------------------------------------- #
# The library mounts
# --------------------------------------------------------------------------- #
def _merged_service(*paths: Path, name: str = "amd-hub") -> dict:
    """The service compose would produce from these files layered in order.

    Only the two merge behaviours these files actually use are modelled, and both are
    compose's documented ones: `environment` is a mapping, so the later file's keys win and
    the rest are inherited; `volumes` is a list keyed by mount target, so a later file
    *adds* a mount rather than replacing the set. Everything else is taken from the last
    file that mentions it.

    This exists because the alternative is reading the overlay on its own and concluding it
    dropped `security_opt` -- which is exactly what a first draft of this test did. An
    overlay has no `security_opt` of its own and must not need one: the whole point is that
    the base file's posture is what runs.
    """
    service: dict = {}
    for path in paths:
        layer = _service(_compose(path), name)
        environment = {**service.get("environment", {}), **layer.get("environment", {})}
        by_target: dict[str, str] = {}
        for volume in [*service.get("volumes", []), *layer.get("volumes", [])]:
            by_target[_volume_split(str(volume))[1]] = str(volume)
        service.update(layer)
        if environment:
            service["environment"] = environment
        service["volumes"] = list(by_target.values())
    return service


def test_the_library_is_one_required_root_and_not_two_optional_ones():
    """The library is one root, and which host directory backs it is the operator's.

    An overlay once made the external volume optional, with a second always-present root as
    the fallback. That arrangement is gone: the library is both the download target and the
    only scan root, so there is nothing to fall back to and a second root would be a tree the
    client never writes into -- a directory nothing ever scans.

    What replaces the "volume absent" case is not a degraded mode but a failed start, from
    `create_host_path: false` on the bind. That is asserted in
    `test_the_library_directory_is_required_and_nothing_defaults_to_this_host`, along with
    the absence of any host default, so this test stays about the root count and the overlay.
    """
    service = _service(_compose(COMPOSE))
    targets = {m["target"] for m in _mounts(service)}
    assert "/library" in targets
    assert service["environment"]["AMD_LIBRARY_ROOTS"] == "/library"
    # Nothing else is a library, so there is no second root to degrade to.
    assert not any(t.startswith("/library/") and t != "/library" for t in targets), targets

    # And the overlay, if someone still passes it, must not undo any of that.
    assert not (REPO_ROOT / "hub" / "deploy" / "compose.ntfs.yaml").exists(), (
        "the NTFS overlay is gone -- the drive is mounted by compose.yaml itself. A second "
        "file stating the same mount is a second thing to keep in step, and that is how the "
        "roots ended up scanned by nothing once already."
    )


def test_the_library_directory_is_required_and_nothing_defaults_to_this_host():
    """No host path may ship in the compose file, and no default may stand in for one.

    The test this replaces read the default straight out of the compose file and asked
    whether it was a symlink *on the machine running the suite*, skipping when it was not
    -- so on any other machine it inspected nothing and passed. That is a weaker test than
    this one for the same property, because it can pass without having checked anything.

    The property is now a property of the file rather than of the host: the operator's
    library path lives in `.env`, so the file must carry no host-specific value to fall
    back on. That is also what makes the deployment portable to a machine that has no
    external drive at all, where the directory named in `.env` is an ordinary one.
    """
    raw = COMPOSE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("#")
    )
    assert "/run/media/" not in code, (
        "the mount source is udev's automount target, which disappears on unmount and "
        "changes when the volume is reformatted, so a file naming it silently rots; the "
        "operator should point AMD_LIBRARY_HOST at a path they manage instead"
    )
    for personal in ("/home/m/", "HDD_Music"):
        assert personal not in code, (
            f"{personal!r} is a path from the machine this was written on; the operator's "
            "library directory belongs in .env, and a default here is wrong on every other "
            "machine"
        )
    binds = [m for m in _mounts(_service(_compose(COMPOSE))) if m["target"] == "/library"]
    assert binds, "the library must be mounted, whatever host directory it comes from"
    assert binds[0]["create_host_path"] is False, (
        "without create_host_path: false, a directory that does not exist yet becomes one "
        "Docker creates on the host, and an empty root reads as healthy"
    )
    assert ":?" in binds[0]["source"], (
        f"AMD_LIBRARY_HOST must be required (:?) rather than defaulted, or this file names one "
        f"machine's filesystem. got {binds[0]['source']!r}"
    )
    # The variable name is pinned, not just the `:?` syntax: `:?` alone would pass on any
    # other variable's required interpolation, which is a different requirement entirely.
    assert binds[0]["source"].startswith("${AMD_LIBRARY_HOST:?"), binds[0]["source"]


def test_the_container_side_library_path_is_the_same_value_everywhere_it_appears():
    """Tie the four places together, because one place is not all there is.

    `AMD_DOWNLOAD_ROOT` derives the image's two values -- the runtime `AMD_LIBRARY_ROOTS` and
    the baked `dirPathFormat`. But compose cannot interpolate a build arg, so compose's own
    three -- its `AMD_DOWNLOAD_ROOT`, the bind's `target:`, and its `AMD_LIBRARY_ROOTS` --
    are literals. Nothing enforced that they agree: a coordinated edit that renamed the mount
    in all three and left the ARG alone would pass every other test here, and the result is
    a client writing to a path the hub never scans, which is the one failure this deployment
    is most careful about and the one with no diagnostic.
    """
    service = _service(_compose(COMPOSE))
    bind = next(m for m in _mounts(service) if m["target"] == "/library")
    named = re.search(
        r"AMD_DOWNLOAD_ROOT:\s*(\S+)", COMPOSE.read_text(encoding="utf-8")
    )
    assert named, "compose must name the container-side library path once, as a build arg"
    root = Path(named.group(1))

    assert bind["target"] == root.as_posix(), (
        f"the bind mounts {bind['target']} but AMD_DOWNLOAD_ROOT is {root}; the two are the "
        f"same directory and a mismatch means the client writes where the scan does not read"
    )
    assert service["environment"]["AMD_LIBRARY_ROOTS"] == root.as_posix(), (
        f"the hub scans {service['environment']['AMD_LIBRARY_ROOTS']} but the client writes "
        f"to {root}"
    )
    # And the image must be told the same value, by derivation rather than by a second copy.
    assert "ARG AMD_DOWNLOAD_ROOT=" + root.as_posix() in _dockerfile(), (
        f"the Dockerfile's ARG default is not {root}, so a `docker build` without "
        f"--build-arg bakes a different write root than compose scans"
    )


def test_the_persistent_trees_are_outside_the_image_layer():
    """The Apple token database is not under `/data`, and neither tree may be ephemeral.

    The launcher chroots into its own rootfs *before* it resolves `--base-dir`
    (`wrapper-lite-rootless.c:131-142`), so `--base-dir /data/wrapper` means
    `<rootfs>/data/wrapper` inside the chroot -- a different namespace from the container's
    `/data`. Left in the image layer, the token database dies with the container and every
    `down`/`up` demands a full 2FA login again. (Observed: the launcher logged
    `mkdir base_dir_arg failed` and the fix removed it.)

    **This asserts the two properties, not the mechanism.** A first version asserted the
    volume *names*, which is asserting an implementation detail: the operator may mount a
    named volume or a bind mount, and both satisfy what this test is for. The properties
    are that the host side is declared (so the tree is not the image's own layer) and that
    the two trees have *different* owners -- one volume, mounted twice, is what a
    `docker volume rm` aimed at the wrong tree would destroy. `deploy/` being the host
    side for both is still two owners.
    """
    service = _service(_compose(COMPOSE))
    mounts = {m["target"]: m["source"] for m in _mounts(service)}
    targets = dict(mounts)  # container path -> host path

    hub_host = targets.get("/data")
    wrapper_host = targets.get("/opt/wrapper/rootfs/data")
    library_host = targets.get("/library")
    assert hub_host, f"hub.db must be mounted from the host, not left in the image layer: {mounts}"
    assert library_host, f"the library must be mounted from the host: {mounts}"
    assert wrapper_host, (
        f"the token database lives inside the launcher's chroot at <rootfs>/data, so it needs "
        f"its own host mount there or the Apple account is lost on every recreate. Mounts: {mounts}"
    )
    assert len({hub_host, wrapper_host, library_host}) == 3, (
        "two of the three trees resolve to the same host path, so one removal takes both. "
        "They hold unrelated state -- a queue, an Apple account and a music library -- and "
        "need separate owners."
    )
    # A short syntax entry with no host side is an anonymous volume, which compose creates
    # per-container and which therefore does not survive a recreate in a way the operator
    # can rely on naming.
    for host in mounts.values():
        if host.startswith(("./", "/")) or ":" in host:
            continue
        assert host in _compose(COMPOSE).get("volumes", {}), (
            f"{host!r} names a volume that is not declared, so compose treats it as a bind "
            f"mount of a relative path that does not exist"
        )


# --------------------------------------------------------------------------- #
# The build itself
# --------------------------------------------------------------------------- #
def test_the_image_builds_the_wrapper_instead_of_copying_prebuilt_artifacts():
    """A fresh clone can build this image, and that is now asserted rather than apologised for.

    This inverts `test_the_wrapper_artifacts_are_gitignored_so_a_fresh_clone_cannot_build`,
    which recorded the opposite as deliberate: `wrapper-lite-rootless` and
    `rootfs/system/bin/lite` are gitignored upstream build outputs, so the image COPYed a
    binary that no clone contained and no build could reproduce. The fix was to build them --
    `FetchContent` wanting the network was the stated reason for not doing so, and a Docker
    build has the network.

    Three things are asserted, and each one is a way this can silently regress:

      * the artifacts are *still* gitignored upstream. If someone commits a compiled Android
        binary to the wrapper repository this test fails, which is the outcome to notice -- not
        a 60 MB ELF in git, and not a silently unpinned build.
      * the NDK revision is named once, in upstream's own `NDK_VERSION` spelling, and the
        download URL is assembled from it rather than written beside it. The download is *not*
        checksummed -- that is now a stated property, with the reasoning on the ARG.
      * the two cmake flags are still there, because each is a build that fails later and more
        confusingly -- a configure-time policy error for one, and a payload that will not start
        for the other.

    What this cannot check is that the build *succeeds*; only a build does that, and
    `hub/deploy/build_gate.py` runs as the image's last `RUN` so a broken payload lands on the
    build rather than on the first start.
    """
    # This half needs a *wrapper clone*, and a clone is not part of this repository any more.
    # `wrapper/` was a submodule until the Dockerfile began cloning upstream itself, and it is
    # now gitignored at the root -- so a fresh clone of this repository has no `wrapper/`
    # directory and this assertion cannot be made from here at all. The property is upstream's
    # to keep rather than ours: we consume a pinned commit, and what matters is whether the
    # commit we pin ignores these paths, which is decided when the pin is moved.
    #
    # Note the `if` and not `pytest.skip`. Skipping would abandon the rest of this test -- the
    # builder stage, both cmake flags and the two post-build `test -x` assertions -- on exactly
    # the machine that most needs them, which is a fresh clone. The absent clone is a reason to
    # check less, not a reason to check nothing.
    wrapper_clone = REPO_ROOT / "wrapper"
    if (wrapper_clone / ".git").exists():
        for artifact in ("wrapper-lite-rootless", "rootfs/system/bin/lite"):
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", artifact],
                cwd=wrapper_clone, capture_output=True,
            )
            assert ignored.returncode == 0, (
                f"wrapper/{artifact} is no longer gitignored by the wrapper repository, so the "
                f"builder stage is building something git already carries -- and if that was "
                f"intentional, a compiled Android binary is about to be committed"
            )

    instructions = _dockerfile_instructions()

    # The launcher and the payload come from the builder stage, and the stage exists.
    assert any(
        verb == "FROM" and "AS wrapper-build" in args for verb, args in instructions
    ), "there is no builder stage, so the two COPYs below have nothing to copy from"
    stage_copies = _stage_copy_destinations()
    for path in ("wrapper-lite-rootless", "rootfs"):
        assert f"wrapper-build:/src/wrapper/{path}" in stage_copies, (
            f"{path} is not COPYed from the builder stage: {stage_copies}"
        )

    # Nothing in the build context is expected to already be built.
    leaked = {
        src: dst for src, dst in _copy_destinations().items()
        if dst == "/opt/wrapper" or dst.startswith("/opt/wrapper/")
    }
    assert not leaked, (
        f"{leaked} still come from the build context, so a clone without a host build cannot "
        f"produce them -- which is the failure this stage was added to remove"
    )

    # **Everything below is read from the instructions, not from the file.** `_dockerfile_
    # instructions()` drops comment lines, and that is the whole point: an earlier version of
    # this test grepped the raw text and passed on four mutations that removed the real flag,
    # the real checksum and the real assertion, because the recipe's *prose* above them still
    # spelled all of it out. A test that reads the comment cannot tell a build from a
    # description of a build.
    runs = " ".join(args for verb, args in instructions if verb == "RUN")
    args_text = " ".join(a for verb, a in instructions if verb == "ARG")

    # **The NDK revision, in upstream's spelling, and nothing beyond that.** This block used to
    # require an `ANDROID_NDK_SHA256` digest and a `sha256sum -c` that checked it, and that
    # requirement is deliberately gone. The digest it demanded was one nothing in this
    # repository could substantiate, and a check that insists on an undiagnosable pin is a
    # check that cannot fail informatively -- it passes on a plausible-looking constant and
    # fails on a correct build. What replaces it is the part that is actually checkable: the
    # revision is stated in one place, under the name upstream uses, and the URL is derived from
    # that same value rather than from a second literal free to drift away from it.
    #
    # The revision is not cosmetic. `-Wall -Werror` is on for both Debug and Release, so it
    # decides which clang compiles the payload, and a different clang is a different build.
    ndk = re.search(r"\bNDK_VERSION=(\d+)", args_text)
    assert ndk is not None, (
        f"no NDK_VERSION ARG, so nothing states which NDK this is and the payload's clang is "
        f"whatever the day served. Upstream's Dockerfile names it the same way, and the two "
        f"builds of one wrapper should stay comparable. The ARGs are: {args_text[:200]}"
    )
    assert ndk.group(1) == "23", (
        f"NDK_VERSION is {ndk.group(1)}, not 23. The wrapper's CMakeLists hardcodes the "
        f"toolchain directory as ./android-ndk-r23b/, and the `b` suffix exists only for some "
        f"releases, so this is not a value that can be raised: the configure step stops with a "
        f"message that never mentions it."
    )
    # One source of truth. The URL is assembled from the ARG, so the revision asserted above is
    # the revision fetched. A literal URL sitting beside the ARG would let the two disagree,
    # which is the same shape of bug as a config mounted at a path nothing reads: it looks
    # pinned and is not.
    assert "dl.google.com/android/repository/android-ndk-r${NDK_VERSION}b-linux.zip" in runs, (
        f"the download does not build its URL from NDK_VERSION, so the stated revision and the "
        f"fetched one can disagree. RUNs: {runs[:200]}"
    )

    # Both cmake flags, asserted individually because either alone is a build that fails later.
    assert "-DCMAKE_POLICY_VERSION_MINIMUM=3.5" in runs, (
        "without this, CMake >= 4 refuses cJSON's cmake_minimum_required(2.8.12) and the "
        "build fails at configure time"
    )
    # And with a real path, not an empty one: `-DDCURL_SHARED_LIB=` leaves find_library() to
    # search the host, which is the failure this flag exists to prevent.
    curl = re.search(r"-DDCURL_SHARED_LIB=(\S*)", runs)
    assert curl is not None and curl.group(1), (
        "DCURL_SHARED_LIB is not set to a path, so find_library() searches host paths and picks "
        "up a /usr/lib/libcurl.so; the Android payload then records the wrong SONAME and will "
        "not start -- a failure that looks like a library problem, not a build one"
    )
    # And the path has to point at the payload's own libcurl, not at a plausible-looking one.
    assert "rootfs/system/lib64/libcurl.so" in curl.group(1), (
        f"DCURL_SHARED_LIB points at {curl.group(1)!r}, not at the libcurl the rootfs ships"
    )

    # Both outputs are asserted after the build, because a target whose
    # RUNTIME_OUTPUT_DIRECTORY is a source subdirectory can configure and link and still write
    # nothing -- and a build that wrote nothing is a green build.
    assert "test -x ./rootfs/system/bin/lite" in runs, (
        "the builder stage does not check that it produced the payload"
    )
    assert "test -x ./wrapper-lite-rootless" in runs, (
        "the builder stage does not check that it produced the launcher, so a green build can "
        "still yield an image with a rootfs and no program to exec"
    )


def test_the_upstream_pins_are_full_hashes_and_not_abbreviations():
    """A pin that is a prefix is a pin until upstream adds an object that shares it.

    Both pins were 7 characters. An abbreviated SHA resolves only while it is unambiguous,
    so a pin written that way can silently stop being one when upstream creates an object
    that collides on the prefix -- and `git checkout` then refuses, at build time, on a
    machine that has nothing wrong with it. A full 40-character hash has a second property the
    abbreviation never had: it is **checkable**. `git rev-parse` in a clone of the same repo
    either produces this value or it does not, so a wrong pin is a one-command contradiction
    rather than something nobody can evaluate.

    The clone itself is what makes the length matter here. `wrapper/rootfs/` is 101 tracked
    `.so` files, so a re-pin genuinely changes the payload and is worth being able to argue
    about; and the builder stage does a full clone (see the comment on its RUN) precisely so
    that the pin does not have to be a branch tip to be reachable.
    """
    args_text = " ".join(a for verb, a in _dockerfile_instructions() if verb == "ARG")
    for arg in ("VENDOR_COMMIT", "WRAPPER_COMMIT"):
        pinned = re.search(rf"\b{arg}=([0-9a-f]*)", args_text)
        assert pinned is not None, f"no {arg} ARG, so the clone is not pinned at all"
        assert re.fullmatch(r"[0-9a-f]{40}", pinned.group(1)), (
            f"{arg} is {pinned.group(1)!r}, which is not a full 40-character hash. An "
            f"abbreviated SHA is a prefix that only resolves while nothing else shares it, so "
            f"a pin written that way can stop being a pin the moment upstream adds an object, "
            f"and it cannot be checked against a clone the way a full hash can."
        )


def test_the_ci_build_takes_its_args_from_compose_and_not_from_the_workflow():
    """The build has one definition, and CI is where a second one would do the most damage.

    `build.yml` points `docker/bake-action` at `compose.yaml`, so the `build.args` an
    operator's `docker compose up --build` uses are the ones CI publishes. The failure this
    guards against has a precise shape: somebody changes `NDK_VERSION` (or the download
    root, or the language) in compose.yaml, the workflow is not part of their diff because
    nothing reminds them it exists, and the published image quietly differs from the one
    everyone else builds -- **with both builds green**, because neither file is wrong on
    its own terms.

    That is not a hypothetical form of bug in this repository. It is the same one the clone
    ARGs turned out to have: four variables declared globally, used inside stages, never
    re-declared where they were read, and 33 tests green right up to the moment a build was
    actually attempted. Nothing in a static suite reads a *second copy* of a value unless
    something is written to look for it.

    Restating an argument here would look like redundancy and would really be an independent
    variable -- the same mistake as a config mounted at a path nothing reads.
    """
    assert CI_BUILD_WORKFLOW.exists(), (
        "there is no build workflow, so the image is published by hand or not at all, and "
        "the question of which compose.yaml produced a given tag has no answer"
    )
    workflow = CI_BUILD_WORKFLOW.read_text(encoding="utf-8")

    # **Comments stripped before any assertion reads the file, and in both directions.**
    #
    # This file already records the half of the lesson that goes one way: a test that
    # grepped raw text passed on four mutations that removed the real flag, the real
    # checksum and the real assertion, because the recipe's *prose* above them still
    # spelled all of it out -- "a test that reads the comment cannot tell a build from a
    # description of a build". This test is that bug mirrored. The header comment below
    # names NDK_VERSION, AMD_DOWNLOAD_ROOT and AMD_VENDOR_LANGUAGE precisely in order to
    # explain that this file must never *set* them, so a raw search failed a workflow that
    # obeys the rule it was written to enforce.
    #
    # Stripping comment lines makes prose neutral in both directions: it can neither
    # satisfy an assertion that a setting exists nor violate one that says it must not.
    active = "\n".join(
        line for line in workflow.splitlines() if not line.strip().startswith("#")
    )

    # The mechanism, not merely the presence of a build step: compose.yaml has to be the
    # file bake reads. `source: .` is paired with it deliberately -- without it bake would
    # take its definition from the remote repository instead of the checkout, which is
    # precisely wrong on the pull request that changes compose.yaml.
    assert "files: ./compose.yaml" in active, (
        "the build workflow does not point bake at compose.yaml, so its build arguments "
        "come from somewhere else -- and that somewhere else is now a second source of "
        "truth for NDK_VERSION, AMD_DOWNLOAD_ROOT and AMD_VENDOR_LANGUAGE"
    )
    assert "source: ." in active, (
        "bake has no `source: .`, so it takes its definition from the Git context (the "
        "remote repository) rather than the checked-out commit -- meaning a pull request "
        "that edits compose.yaml is built with the *old* compose.yaml and reports success"
    )

    # The other direction, and the one that catches the drift rather than its mechanism:
    # the three build args must not be written out here at all. Each name below is a value
    # an operator changes, so each is a value CI could contradict while staying green.
    #
    # `AMD_PASSWORD` and `AMD_LIBRARY_HOST` are absent from this list on purpose. They are
    # in the workflow's `env:` block as placeholders for compose's `${VAR:?}` interpolation
    # -- runtime-only values the build never reads -- and asserting "no AMD_ names at all"
    # would fail on a file that is correct.
    for arg in ("NDK_VERSION", "AMD_DOWNLOAD_ROOT", "AMD_VENDOR_LANGUAGE"):
        assert arg not in active, (
            f"the build workflow sets {arg}, which compose.yaml already states. Restating "
            f"it here means two places to change and one that will be missed; delete it "
            f"from the workflow and let `files: ./compose.yaml` carry the value."
        )
    assert "--build-arg" not in active, (
        "the build workflow passes --build-arg, so it is supplying build arguments itself "
        "instead of taking them from compose.yaml"
    )


def test_every_arg_a_stage_uses_is_declared_in_that_stage():
    """A global `ARG` is in scope for `FROM` lines only, so a stage must re-declare its own.

    **This is the test that would have caught the image not building at all.** `VENDOR_URL`,
    `VENDOR_COMMIT`, `WRAPPER_URL` and `WRAPPER_COMMIT` were all declared above the first
    `FROM` and used by clone steps inside two stages, with no re-declaration in either. The
    image had therefore never been built: `sh` reported `WRAPPER_URL: parameter not set` and
    the stage exited 2 -- *after* the 692 MB NDK had already been downloaded, so the failure
    arrives minutes in and looks like a network or a `git` problem.

    Every other test in this file was green the whole time, which is the point. Nothing else
    here reads a stage's variable scope, and a Docker build is not one of these tests
    (`acceptance_check.py` and a `docker compose up` are), so the gap between "the static
    contract holds" and "the image exists" had nothing watching it.
    """
    # Walk the raw file rather than `_dockerfile_instructions()`, which flattens stages and
    # drops the distinction this test *is* about.
    #
    # Docker's rule, stated exactly because getting it backwards produces a test that passes
    # on a broken file: a global `ARG` (above the first `FROM`) is in scope **for `FROM`
    # lines only**. Inside a stage it is invisible to every other instruction, and a stage
    # only sees the `ARG`s it declares itself -- where a bare `ARG NAME` picks up the global
    # *value*. So a stage starts with nothing declared, not with the globals.
    lines = _dockerfile().splitlines()
    stage: str | None = None
    declared: set[str] = set()  # this stage's own ARGs; empty at every FROM
    undeclared: list[str] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        verb, _, rest = line.partition(" ")
        if verb == "FROM":
            # `FROM image AS name`, case-insensitively, because the stage name is what a
            # reader needs in the failure message.
            parts = rest.split()
            stage = parts[-1] if len(parts) > 1 and parts[-2].upper() == "AS" else "<final>"
            declared = set()
            continue
        if verb == "ARG":
            declared.add(rest.split()[0].split("=")[0])
            continue
        # `$(nproc)` is a shell substitution, not a variable, and the pattern does not match
        # it: `$(` is not `$IDENT` and not `${IDENT}`.
        for name in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", rest):
            if name not in declared:
                undeclared.append(f"stage {stage}: `{verb}` uses ${name}, which it never declares")

    assert not undeclared, (
        "these instructions use a variable their stage never declares, so `sh` substitutes "
        "nothing and the build fails at that line: "
        + "; ".join(undeclared)
        + ". A global ARG (declared above the first FROM) is in scope for FROM lines only -- "
        "re-declare it inside the stage that uses it, bare, so it inherits the value."
    )


def test_every_copy_source_exists_in_the_repository():
    """A rename fails here rather than twenty minutes into a build.

    Cheap, and the build context is the workspace root, so every `COPY` source is a path in
    this repository that a refactor can move without touching the Dockerfile.

    `AppleMusicDecrypt` and `wrapper` are excluded: they are cloned at build time, not
    COPYed, so they do not need to exist in the repository.
    """
    cloned = {"AppleMusicDecrypt", "wrapper"}
    missing = [
        source
        for source in _copy_destinations()
        if source not in cloned and not (REPO_ROOT / source.rstrip("/")).exists()
    ]
    assert not missing, (
        f"the Dockerfile COPYs {missing}, which is not in the repository. The build context "
        f"is the workspace root, so these have to exist there."
    )


def test_the_context_excludes_what_must_never_ship():
    """69 GB of downloads, a host venv, and a real logged-in account.

    The last one is the interesting exclusion: `wrapper/rootfs/data/` holds the host's
    `accounts.sqlitedb`, `cookies.sqlitedb` and `token_cache.json` from a wrapper that was
    logged in by hand. Shipping it would put an Apple account into a published image layer,
    where `docker history` can read it and no amount of later scrubbing is reliable.

    `AppleMusicDecrypt/` and `wrapper/` are excluded as whole trees: they are cloned at
    build time, so the local copies (with their downloads/, .venv/, config.toml, build/,
    android-ndk-r23b/, rootfs/data/) must never enter the build context.
    """
    for required in (
        "AppleMusicDecrypt",
        "AppleMusicDecrypt/downloads",
        "AppleMusicDecrypt/.venv",
        "AppleMusicDecrypt/config.toml",
        "wrapper",
        "wrapper/rootfs/data",
        "wrapper/build",
        "wrapper/android-ndk-r23b",
        "hub/.venv",
        "hub/tests",
        ".env",
        # Nested, and the case the first version of this test could not see: a root-anchored
        # `*.db` excludes a database at the context root and nothing else, so
        # `hub/hub/web/worker-check.db` was in the image for the whole of task 10. It is
        # named here rather than described, because a description is what let it through.
        "hub/hub/web/worker-check.db",
        "hub/hub/deeply/nested/hub.db",
        "hub.db",
        "hub/x/y/.env",
    ):
        assert _excluded_from_context(required), (
            f"{required} is not excluded from the build context. 69 GB of downloads, a host "
            f"venv, a queue database and a real logged-in account all belong in an image "
            f"layer that `docker history` can read."
        )
    # ...and what must ship, or the build cannot work at all.
    for required in (
        "hub/hub",
        "hub/pyproject.toml",
        "hub/uv.lock",
        "hub/deploy/build_gate.py",
    ):
        assert not _excluded_from_context(required), (
            f"{required} is excluded from the build context but the image needs it"
        )


def test_the_build_gate_runs_in_the_image_and_the_harnesses_compile():
    """The gate is what makes "the image built" mean something.

    Two ways to build an image that is broken in a way no build log shows: the vendor
    derivation moved, and `config.toml` is not in the image. Both are asserted by
    `build_gate.py` through the code's own functions, and it has to be *wired in* -- a gate
    nobody runs is a comment.

    The other two files are checked for compiling rather than behaviour, because their
    behaviour is `docker compose up` plus `docker compose exec`, which is the acceptance
    run and not a unit test. A syntax error in either would otherwise surface as a
    traceback halfway through that run.
    """
    gate_runs = [
        args
        for verb, args in _dockerfile_instructions()
        if verb == "RUN" and "build_gate.py" in args
    ]
    assert gate_runs, "the build gate is not invoked by the Dockerfile"
    assert _copy_destinations()["hub/deploy/build_gate.py"] == "/app/build_gate.py", (
        "the gate has to be copied into the image to run in it"
    )
    # And it has to be the *last* RUN, so it sees the finished image rather than a half-built
    # one that happens to satisfy it.
    runs = [verb for verb, _ in _dockerfile_instructions() if verb == "RUN"]
    assert runs[-1] == "RUN" and "build_gate.py" in _dockerfile_instructions()[-1][1], (
        "the build gate must be the last instruction: it checks the layout the earlier "
        "instructions produced"
    )
    for path in (BUILD_GATE, ACCEPTANCE):
        assert path.is_file(), f"{path} is referenced by the deployment but does not exist"
        ast.parse(path.read_text(encoding="utf-8")), f"{path} does not parse"


def test_the_vendor_config_in_the_image_parses_and_says_what_the_gate_expects():
    """The image's config is *built*, not committed -- so its shape is asserted, not read.

    There is no `config.toml` in this repository to check: the Dockerfile copies upstream's
    `config.example.toml` and rewrites three lines in it. What can be checked is that the
    upstream file those lines come from still has the shape the seds assume, and that the
    result is a valid config with the required sections -- because a sed that stops matching
    ships a config that is wrong in exactly the way that fails silently.
    """
    example = REPO_ROOT / "AppleMusicDecrypt" / "config.example.toml"
    assert example.is_file(), (
        "the Dockerfile builds the vendor config from AppleMusicDecrypt/config.example.toml, "
        "so upstream has to ship it"
    )
    parsed = tomllib.loads(example.read_text(encoding="utf-8"))
    for section in ("region", "instance", "localInstance", "download", "metadata"):
        assert section in parsed, (
            f"config.example.toml has no [{section}]; the seam requires it and the image's "
            f"config would fail to load"
        )
    # The two values the image deliberately does NOT rewrite, because they are already right
    # for this topology and an image that "fixes" them is a second source of truth.
    assert parsed["localInstance"]["enable"] is False, (
        "upstream's example enables the local QEMU backend. The hub supervises the wrapper "
        "itself; leaving this on would make the client launch a second backend and overwrite "
        "[instance].url."
    )
    assert parsed["instance"]["url"] == "127.0.0.1:12340", (
        f"[instance].url is {parsed['instance']['url']!r}; the supervisor binds "
        f"127.0.0.1:12340 and the build gate compares the two"
    )
    # And the lines the seds rewrite have to be the lines, at the top level, unindented.
    for key in ("dirPathFormat", "playlistDirPathFormat", "language"):
        line = next(
            (
                ln
                for ln in example.read_text(encoding="utf-8").splitlines()
                if ln.startswith(f"{key} = ")
            ),
            None,
        )
        assert line is not None, f"config.example.toml no longer has a top-level `{key} = `"


def test_the_documented_skip_reason_format_is_the_one_the_code_emits():
    """Two format docs that the round-1 change invalidated, and a guard against the next one.

    `app.js` and `job_row.html` both documented `duplicate:<relpath>|<relpath>`, which stopped
    existing when `skip_reason` switched to `DuplicateHit.resolved`. Rendering is
    format-agnostic -- the templates split on `|`, emit one `<li>` per path, and escape -- so
    nothing broke and nothing failed, which is exactly why it went unnoticed.

    Rather than assert the *absence* of a stale string -- which is a test that breaks on a
    harmless rewording, and which my own first attempt did by quoting the old format literally
    while "fixing" it -- this asserts the positive twice: the code emits one specific string,
    and both docs say the paths are root-qualified, which is the part that changed.
    """
    from hub.app import _skip_reason
    from hub.dedup import DuplicateHit

    emitted = _skip_reason(
        DuplicateHit(matched=("artist/album",), resolved=("/library/a/artist/album",))
    )
    assert emitted == "duplicate:/library/a/artist/album", emitted

    for path in (
        REPO_ROOT / "hub/hub/web/static/app.js",
        REPO_ROOT / "hub/hub/web/templates/job_row.html",
    ):
        body = path.read_text(encoding="utf-8")
        assert "duplicate:" in body, f"{path.name} no longer documents the skip_reason format"
        assert "root-qualified" in body, (
            f"{path.name} does not say the paths are root-qualified, so a reader could still "
            f"assume the bare-relpath format the code stopped emitting"
        )
        # Two paths, `|`-joined: the shape both files claim, asserted as a shape rather than as
        # one literal, so a rewording that keeps the meaning does not fail.
        assert "|" in body and "relpath" in body, (
            f"{path.name} should say what joins the paths and what a path is built from"
        )


def _resolve_compose_interpolation(spec: str, env: dict[str, str]) -> str:
    """What compose actually sets, given a `.env`.

    `${VAR:-default}` takes the default only when `VAR` is **unset or empty**. Compose reads
    `.env` for interpolation, so an operator who sets the variable in `.env` gets their value
    and never the default. `_default_of` reads the default because reading the whole spec is
    confusing -- and that is the trap: the default is *right*, so a test on it passes while
    the deployed value is wrong.
    """
    def replace(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        return env.get(name) or default

    return re.sub(r"\$\{([A-Z_][A-Z0-9_]*):-([^}]*)\}", replace, spec)


def test_dotenv_cannot_move_the_scan_away_from_the_write_root():
    """The bug that cost 3,670 albums, stated as the invariant that prevents it.

    `AMD_LIBRARY_ROOTS` used to be `${AMD_LIBRARY_ROOTS:-/library/a,/library/b}` in the
    overlay, with `.env` setting it to `/library/a`. A default only applies when the variable
    is unset, and compose reads `.env` for interpolation -- so the default never applied, the
    drive was mounted, and nothing scanned it. Measured with `docker compose config` before
    the fix: the mount present, the variable `/library/a`.

    Both sides are now literals derived from one build argument, so the arrangement that broke
    is unrepresentable. This asserts the *shape* rather than the resolved value, because the
    point is that no `.env` can win: an interpolation would be the regression.
    """
    compose_code = COMPOSE.read_text(encoding="utf-8")
    for line in compose_code.splitlines():
        if "AMD_LIBRARY_ROOTS" in line and not line.strip().startswith("#"):
            assert "${" not in line, (
                f"AMD_LIBRARY_ROOTS is interpolated again ({line.strip()!r}), so an .env can "
                f"point the scan at a tree the client never writes into"
            )



def test_the_write_root_and_the_scan_roots_come_from_one_build_argument():
    """One place decides both, so they cannot disagree.

    `dirPathFormat` is baked into `<vendor>/config.toml` at build time and
    `AMD_LIBRARY_ROOTS` is read at runtime. Historically they were two independent
    literals, which is what let `.env` point the scan at a tree the client never
    writes to -- and the result is a silent re-download of everything, with no error
    anywhere. Deriving both from `AMD_DOWNLOAD_ROOT` makes that unrepresentable.
    """
    dockerfile = _dockerfile()

    assert "ARG AMD_DOWNLOAD_ROOT=" in dockerfile, (
        "the download root is a build argument, so compose states it once"
    )
    # The runtime roots and the baked paths come from the same argument.
    assert "ENV AMD_LIBRARY_ROOTS=${AMD_DOWNLOAD_ROOT}" in dockerfile, (
        "AMD_LIBRARY_ROOTS must be derived from AMD_DOWNLOAD_ROOT; setting it separately is "
        "what let .env move the scan away from the tree the client writes into"
    )
    # The literals that would be *baked*, not the parameterised `grep -qx` lines that
    # mention the keys. An earlier version of this assertion looked for `"dirPathFormat`
    # and so failed on the very lines that prove the value is derived.
    for literal in ("/library/a/{album_artist}", "/library/a/playlists"):
        assert literal not in dockerfile, (
            f"{literal} is still a hardcoded literal, so the build would bake a path the "
            f"runtime roots do not include"
        )
    assert '{AMD_DOWNLOAD_ROOT}/{album_artist}/{album}' in dockerfile
    assert '{AMD_DOWNLOAD_ROOT}/playlists/{playlistName}' in dockerfile


def test_the_library_is_one_root_and_nothing_else():
    """One root. `/library/a` is no longer mounted, so it cannot be scanned by accident."""
    targets = {m["target"] for m in _mounts(_service(_compose(COMPOSE)))}
    assert "/library/a" not in targets, (
        "the client's own downloads tree is no longer a library root; a mount that is "
        "present but not scanned is a tree the hub silently ignores"
    )
    env = _service(_compose(COMPOSE))["environment"]
    assert env["AMD_LIBRARY_ROOTS"] == "/library", env["AMD_LIBRARY_ROOTS"]
