"""A title and an author for a document that has neither.

`document.author` is a column nothing in this product has ever written, and
`document.title` is the picked file's stem — so a library of 74 books renders
as `01_RetoDeDios_INT-S` and every EPUB built from one would carry that on its
cover. One bounded call over the opening pages fixes both, once per document
ever.

Modelled on :mod:`brainworker.chat.title`, which is the smallest one-call use in
this codebase, and it borrows the rule that matters: **`None` is an answer**.
A title page that names no author must come back as no author rather than as a
guess, because the value is written into the catalog and the catalog is what a
person then edits — a plausible invention is harder to notice and correct than
a blank.

Pure except for the provider. Nothing here reads the catalog or a file; the
activity decides whether to call this at all.
"""

from __future__ import annotations

import json
import logging

from .pipeline import Spend
from .providers import Provider, VertexAdapter

log = logging.getLogger(__name__)

STAGE = "epub-metadata"

#: How much of the document the model reads. A title page, a half-title and a
#: copyright page fit comfortably; a whole book would cost a hundred times as
#: much to answer the same question no better. The estimate prices exactly this
#: number, so the two must not drift.
METADATA_CHARS = 6000

SYSTEM = """Lees el comienzo de un documento y dices cómo se titula y quién lo \
escribió.

REGLAS:
1. Copia el título tal como aparece. No lo traduzcas, no lo abrevies, no le \
pongas mayúsculas que no tiene.
2. El autor es la persona o la institución que firma la obra. No es el editor, \
ni la imprenta, ni el traductor, ni quien escribe el prólogo.
3. Si el fragmento no dice quién es el autor, devuelve null. No lo deduzcas del \
tema, del estilo ni de a quién cita.
4. Si el fragmento no tiene un título propio — porque empieza a mitad del texto \
— devuelve null en el título también.
5. Nunca inventes. Un campo vacío es una respuesta correcta; uno verosímil y \
falso no lo es."""

SCHEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": ["string", "null"]},
        "autor": {"type": ["string", "null"]},
    },
    "required": ["titulo", "autor"],
}


def _clean(value: object) -> str | None:
    """A field the model returned, or None for everything that is not one.

    The literal strings matter: a model asked for `null` in a JSON envelope
    sometimes writes `"null"`, and a title of `"null"` would be written into the
    catalog and rendered on a cover. The same for the Spanish forms a Spanish
    prompt invites.
    """
    text = " ".join(str(value or "").split()).strip(" \"'.")
    if not text or text.lower() in {"null", "none", "n/a", "desconocido", "sin autor"}:
        return None
    return text


def read_metadata(
    provider: Provider, text: str
) -> tuple[str | None, str | None, Spend | None]:
    """``(title, author, spend)`` — any of the three may be ``None``.

    A failure returns no title and no author *and still reports the spend*: the
    call was made and the tokens were billed whether or not the envelope parsed,
    and a stage that spends and reports nothing is how the ledger came to hold
    $0 for every question ever asked.
    """
    adapter = VertexAdapter(provider)
    excerpt = text[:METADATA_CHARS]
    try:
        raw = adapter.generate_json(
            json.dumps({"comienzo": excerpt}, ensure_ascii=False),
            system=SYSTEM,
            schema=SCHEMA,
            stage=STAGE,
        )
    except Exception as e:  # noqa: BLE001 — a book with no metadata is still a book
        log.warning("could not read the document's metadata: %s", e)
        return None, None, _spend(provider, adapter)
    return _clean(raw.get("titulo")), _clean(raw.get("autor")), _spend(
        provider, adapter
    )


def _spend(provider: Provider, adapter: VertexAdapter) -> Spend:
    from .activities.ingest import price_for

    usage = adapter.usage
    return Spend(
        stage=STAGE,
        model=provider.settings.model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        usd=price_for(provider.settings.model, usage.input_tokens, usage.output_tokens),
    )
