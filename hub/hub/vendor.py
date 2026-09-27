"""The place that knows where `AppleMusicDecrypt/` is, and the only route into it.

**Why this module exists and why it is one function long.** `AppleMusicDecrypt/` is a
separate upstream clone, meant to become a git submodule and to be upgraded from
upstream, so the hub is not allowed to be coupled to its layout: an upstream rename
should break one file here rather than the codebase. `tests/test_ripper_host.py` walks
the AST of every module under `hub/hub/` and `hub/spike/` and fails on any `import
src.*`, on `importlib`/`runpy`, on `__import__`, and on any reference to `sys.path`.

That leaves two files allowed to reach upstream, and this is the one that is not a
lifecycle class. It was previously `ripper_host.parse_apple_music_url`, which was the
wrong home: `ripper_host` is a process-lifecycle class that holds the working directory
and runs subprocesses, and it does not own pure URL parsing. `vendor.py` is honestly
"the place that knows about the vendor tree", it is the narrowest thing that can hold
the one parser the hub needs, and it makes the boundary's two exemptions legible by what
each is *for* rather than by accident of history.

**It is a pass-through on purpose.** `AppleMusicURL.parse_url` owns the `?i=` share-link
rule, the storefront extraction and the host regex, and a second copy of any of that in
the hub is a second source of truth for what a URL *means* -- which drifts silently and
is exactly the failure a user cannot debug. So nothing here re-derives it, and the
return type is declared rather than left as the module's only unannotated contract.

**It reaches exactly one upstream name.** The vendor root and the `sys.path` insert stay
in `ripper_host`, which is where they are already tested
(`test_vendor_root_is_derived_from_this_file_not_the_working_directory`) and where the
seam's own error type lives. Reaching into them rather than re-deriving the path is
deliberate: two derivations of "where is AppleMusicDecrypt" is the same class of
duplication as two parsers, and the seam's can be monkeypatched by tests, which this
call path honours because it goes through the module rather than binding the function.

Not a pass-through, though, in one respect: it puts the vendor root on `sys.path`
itself. `AppleMusicDecrypt/` is a top-level package directory named `src`, so nothing
resolves it until that directory is on the path -- and a caller that only wants to parse
a URL should not have to start a `RipperHost`, chdir the process and register six
creart creators to do it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hub import ripper_host

if TYPE_CHECKING:  # pragma: no cover - the annotation is what this is for
    from src.url import AppleMusicURL


def parse_apple_music_url(url: str) -> AppleMusicURL | None:
    """Upstream's ``AppleMusicURL.parse_url``, or ``None`` if this is not an Apple URL.

    Returns one of `Song` / `Album` / `Playlist` / `Artist` / `MusicVideo`
    (`src/url.py`), carrying the four fields the resolver needs: `url` (the string as
    given), `storefront`, `type` and `id`.

    ``None`` for anything that is not an Apple Music link is **upstream's own answer,
    not a failure** -- `parse_url` returns `None` for a non-matching host, and whether an
    unrecognised URL is worth spending a redirect on is the caller's decision, not this
    function's. `ResolveError` is raised by `hub.resolver` for a URL that ends up
    unrecognised; this function never raises about the URL itself.

    `AppleMusicURL.parse_url` checks the host with a regex and does **not** check the
    scheme, so a `http://` or `file://` string reaches here and is answered with `None`.
    That is the right division of labour: this function is a faithful pass-through and
    makes no policy of its own, and the scheme allowlist is `hub.resolver`'s to enforce
    before it gets here.

    Raises `ripper_host.RipperHostError` if the `AppleMusicDecrypt/` checkout is missing.
    That is an operator problem -- a clone that is not there -- and it is reported as
    one, with the expected path and the command that fixes it, rather than as a
    `ResolveError` about the URL, which would send the reader to debug the wrong thing.
    """
    # Through the module, not `from hub.ripper_host import _require_vendor_root`: the
    # seam's tests monkeypatch `ripper_host._VENDOR_ROOT`, and a module attribute lookup
    # at call time is what lets them.
    ripper_host._ensure_vendor_on_path(ripper_host._require_vendor_root())

    from src.url import AppleMusicURL

    return AppleMusicURL.parse_url(url)
