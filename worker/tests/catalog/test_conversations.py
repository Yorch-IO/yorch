"""Conversations against a real Postgres, in a disposable schema.

The properties worth asserting here are the ones the SQL is carrying rather than
the Python: the ownership predicate inside `open_turn`'s insert, the tenant
derived rather than passed, and the resume point the relay exists to provide.
"""

from __future__ import annotations

import pytest

from brainworker.catalog import Catalog, CatalogError
from brainworker.catalog import migrations as m
from brainworker.graph.schema import LEGACY_TENANT_ID

OTHER = "tnt_" + "b" * 24


@pytest.fixture
def catalog(database_url: str):
    m.apply_migrations(database_url)
    with Catalog(database_url, min_size=1, max_size=2) as c:
        with c._conn() as conn:  # noqa: SLF001 - seeding a second organisation
            conn.execute(
                "INSERT INTO tenant (id, slug, name) VALUES (%s, %s, %s) "
                "ON CONFLICT (id) DO NOTHING",
                (OTHER, "otra", "Otra"),
            )
        c.ensure_library("lib_1", "Biblioteca", tenant_id=LEGACY_TENANT_ID)
        yield c


def start(c: Catalog, cid="cnv_1", tenant=LEGACY_TENANT_ID) -> str:
    c.start_conversation(cid, tenant_id=tenant, library_id="lib_1", title="Primera")
    return cid


# -- the record -------------------------------------------------------------


def test_a_new_conversation_is_listed_with_no_turns(catalog: Catalog):
    start(catalog)
    rows = catalog.conversations(tenant_id=LEGACY_TENANT_ID)
    assert [(r.id, r.title, r.turns, r.title_generated) for r in rows] == [
        ("cnv_1", "Primera", 0, False)
    ]


def test_a_conversation_is_invisible_to_another_organisation(catalog: Catalog):
    start(catalog)
    assert catalog.conversations(tenant_id=OTHER) == []
    assert catalog.conversation("cnv_1", tenant_id=OTHER) is None
    assert catalog.conversation("cnv_1", tenant_id=LEGACY_TENANT_ID) is not None


def test_the_list_is_ordered_by_use_not_by_creation(catalog: Catalog):
    """A conversation somebody came back to belongs at the top."""
    start(catalog, "cnv_old")
    start(catalog, "cnv_new")
    catalog.open_turn("cnv_old", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="standard")
    catalog.settle_turn("cnv_old", 1, state="answered", answer="sí")
    assert [r.id for r in catalog.conversations(tenant_id=LEGACY_TENANT_ID)] == [
        "cnv_old", "cnv_new"
    ]


def test_naming_a_conversation_marks_it_named(catalog: Catalog):
    start(catalog)
    catalog.set_conversation_title("cnv_1", "La divinidad de Cristo", tenant_id=LEGACY_TENANT_ID)
    row = catalog.conversation("cnv_1", tenant_id=LEGACY_TENANT_ID)
    assert row.title == "La divinidad de Cristo" and row.title_generated is True


def test_another_organisation_cannot_rename_it(catalog: Catalog):
    start(catalog)
    catalog.set_conversation_title("cnv_1", "secuestrada", tenant_id=OTHER)
    assert catalog.conversation("cnv_1", tenant_id=LEGACY_TENANT_ID).title == "Primera"


def test_deleting_reports_whether_it_was_theirs(catalog: Catalog):
    start(catalog)
    assert catalog.delete_conversation("cnv_1", tenant_id=OTHER) is False
    assert catalog.delete_conversation("cnv_1", tenant_id=LEGACY_TENANT_ID) is True
    assert catalog.conversation("cnv_1", tenant_id=LEGACY_TENANT_ID) is None


# -- turns ------------------------------------------------------------------


def test_a_turn_is_visible_while_it_is_still_running(catalog: Catalog):
    """Written before it is answered, so a reader who reloads mid-answer does
    not see a conversation that has forgotten what they just said."""
    start(catalog)
    seq = catalog.open_turn(
        "cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿Quién fue?", effort="thorough"
    )
    assert seq == 1
    (turn,) = catalog.turns("cnv_1", tenant_id=LEGACY_TENANT_ID)
    assert turn.state == "running" and turn.question == "¿Quién fue?"
    assert turn.effort == "thorough" and turn.answered_at is None


def test_turn_numbers_are_claimed_in_order(catalog: Catalog):
    start(catalog)
    seqs = [
        catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question=f"q{i}", effort="brief")
        for i in range(3)
    ]
    assert seqs == [1, 2, 3]


def test_another_organisation_cannot_open_a_turn(catalog: Catalog):
    """The ownership predicate lives inside the insert. An id is not
    authorization — the free plane's `/reindex` is the recorded counter-example.
    """
    start(catalog)
    assert catalog.open_turn("cnv_1", tenant_id=OTHER, question="¿?", effort="brief") is None
    assert catalog.turns("cnv_1", tenant_id=LEGACY_TENANT_ID) == []


def test_a_turn_on_a_conversation_that_does_not_exist_writes_nothing(catalog: Catalog):
    assert catalog.open_turn("cnv_ausente", tenant_id=LEGACY_TENANT_ID,
                             question="¿?", effort="brief") is None


def test_a_turns_tenant_is_derived_and_cannot_disagree(catalog: Catalog):
    start(catalog, "cnv_o", tenant=OTHER)
    catalog.open_turn("cnv_o", tenant_id=OTHER, question="¿?", effort="brief")
    with catalog._conn() as conn:  # noqa: SLF001
        rows = conn.execute(
            "SELECT tenant_id FROM conversation_turn WHERE conversation_id = 'cnv_o'"
        ).fetchall()
    assert [r[0] for r in rows] == [OTHER]


def test_settling_records_the_outcome_and_what_was_searched(catalog: Catalog):
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿y su muerte?",
                      effort="standard")
    catalog.settle_turn(
        "cnv_1", 1,
        state="answered",
        searched="¿Qué dice el corpus sobre la muerte de Jesucristo?",
        answer="Los fragmentos dicen…",
        style_effort="brief",
        citations=[{"chunk_id": "chk_" + "0" * 24, "locator": "Cap 1 · [1:2]"}],
        cited_evidence=[{"chunk_id": "chk_" + "0" * 24, "text": "…"}],
    )
    (turn,) = catalog.turns("cnv_1", tenant_id=LEGACY_TENANT_ID)
    assert turn.state == "answered"
    assert turn.searched.startswith("¿Qué dice el corpus")
    assert turn.question == "¿y su muerte?", "the person's own words are kept"
    assert turn.style_effort == "brief"
    assert turn.citations[0]["locator"] == "Cap 1 · [1:2]"
    assert turn.answered_at is not None


def test_a_failed_turn_keeps_its_error(catalog: Catalog):
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    catalog.settle_turn("cnv_1", 1, state="failed",
                        error={"kind": "provider_quota", "message": "sin cuota"})
    (turn,) = catalog.turns("cnv_1", tenant_id=LEGACY_TENANT_ID)
    assert turn.state == "failed" and turn.error["kind"] == "provider_quota"


def test_the_transcript_reads_forwards_even_when_only_the_tail_is_asked_for(
    catalog: Catalog,
):
    """Reseeding a dormant session needs the *last* n turns, and the rewrite
    reads them oldest first. Getting that backwards hands it the opening of a
    long conversation instead of the part the pronoun refers to."""
    start(catalog)
    for i in range(5):
        catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question=f"q{i}", effort="brief")
    tail = catalog.turns("cnv_1", tenant_id=LEGACY_TENANT_ID, limit=2)
    assert [t.question for t in tail] == ["q3", "q4"]


def test_another_organisation_reads_no_turns(catalog: Catalog):
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    assert catalog.turns("cnv_1", tenant_id=OTHER) == []


# -- the relay --------------------------------------------------------------


def test_deltas_come_back_in_order_after_the_resume_point(catalog: Catalog):
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    catalog.publish("cnv_1", 1, [(1, "token", "La ", None), (2, "token", "gente ", None), (3, "token", "es feliz", None)])
    rows = catalog.relay("cnv_1", 1, tenant_id=LEGACY_TENANT_ID)
    assert [(r["chunk_seq"], r["text"]) for r in rows] == [
        (1, "La "), (2, "gente "), (3, "es feliz")
    ]
    later = catalog.relay("cnv_1", 1, tenant_id=LEGACY_TENANT_ID, since=2)
    assert [(r["chunk_seq"], r["text"]) for r in later] == [(3, "es feliz")]


def test_a_reconnecting_reader_sees_no_organisation_but_its_own(catalog: Catalog):
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    catalog.publish("cnv_1", 1, [(1, "token", "secreto", None)])
    assert catalog.relay("cnv_1", 1, tenant_id=OTHER) == []


def test_republishing_a_chunk_is_harmless(catalog: Catalog):
    """A retried flush must not duplicate what the reader has already seen."""
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    catalog.publish("cnv_1", 1, [(1, "token", "una", None)])
    catalog.publish("cnv_1", 1, [(1, "token", "una", None), (2, "token", "dos", None)])
    rows = catalog.relay("cnv_1", 1, tenant_id=LEGACY_TENANT_ID)
    assert [(r["chunk_seq"], r["text"]) for r in rows] == [(1, "una"), (2, "dos")]


def test_clearing_the_relay_leaves_the_answer(catalog: Catalog):
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    catalog.publish("cnv_1", 1, [(1, "token", "borrador", None)])
    catalog.settle_turn("cnv_1", 1, state="answered", answer="definitiva")
    catalog.clear_deltas("cnv_1", 1)
    assert catalog.relay("cnv_1", 1, tenant_id=LEGACY_TENANT_ID) == []
    assert catalog.turns("cnv_1", tenant_id=LEGACY_TENANT_ID)[0].answer == "definitiva"


def test_deleting_a_conversation_takes_its_turns_and_deltas_with_it(catalog: Catalog):
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    catalog.publish("cnv_1", 1, [(1, "token", "x", None)])
    catalog.delete_conversation("cnv_1", tenant_id=LEGACY_TENANT_ID)
    with catalog._conn() as conn:  # noqa: SLF001
        for table in ("conversation_turn", "conversation_delta"):
            n = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            assert n == 0, f"{table} kept rows for a deleted conversation"


def test_a_provisional_title_never_overwrites_a_generated_one(catalog: Catalog):
    """The two writers race on a fast first turn.

    The model names the conversation while the route's provisional write — the
    first question, truncated — is still in flight. Without the guard the good
    title is replaced by the truncation, and `title_generated` says it was named.
    """
    start(catalog)
    catalog.set_conversation_title(
        "cnv_1", "La divinidad de Cristo", tenant_id=LEGACY_TENANT_ID
    )
    catalog.set_conversation_title(
        "cnv_1", "¿Quién fue Jesucri…", tenant_id=LEGACY_TENANT_ID, generated=False
    )
    row = catalog.conversation("cnv_1", tenant_id=LEGACY_TENANT_ID)
    assert row.title == "La divinidad de Cristo"
    assert row.title_generated is True


def test_a_provisional_title_lands_when_there_is_no_generated_one(catalog: Catalog):
    start(catalog)
    catalog.set_conversation_title(
        "cnv_1", "¿Quién fue Jesucristo?", tenant_id=LEGACY_TENANT_ID, generated=False
    )
    row = catalog.conversation("cnv_1", tenant_id=LEGACY_TENANT_ID)
    assert row.title == "¿Quién fue Jesucristo?"
    assert row.title_generated is False, "a truncation must not stop the model naming it"


def test_stages_and_prose_share_one_ordered_sequence(catalog: Catalog):
    """Which is why the kind is a column rather than a second table: a stage row
    placed in `chunk_seq` order interleaves with the prose for free, and no
    reader has to merge two sources by timestamp — two flushes inside one
    millisecond being exactly what the sequence exists to disambiguate."""
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    catalog.publish("cnv_1", 1, [
        (1, "planning", "", None),
        (2, "evidence", "", {"chunks": 48, "dense": 27}),
        (3, "generating", "", None),
        (4, "token", "La gente ", None),
        (5, "token", "es feliz", None),
    ])
    rows = catalog.relay("cnv_1", 1, tenant_id=LEGACY_TENANT_ID)
    assert [r["kind"] for r in rows] == [
        "planning", "evidence", "generating", "token", "token"
    ]
    assert rows[1]["detail"] == {"chunks": 48, "dense": 27}
    # A stage carries no prose, so a client appending `text` without checking
    # the kind still gets exactly the answer.
    assert "".join(r["text"] for r in rows) == "La gente es feliz"


def test_a_reader_resuming_mid_turn_learns_the_stage_too(catalog: Catalog):
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    catalog.publish("cnv_1", 1, [
        (1, "planning", "", None),
        (2, "retrieving", "", None),
        (3, "token", "hola", None),
    ])
    rows = catalog.relay("cnv_1", 1, tenant_id=LEGACY_TENANT_ID, since=1)
    assert [r["kind"] for r in rows] == ["retrieving", "token"]


def test_prose_defaults_to_the_token_kind(catalog: Catalog):
    """The column defaults, so a row written by code that predates stages — or
    by the other plane before it is updated — is prose, which is what it was."""
    start(catalog)
    catalog.open_turn("cnv_1", tenant_id=LEGACY_TENANT_ID, question="¿?", effort="brief")
    with catalog._conn() as conn:  # noqa: SLF001 - exercising the column default
        conn.execute(
            "INSERT INTO conversation_delta "
            "(conversation_id, turn_seq, chunk_seq, tenant_id, text) "
            "VALUES ('cnv_1', 1, 9, %s, 'antiguo')",
            (LEGACY_TENANT_ID,),
        )
    (row,) = catalog.relay("cnv_1", 1, tenant_id=LEGACY_TENANT_ID)
    assert row["kind"] == "token" and row["text"] == "antiguo"
