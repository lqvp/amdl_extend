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
    s = load_settings({"AMD_PASSWORD": "x"})
    assert s.bind == "0.0.0.0" and s.port == 8080
    assert s.wrapper_port == 12340 and s.wrapper_host == "127.0.0.1"
    assert s.dedup_artist_scope == "loose"


def test_default_library_roots_are_the_two_spec_libraries():
    # §7.2 names the two libraries that actually exist on this host. compose.yaml
    # overrides them with its own mount points, so this default only decides local
    # development behaviour -- where these two are the right answer. Pinned by value, not
    # by count: asserting len()==2 and is_absolute() let "/library/a" and "/library/b"
    # through, which are the container mount points and are wrong for every local run.
    s = load_settings({"AMD_PASSWORD": "x"})
    assert [p.as_posix() for p in s.library_roots] == [
        "/home/m/apple-dl_extend/AppleMusicDecrypt/downloads",
        "/run/media/m/1A5E05A75E057D2F/Music",
    ]


def test_reads_os_environ_when_no_mapping_is_given(monkeypatch):
    monkeypatch.setenv("AMD_PASSWORD", "from-environ")
    monkeypatch.setenv("AMD_PORT", "9999")
    s = load_settings()
    assert s.password == "from-environ"
    assert s.port == 9999


def test_generates_a_session_secret_when_unset():
    # A missing secret must not silently become a constant, and regenerating costs only
    # the user's own logged-in sessions on restart.
    first = load_settings({"AMD_PASSWORD": "x"})
    second = load_settings({"AMD_PASSWORD": "x"})
    assert len(first.session_secret) == 32
    assert first.session_secret != second.session_secret


def test_rejects_a_short_session_secret():
    # Otherwise an operator "sets" a secret and the cookie becomes trivially forgeable.
    with pytest.raises(RuntimeError, match="AMD_SESSION_SECRET"):
        load_settings({"AMD_PASSWORD": "x", "AMD_SESSION_SECRET": "short"})


def test_rips_four_tracks_at_once_by_default():
    # The number is the claim the queue page's speed rests on, and it is also what an
    # operator reads to decide whether to change it, so it is stated here rather than only
    # in compose.yaml. Four is measured, not guessed: 6.1 s of a 9.8 s ALAC track is the
    # wrapper's metadata round-trip, so four overlapping rips recover nearly all of what a
    # serial queue spent waiting.
    assert load_settings({"AMD_PASSWORD": "x"}).rip_concurrency == 4


def test_the_rip_concurrency_is_configurable():
    assert load_settings({"AMD_PASSWORD": "x", "AMD_RIP_CONCURRENCY": "2"}).rip_concurrency == 2
    # 1 is not a mistake to be corrected but a legitimate request for the old
    # one-at-a-time behaviour, and it is how anyone would measure what concurrency bought.
    assert load_settings({"AMD_PASSWORD": "x", "AMD_RIP_CONCURRENCY": "1"}).rip_concurrency == 1


def test_rejects_a_rip_concurrency_that_is_not_a_number():
    with pytest.raises(RuntimeError, match="AMD_RIP_CONCURRENCY"):
        load_settings({"AMD_PASSWORD": "x", "AMD_RIP_CONCURRENCY": "four"})


def test_rejects_a_rip_concurrency_below_one():
    # Zero and negatives are not "slow", they are broken: a gather over an empty or negative
    # list claims nothing while the loop believes it did work, and the rows it marked
    # `running` would never be marked anything again.
    for value in ("0", "-1"):
        with pytest.raises(RuntimeError, match="AMD_RIP_CONCURRENCY"):
            load_settings({"AMD_PASSWORD": "x", "AMD_RIP_CONCURRENCY": value})


def test_rejects_an_unknown_artist_scope():
    # A typo must not silently fall back to "loose", which is the false-skip mode §7.4
    # is written to warn about.
    with pytest.raises(RuntimeError, match="AMD_DEDUP_ARTIST_SCOPE"):
        load_settings({"AMD_PASSWORD": "x", "AMD_DEDUP_ARTIST_SCOPE": "loose-ish"})


def test_rejects_a_non_numeric_port():
    with pytest.raises(RuntimeError, match="AMD_PORT"):
        load_settings({"AMD_PASSWORD": "x", "AMD_PORT": "http"})


def test_rejects_a_port_outside_the_bindable_range():
    # An out-of-range port cannot be bound, so it must fail here by name rather than as an
    # OSError from the server after everything else has been wired up.
    with pytest.raises(RuntimeError, match="AMD_PORT"):
        load_settings({"AMD_PASSWORD": "x", "AMD_PORT": "-1"})
    with pytest.raises(RuntimeError, match="AMD_WRAPPER_PORT"):
        load_settings({"AMD_PASSWORD": "x", "AMD_WRAPPER_PORT": "99999"})
