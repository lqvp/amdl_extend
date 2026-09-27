import pytest

from hub.config import load_settings


def test_requires_password():
    with pytest.raises(RuntimeError, match="AMD_PASSWORD"):
        load_settings({"AMD_PASSWORD": ""})


def test_requires_password_when_the_variable_is_absent():
    # The realistic startup case: compose passes the variable through from .env, so a
    # missing .env entry arrives as an absent key rather than an empty string.
    with pytest.raises(RuntimeError, match="AMD_PASSWORD"):
        load_settings({})


def test_parses_multiple_library_roots():
    s = load_settings({"AMD_PASSWORD": "x", "AMD_LIBRARY_ROOTS": "/library/a,/library/b"})
    assert [p.as_posix() for p in s.library_roots] == ["/library/a", "/library/b"]


def test_defaults_match_the_spec():
    s = load_settings({"AMD_PASSWORD": "x", "AMD_LIBRARY_ROOTS": "/library"})
    assert s.bind == "0.0.0.0" and s.port == 8080
    assert s.wrapper_port == 12340 and s.wrapper_host == "127.0.0.1"
    assert s.dedup_artist_scope == "loose"


def test_requires_library_roots():
    # No default. The two roots this default was sized for no longer both exist, and the
    # surviving one points into a directory upstream gitignores, so a fresh clone would
    # scan nothing and report nothing -- the silent-empty-root failure the deployment
    # notes are most careful about.
    with pytest.raises(RuntimeError, match="AMD_LIBRARY_ROOTS"):
        load_settings({"AMD_PASSWORD": "x"})


def test_the_missing_library_roots_message_says_how_to_set_it():
    # A message that only names the variable leaves the operator to guess the format, and
    # the format is a comma-separated list of *container* paths -- not the host path they
    # put in LIBRARY_B. Both halves are asserted because the point of this change is that
    # a new user can get it right without reading the source.
    with pytest.raises(RuntimeError) as excinfo:
        load_settings({"AMD_PASSWORD": "x"})
    message = str(excinfo.value)
    assert "AMD_LIBRARY_ROOTS" in message
    assert "comma-separated" in message


def test_the_password_is_still_checked_before_the_library_roots():
    # Both required settings fail this way, so the order is a contract: a caller who has
    # set neither must be told about the one it is more likely to have meant. This is what
    # keeps the two existing password tests honest -- without it they would pass for the
    # wrong reason and stop testing the password.
    with pytest.raises(RuntimeError, match="AMD_PASSWORD"):
        load_settings({})


def test_reads_os_environ_when_no_mapping_is_given(monkeypatch):
    monkeypatch.setenv("AMD_PASSWORD", "from-environ")
    monkeypatch.setenv("AMD_PORT", "9999")
    monkeypatch.setenv("AMD_LIBRARY_ROOTS", "/library")
    s = load_settings()
    assert s.password == "from-environ"
    assert s.port == 9999


def test_generates_a_session_secret_when_unset():
    # A missing secret must not silently become a constant, and regenerating costs only
    # the user's own logged-in sessions on restart.
    first = load_settings({"AMD_PASSWORD": "x", "AMD_LIBRARY_ROOTS": "/library"})
    second = load_settings({"AMD_PASSWORD": "x", "AMD_LIBRARY_ROOTS": "/library"})
    assert len(first.session_secret) == 32
    assert first.session_secret != second.session_secret


def test_rejects_a_short_session_secret():
    # Otherwise an operator "sets" a secret and the cookie becomes trivially forgeable.
    with pytest.raises(RuntimeError, match="AMD_SESSION_SECRET"):
        load_settings(
            {"AMD_PASSWORD": "x", "AMD_LIBRARY_ROOTS": "/library", "AMD_SESSION_SECRET": "short"}
        )


def test_rips_four_tracks_at_once_by_default():
    # The number is the claim the queue page's speed rests on, and it is also what an
    # operator reads to decide whether to change it, so it is stated here rather than only
    # in compose.yaml. Four is measured, not guessed: 6.1 s of a 9.8 s ALAC track is the
    # wrapper's metadata round-trip, so four overlapping rips recover nearly all of what a
    # serial queue spent waiting.
    s = load_settings({"AMD_PASSWORD": "x", "AMD_LIBRARY_ROOTS": "/library"})
    assert s.rip_concurrency == 4


def test_the_rip_concurrency_is_configurable():
    assert load_settings(
        {"AMD_PASSWORD": "x", "AMD_RIP_CONCURRENCY": "2", "AMD_LIBRARY_ROOTS": "/library"}
    ).rip_concurrency == 2
    # 1 is not a mistake to be corrected but a legitimate request for the old
    # one-at-a-time behaviour, and it is how anyone would measure what concurrency bought.
    assert load_settings(
        {"AMD_PASSWORD": "x", "AMD_RIP_CONCURRENCY": "1", "AMD_LIBRARY_ROOTS": "/library"}
    ).rip_concurrency == 1


def test_rejects_a_rip_concurrency_that_is_not_a_number():
    with pytest.raises(RuntimeError, match="AMD_RIP_CONCURRENCY"):
        load_settings(
            {"AMD_PASSWORD": "x", "AMD_LIBRARY_ROOTS": "/library", "AMD_RIP_CONCURRENCY": "four"}
        )


def test_rejects_a_rip_concurrency_below_one():
    # Zero and negatives are not "slow", they are broken: a gather over an empty or negative
    # list claims nothing while the loop believes it did work, and the rows it marked
    # `running` would never be marked anything again.
    for value in ("0", "-1"):
        with pytest.raises(RuntimeError, match="AMD_RIP_CONCURRENCY"):
            load_settings(
                {"AMD_PASSWORD": "x", "AMD_RIP_CONCURRENCY": value, "AMD_LIBRARY_ROOTS": "/library"}
            )


def test_rejects_an_unknown_artist_scope():
    # A typo must not silently fall back to "loose", which is the false-skip mode §7.4
    # is written to warn about.
    with pytest.raises(RuntimeError, match="AMD_DEDUP_ARTIST_SCOPE"):
        load_settings(
            {
                "AMD_PASSWORD": "x",
                "AMD_DEDUP_ARTIST_SCOPE": "loose-ish",
                "AMD_LIBRARY_ROOTS": "/library",
            }
        )


def test_rejects_a_non_numeric_port():
    with pytest.raises(RuntimeError, match="AMD_PORT"):
        load_settings({"AMD_PASSWORD": "x", "AMD_PORT": "http", "AMD_LIBRARY_ROOTS": "/library"})


def test_rejects_a_port_outside_the_bindable_range():
    # An out-of-range port cannot be bound, so it must fail here by name rather than as an
    # OSError from the server after everything else has been wired up.
    with pytest.raises(RuntimeError, match="AMD_PORT"):
        load_settings({"AMD_PASSWORD": "x", "AMD_PORT": "-1", "AMD_LIBRARY_ROOTS": "/library"})
    with pytest.raises(RuntimeError, match="AMD_WRAPPER_PORT"):
        load_settings(
            {"AMD_PASSWORD": "x", "AMD_WRAPPER_PORT": "99999", "AMD_LIBRARY_ROOTS": "/library"}
        )
