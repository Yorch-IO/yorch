"""The tenant `activate_version` is given, and the fact that it must be given.

Not "activation works" — `tests/graph` and `tests/catalog` cover the two writes
against live stores. The claim here is narrower and is the one that broke: the
organisation this promotes for has to arrive from the caller, because this
function both *checks* it (the catalog lookup is the authorization predicate, so
a wrong one is a refusal) and *writes* it (`proj.activate` builds a `VersionNode`
from it, so a wrong one lands in another organisation's graph).

`VersionNode.tenant_id` already made this exact mistake as a field default, and
CLAUDE.md records what it cost: a paying organisation's document, sections,
chunks and citations went into the legacy tenant's graph under an unsalted id,
and **nothing failed**. A required parameter cannot stop a caller passing the
wrong tenant; it stops one passing no tenant and never noticing.
"""

from __future__ import annotations

import inspect

import pytest

from brainworker import activation
from brainworker.activities import activating


def test_the_tenant_cannot_be_left_out():
    """It defaulted to the legacy organisation until 2026-08-31, which made the
    free plane's answer right by accident and every other plane's wrong."""
    tenant = inspect.signature(activation.activate_version).parameters["tenant_id"]
    assert tenant.default is inspect.Parameter.empty, (
        "a defaulted tenant is the field-default bug with a different name"
    )
    assert tenant.kind is inspect.Parameter.KEYWORD_ONLY, (
        "keyword-only, so it cannot be filled by position from a caller that "
        "meant the library or the version"
    )
    with pytest.raises(TypeError):
        activation.activate_version("lib_teologia", "ver_0b71")


async def test_the_activity_hands_the_module_the_tenant_it_was_given(monkeypatch):
    """The path the paid plane takes. It is the only path a non-legacy
    organisation has, so a tenant dropped here is unreachable activation for
    everybody who is paying."""
    seen: dict = {}

    def fake_activate(library_id, version_id, *, tenant_id):
        seen.update(library=library_id, version=version_id, tenant=tenant_id)
        return activation.Activation(version_id=version_id, documents=["doc_1"])

    monkeypatch.setattr(activation, "activate_version", fake_activate)
    result = await activating.promote_version(
        "lib_teologia", "ver_0b71d21eeb3228f54437d9cf", "tnt_f489b4a62220158ef6790c07"
    )

    assert seen == {
        "library": "lib_teologia",
        "version": "ver_0b71d21eeb3228f54437d9cf",
        "tenant": "tnt_f489b4a62220158ef6790c07",
    }
    assert result == {
        "version_id": "ver_0b71d21eeb3228f54437d9cf",
        "documents": ["doc_1"],
    }


async def test_a_refusal_keeps_its_kind_across_the_temporal_boundary(monkeypatch):
    """`kind` is what `app/src/lib/api.ts` keys its guidance on; flattening it
    into a message costs the UI its ability to offer a fix."""
    from temporalio.exceptions import ApplicationError

    def refuse(library_id, version_id, *, tenant_id):
        raise activation.ActivationError("no existe", kind="version_not_found")

    monkeypatch.setattr(activation, "activate_version", refuse)
    with pytest.raises(ApplicationError) as caught:
        await activating.promote_version("lib_a", "ver_x", "tnt_other")

    assert caught.value.details[0] == "version_not_found"
    assert caught.value.non_retryable, (
        "a version that is not this organisation's will not become so on a retry"
    )
