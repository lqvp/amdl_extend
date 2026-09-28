"""The declared interface asserts itself.

`hub/state.py` replaced the dynamically-attached attributes of `app.state` with a
declaration. These tests pin the properties that made the declaration the point --
not what any field *does* (every module tests its own), but that the interface is
complete and sealed:

* **complete** -- the field roster is fixed. A field removed from the interface is
  noticed by this list, not by an `AttributeError` in production.
* **constructed** -- the wired singletons and the driving partials are every one built
  by `create_app`; there is no half-wired app.
* **sealed** -- `slots=True` makes an undeclared attribute an `AttributeError` on the
  spot, so the seam cannot grow quietly. The same discipline
  `tests/test_ripper_host.py` applies to imports, applied to the interface.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from hub.app import create_app
from hub.config import Settings
from hub.state import HubState

#: The whole interface. Adding or dropping a field is a change to the hub's internal
#: API, and this list is where the change has to be made visible.
DECLARED = frozenset(
    {
        "broker",
        "cached_problem",
        "current_job",
        "degraded_roots",
        "jobs",
        "jobs_counts",
        "leaves",
        "loop",
        "pending_2fa",
        "ripper",
        "ripper_config_path",
        "ripping_adam_ids",
        "run_one",
        "run_pool",
        "scheduler",
        "scheduler_loop",
        "session_generation",
        "sessions",
        "settings",
        "startup_error",
        "stopping",
        "supervisor",
        "templates",
    }
)

#: Built by `create_app` itself or bound beside it -- an app is not wired until every
#: one of them answers.
CONSTRUCTED = (
    "settings",
    "sessions",
    "broker",
    "jobs",
    "leaves",
    "ripper_config_path",
    "ripper",
    "supervisor",
    "templates",
    "jobs_counts",
    "run_one",
    "run_pool",
    "scheduler_loop",
)


def _settings(tmp_path: Path) -> Settings:
    """A Settings that opens nothing outside `tmp_path`. `load_settings` reads the
    environment; this is the same object the constructor takes."""
    return Settings(
        password="declared-interface",
        bind="127.0.0.1",
        port=0,
        library_roots=[tmp_path / "library"],
        wrapper_binary=tmp_path / "wrapper-lite-rootless",
        wrapper_base_dir=tmp_path / "base",
        wrapper_host="127.0.0.1",
        wrapper_port=0,
        dedup_artist_scope="loose",
        db_path=tmp_path / "hub.db",
        session_secret=b"0" * 32,
    )


@pytest.fixture
def app(tmp_path: Path):
    """A wired app that never starts anything: the injection points take placeholders
    because this tests the wiring, and no collaborator is asked a question."""
    return create_app(
        _settings(tmp_path), supervisor=object(), ripper=object(), autostart=False
    )


def test_the_interface_is_the_declared_field_list(app) -> None:
    """`app.state` is the declared type, and the declaration is the roster below."""
    assert isinstance(app.state, HubState)
    assert {f.name for f in fields(HubState)} == DECLARED


def test_the_seam_cannot_grow_an_undeclared_attribute(app) -> None:
    """Slots is the seal: a new attribute is an error here, not a silent new field.

    This is the interface half of the rule `test_ripper_host.py` enforces for imports
    -- the walk that inspects nothing must not report success, and an interface anyone
    can extend from the inside is not one.
    """
    with pytest.raises(AttributeError):
        app.state.jobz = None  # type: ignore[attr-defined]


def test_create_app_wires_every_field_it_owns(app) -> None:
    """No half-wired app: what `create_app` promises, it built."""
    for name in CONSTRUCTED:
        assert getattr(app.state, name) is not None, (
            f"{name} is create_app's to build; a None here is an app whose callers "
            f"will meet an unconfigured collaborator at request time."
        )


def test_runtime_fields_start_at_their_declared_empty(app) -> None:
    """The fields a caller owns start where the declaration says, before anything runs.

    `stopping` is a fresh event and `ripping_adam_ids` the empty in-flight table --
    the two whose shape a getattr fallback used to paper over.
    """
    assert app.state.loop is None
    assert app.state.scheduler is None
    assert app.state.current_job is None
    assert app.state.cached_problem is None
    assert app.state.startup_error is None
    assert app.state.pending_2fa is None
    assert app.state.degraded_roots == ()
    assert app.state.ripping_adam_ids == {}
    assert app.state.session_generation == 0
    assert not app.state.stopping.is_set()
