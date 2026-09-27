"""`hub/vendor.py`: the pass-through to upstream's URL parser, tested on its own terms.

**Why this file exists at all, given `tests/test_resolver.py` covers the parser
indirectly.** Every other use of `parse_apple_music_url` in the suite is downstream of
it: `test_resolver.py` calls it through a `pytest.skip` fixture, and a CI without the
`AppleMusicDecrypt/` checkout would take all of those tests with it and still report
green. A module whose only job is a pass-through cannot be allowed to have *no* test of
its own, so the four checks that do not need the checkout are here, unconditionally, and
only the two that genuinely need it are marked.

**The return type is part of what is being pinned.** The function had no annotation at
all when it was first written, in a package that is otherwise annotated throughout, and
`AppleMusicURL | None` is exactly the union a caller has to handle. `test_it_declares
_the_union_a_caller_has_to_handle` reads the annotation rather than trusting it.

**`None` is not an error, and that is upstream's answer, not this module's.** The test
below says so for a URL that is not Apple's, and the pass-through test says the return
value is upstream's verbatim, which is what makes "a `ResolveError` is the resolver's
decision, not the parser's" a checked statement rather than a claim in a docstring.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hub import resolver, ripper_host, vendor
from hub.vendor import parse_apple_music_url

VENDOR_ROOT = REPO_ROOT = Path(vendor.__file__).resolve().parents[2]
VENDOR_SRC = REPO_ROOT / "AppleMusicDecrypt" / "src"

needs_vendor = pytest.mark.skipif(
    not (VENDOR_SRC / "url.py").is_file(),
    reason="AppleMusicDecrypt checkout not present; the vendor tree has to exist "
           "before anything can be asked of it",
)


# --------------------------------------------------------------------------- #
# These three need nothing but this repository
# --------------------------------------------------------------------------- #
def test_the_module_imports_and_exposes_exactly_one_function():
    """Importable with no checkout, and thin.

    The thinness is the design: this module reaches exactly one upstream name, and a
    second one would be a second thing for an upstream rename to break. Read from the
    source because that is the only place the claim can be false.
    """
    assert callable(parse_apple_music_url)

    tree = ast.parse(Path(vendor.__file__).read_text(encoding="utf-8"))
    upstream = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.split(".")[0] in {"src", "AppleMusicDecrypt"}
    }
    assert upstream == {"src.url"}, upstream
    assert [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
            for alias in node.names] == [], "vendor.py has no top-level imports of its own"


def test_it_declares_the_union_a_caller_has_to_handle():
    """`AppleMusicURL | None`, as a string annotation, not absent.

    Checked as text because the name is only importable under `TYPE_CHECKING`, so
    `typing.get_type_hints` would raise rather than answer in a runtime environment.
    `from __future__ import annotations` means the annotation is never evaluated, which
    is what lets the honest name be written without the import costing anything.
    """
    source = Path(vendor.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "parse_apple_music_url"
    )

    assert isinstance(function.returns, ast.BinOp), "no return annotation at all"
    assert ast.unparse(function.returns) == "AppleMusicURL | None", ast.unparse(function.returns)
    assert "from __future__ import annotations" in source
    # And it is a TYPE_CHECKING import, so the annotation costs no import at runtime.
    assert "if TYPE_CHECKING" in source


def test_a_missing_checkout_is_reported_with_the_path_and_the_fix(monkeypatch, tmp_path):
    """An operator problem, reported as one -- and this runs with no checkout present.

    `RipperHostError` rather than anything about the URL: a missing clone and a bad link
    are different problems with different fixes, and telling a reader that a perfectly
    good URL "is not an Apple Music URL" sends them to debug the wrong thing.
    """
    monkeypatch.setattr(ripper_host, "_VENDOR_ROOT", tmp_path / "AppleMusicDecrypt")

    with pytest.raises(ripper_host.RipperHostError) as excinfo:
        parse_apple_music_url("https://music.apple.com/jp/album/x/1")

    message = str(excinfo.value)
    assert "AppleMusicDecrypt" in message
    assert str(tmp_path) in message
    # And it is a RuntimeError, so one `except RuntimeError` covers it alongside
    # `ResolveError` and every other way this package reports a bad request.
    assert isinstance(excinfo.value, RuntimeError)


def test_the_resolver_reaches_the_parser_through_this_exact_function():
    """The wiring, checked without the checkout -- the part a rename would break.

    If `hub/resolver.py` ever re-implements the parse, or routes through somewhere
    else, `resolver.parse_apple_music_url` stops being this object and this fails. That
    is the assertion standing in for the whole of `tests/test_resolver.py`, which cannot
    run here: it needs `src.models` and upstream's own parser, and removing that need
    would mean duplicating both, which is the one thing the design forbids.
    """
    assert resolver.parse_apple_music_url is parse_apple_music_url
    assert vendor.parse_apple_music_url is parse_apple_music_url


# --------------------------------------------------------------------------- #
# These two genuinely need the checkout
# --------------------------------------------------------------------------- #
@needs_vendor
def test_it_returns_none_for_a_url_that_is_not_apple_music():
    """A non-Apple URL, and specifically a non-Apple *host* over https.

    `None` is upstream's own answer and is not a failure. The resolver, not this
    function, decides that an unrecognised URL is a `ResolveError` -- and decides
    whether it is worth a redirect first.
    """
    assert parse_apple_music_url("https://example.com/album/1") is None
    assert parse_apple_music_url("https://itunes.apple.com/jp/album/x/1") is None
    # The scheme is *not* this function's business: a http:// Apple URL is `None` here,
    # and the resolver's allowlist refuses it before it ever gets here.
    assert parse_apple_music_url("http://music.apple.com/jp/album/x/1") is None


@needs_vendor
def test_it_is_a_pass_through_to_upstreams_parser():
    """The return value is upstream's, verbatim -- the same object, not a copy.

    The share link is the interesting case: an *album* URL carrying `?i=<songId>`,
    which upstream turns into a `Song` with the id from the query. If this function
    ever normalised, re-wrapped or re-derived the URL, that would be the second source
    of truth it exists to avoid.
    """
    from src.url import AppleMusicURL, URLType

    share = "https://music.apple.com/jp/album/nameless-name-single/1688539265?i=1688539274"
    parsed = parse_apple_music_url(share)

    assert parsed is not None
    assert type(parsed) is type(AppleMusicURL.parse_url(share))
    assert (parsed.type, parsed.id, parsed.storefront) == (URLType.Song, "1688539274", "jp")
    # `url` is the string as given, which is what `Leaf.url` is later asserted against.
    assert parsed.url == share
