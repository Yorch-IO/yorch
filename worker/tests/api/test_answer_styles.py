"""Editing how an organisation words its answers, per effort level.

The routes are small; what is worth asserting is the shape of the two states a
level can be in. A level nobody edited must come back with the built-in default
*and* say it is not custom, because those two facts drive different things on
screen — the text goes in the box, the flag decides whether "restore the
default" would do anything.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from brainworker.answering.effort import BUDGETS, EFFORT_LEVELS, MAX_STYLE_CHARS
from brainworker.api import main
from brainworker.graph.schema import LEGACY_TENANT_ID


class _FakeCatalog:
    """Only what these two routes call. A real `Catalog` needs Postgres."""

    def __init__(self, saved: dict[str, str] | None = None) -> None:
        self.saved = dict(saved or {})
        self.writes: list[tuple[str, str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def answer_styles(self, *, tenant_id: str) -> dict[str, str]:
        self.tenant_read_as = tenant_id
        return dict(self.saved)

    def set_answer_style(self, effort: str, body: str, *, tenant_id: str) -> None:
        self.writes.append((effort, body, tenant_id))


@pytest.fixture
def catalog(monkeypatch):
    fake = _FakeCatalog()
    monkeypatch.setattr(main, "Catalog", lambda *_, **__: fake)
    return fake


@pytest.fixture
def client():
    return TestClient(main.app)


def test_every_level_comes_back_whether_or_not_it_was_edited(client, catalog):
    """The screen draws one box per level, so a level missing from the response
    would be a level nobody could ever give a style to."""
    body = client.get("/answer-styles").json()
    assert [lvl["effort"] for lvl in body["levels"]] == list(EFFORT_LEVELS)


def test_an_unedited_level_carries_the_built_in_default_and_says_so(client, catalog):
    body = client.get("/answer-styles").json()
    for lvl in body["levels"]:
        assert lvl["body"] == BUDGETS[lvl["effort"]].style
        assert lvl["custom"] is False


def test_an_edited_level_carries_its_own_wording_and_the_default_beside_it(
    client, catalog
):
    """`default_body` travels too, so "restore" needs no second request — and so
    the screen can show what it would restore *to* before doing it."""
    catalog.saved = {"thorough": "en verso"}
    levels = {l["effort"]: l for l in client.get("/answer-styles").json()["levels"]}

    assert levels["thorough"]["body"] == "en verso"
    assert levels["thorough"]["custom"] is True
    assert levels["thorough"]["default_body"] == BUDGETS["thorough"].style
    assert levels["brief"]["custom"] is False


def test_the_free_plane_reads_as_the_organisation_it_serves(client, catalog):
    client.get("/answer-styles")
    assert catalog.tenant_read_as == LEGACY_TENANT_ID


def test_saving_a_style_writes_it_for_that_organisation(client, catalog):
    r = client.put("/answer-styles/thorough", json={"body": "  en verso  "})
    assert r.status_code == 200
    assert r.json() == {"effort": "thorough", "custom": True}
    assert catalog.writes == [("thorough", "  en verso  ", LEGACY_TENANT_ID)]


def test_an_empty_body_is_how_a_default_is_restored(client, catalog):
    """The same request as "save an empty box", deliberately: two gestures that
    mean one thing must not be able to drift into two states."""
    r = client.put("/answer-styles/brief", json={"body": "   "})
    assert r.status_code == 200
    assert r.json()["custom"] is False
    assert catalog.writes == [("brief", "   ", LEGACY_TENANT_ID)]


def test_a_level_the_table_does_not_name_is_refused(client, catalog):
    """Not merely tidy. Reads are keyed by the level being asked for, so a row
    written under a name no level has is invisible — it would read as a save
    that silently did nothing."""
    r = client.put("/answer-styles/exhaustivo", json={"body": "x"})
    assert r.status_code == 404
    assert r.json()["detail"]["kind"] == "effort_not_found"
    assert catalog.writes == []


def test_a_style_longer_than_the_cap_is_refused(client, catalog):
    r = client.put("/answer-styles/brief", json={"body": "x" * (MAX_STYLE_CHARS + 1)})
    assert r.status_code == 422
    # A list, not an object with a kind: this is FastAPI's own validation shape,
    # which is what the paid plane reproduces for its 
    # failures. A hand-rolled kind would make the two planes answer one
    # oversized body two different ways.
    assert isinstance(r.json()["detail"], list)
    assert catalog.writes == []


def test_a_catalog_that_is_down_says_so_rather_than_inventing_defaults(
    client, monkeypatch
):
    """Unlike the answering path, which falls back to the default because a
    question must not fail. Here the whole point of the screen is to show what
    is *stored*, and defaults presented as the stored value would be a lie the
    user would then edit and save over."""
    def boom(*_, **__):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main, "Catalog", boom)
    r = client.get("/answer-styles")
    assert r.status_code == 503
    assert r.json()["detail"]["kind"] == "catalog_unreachable"
