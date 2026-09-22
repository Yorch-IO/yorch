import { useCallback, useEffect, useState } from "react";
import { Locator } from "../Locator";
import { useTranslation } from "react-i18next";

import {
  api,
  errorGuidanceKey,
  errorMessage,
  type Claim,
  type ChunkContext,
  type ChunkOverrideRow,
  type Concept,
  type DocumentRow,
  type RelatedDocument,
  type ChunkRow,
  type Section,
} from "../lib/api";
import { useLibraries } from "../lib/libraries";
import { ChunkEditor } from "./ChunkEditor";
import { RetrievalProbe } from "./RetrievalProbe";

/** Marks a result a model proposed rather than one read off the document.
 *
 * `concepts_in_version`, `related_documents` and `claims_about_concept` all
 * declare `uses_semantic_edges` in the template registry, and that flag exists
 * precisely because an edge a model suggested and an edge derived from the
 * document's own table of contents have different standing as evidence. The
 * registry can only declare it; this is where a person actually sees it.
 */
function Proposed({ confidence }: { confidence?: number }) {
  const { t } = useTranslation();
  return (
    <span className="semantic" title={t("explore.proposedHelp")}>
      {t("explore.proposed")}
      {confidence !== undefined && (
        <span className="confidence"> {(confidence * 100).toFixed(0)}%</span>
      )}
    </span>
  );
}

export function ExploreScreen() {
  const { t } = useTranslation();

  const { selected: libraryId } = useLibraries();
  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [versionId, setVersionId] = useState<string | null>(null);

  const [sections, setSections] = useState<Section[]>([]);
  const [sectionId, setSectionId] = useState<string | null>(null);
  const [chunks, setChunks] = useState<ChunkRow[]>([]);
  const [context, setContext] = useState<ChunkContext | null>(null);
  //: What a person has changed about this version's chunks, by chunk index.
  //: Refetched after every edit rather than patched locally: the catalog is
  //: the source of truth and the screen is a view of it, which is the same
  //: ordering `removal.py` states about the stores.
  const [overrides, setOverrides] = useState<Map<number, ChunkOverrideRow>>(new Map());

  const [concepts, setConcepts] = useState<Concept[] | null>(null);
  const [conceptId, setConceptId] = useState<string | null>(null);
  const [claims, setClaims] = useState<Claim[]>([]);
  const [related, setRelated] = useState<RelatedDocument[]>([]);

  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const loadDocuments = useCallback(async () => {
    // See LibraryScreen: an empty id would be sent as `/libraries//documents`.
    if (!libraryId) {
      setDocuments([]);
      return;
    }
    setBusy(true);
    try {
      const library = await api.libraryDocuments(libraryId);
      // Only an active version has a projection to browse. A document still
      // waiting at the gate would give an empty outline that looks like the
      // "no headings" defect rather than like work in progress.
      setDocuments(library.documents.filter((d) => d.activeVersionId));
      setError(null);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  }, [libraryId]);

  useEffect(() => {
    void loadDocuments();
  }, [loadDocuments]);

  const openVersion = useCallback(async (id: string) => {
    setVersionId(id);
    setSectionId(null);
    setChunks([]);
    setContext(null);
    setConceptId(null);
    setClaims([]);
    setBusy(true);
    try {
      const [outline, semantic, neighbours, edits] = await Promise.all([
        api.exploreOutline(id),
        api.exploreConcepts(id),
        api.exploreRelated(id),
        api.exploreOverrides(id),
      ]);
      setOverrides(new Map(edits.overrides.map((o) => [o.chunkIndex, o])));
      setSections(outline.sections);
      setConcepts(semantic.concepts);
      setRelated(neighbours.documents);
      setError(null);
    } catch (e) {
      setError(e);
      setSections([]);
      setConcepts(null);
      setRelated([]);
    } finally {
      setBusy(false);
    }
  }, []);

  const openSection = useCallback(async (id: string) => {
    setSectionId(id);
    setContext(null);
    try {
      setChunks((await api.exploreSectionChunks(id)).chunks);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  const openChunk = useCallback(async (id: string) => {
    try {
      setContext(await api.exploreChunkContext(id));
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  const reloadEdits = useCallback(async () => {
    if (!versionId) return;
    try {
      const edits = await api.exploreOverrides(versionId);
      setOverrides(new Map(edits.overrides.map((o) => [o.chunkIndex, o])));
      if (sectionId) setChunks((await api.exploreSectionChunks(sectionId)).chunks);
    } catch (e) {
      setError(e);
    }
  }, [versionId, sectionId]);

  const openConcept = useCallback(async (id: string) => {
    setConceptId(id);
    try {
      setClaims((await api.exploreClaims(id)).claims);
      setError(null);
    } catch (e) {
      setError(e);
    }
  }, []);

  const guidance = error ? errorGuidanceKey(error) : undefined;

  return (
    <section className="screen explore">
      <h2>{t("explore.title")}</h2>
      <p className="intro">{t("explore.intro")}</p>

      <div className="actions">
        <button onClick={() => void loadDocuments()} disabled={busy}>
          {t("explore.reload")}
        </button>
      </div>

      {error !== null && (
        <div className="error">
          <strong>{t("explore.failed")}</strong>
          {guidance && <p>{t(guidance)}</p>}
          <p className="detail">{errorMessage(error)}</p>
        </div>
      )}

      <div className="explore-panes">
        <div className="pane pane-outline">
          <h3>{t("explore.documents")}</h3>
          {documents.length === 0 ? (
            <p className="notice">{t("explore.noDocuments")}</p>
          ) : (
            <ul className="documents-list">
              {documents.map((d) => (
                <li key={d.id}>
                  <button
                    className={
                      d.activeVersionId === versionId ? "link active" : "link"
                    }
                    onClick={() =>
                      void openVersion(d.activeVersionId as string)
                    }
                  >
                    {d.title}
                  </button>
                </li>
              ))}
            </ul>
          )}

          {versionId && (
            <>
              <h3>{t("explore.outline")}</h3>
              {sections.length === 0 ? (
                // Not an empty box. This is the surface where the missing table
                // of contents used to be discovered silently, so it says what
                // caused it and what fixes it.
                <p className="warn">{t("explore.noOutline")}</p>
              ) : (
                <ul className="outline">
                  {sections.map((s) => (
                    <li
                      key={s.id}
                      // Depth comes from the number of dotted segments; the path
                      // is a string and must never be parsed as a number.
                      style={{
                        paddingLeft: `${(s.path.split(".").length - 1) * 1.1}rem`,
                      }}
                    >
                      <button
                        className={s.id === sectionId ? "link active" : "link"}
                        onClick={() => void openSection(s.id)}
                      >
                        {s.title}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>

        <div className="pane pane-reading">
          <h3>{t("explore.chunks")}</h3>
          {!sectionId ? (
            <p className="notice">{t("explore.pickSection")}</p>
          ) : chunks.length === 0 ? (
            <p className="notice">{t("explore.noChunks")}</p>
          ) : (
            <ul className="chunks">
              {chunks.map((c) => (
                <li
                  key={c.id}
                  className={[
                    "chunk",
                    context?.chunkId === c.id ? "active" : "",
                    overrides.get(c.ordinal)?.disabled ? "hidden" : "",
                  ].filter(Boolean).join(" ")}
                >
                  <div className="chunk-head">
                    {/* Chunk kinds stay Spanish on the wire — they are stored in
                        Qdrant payloads and used in filters — so the UI maps them
                        to localised labels rather than renaming them. */}
                    <span className="kind">{t(`explore.kind.${c.kind}`)}</span>
                    {/* The page the chunk starts on, for a source that has
                        pages. Null for a `.txt`, a spreadsheet or a
                        transcript, and rendered as nothing rather than as
                        "p. 0" — a page a reader cannot find teaches them not
                        to trust the locator. */}
                    {c.page !== null && (
                      <span className="model">{t("explore.page", { page: c.page })}</span>
                    )}
                    <span className="model">
                      [{c.charStart}:{c.charEnd}]
                    </span>
                    <button
                      className="link"
                      onClick={() => void openChunk(c.id)}
                    >
                      {t("explore.showContext")}
                    </button>
                  </div>
                  <p className="chunk-text">{c.text}</p>
                  {/* Marked, because an edited chunk's `char_span` no longer
                      indexes any stream this product holds and a hidden one
                      answers no question — neither is visible from the text. */}
                  {overrides.get(c.ordinal)?.disabled && (
                    <p className="warn">{t("edit.isHidden")}</p>
                  )}
                  {(overrides.get(c.ordinal)?.text ?? null) !== null && (
                    <p className="notice">{t("edit.isEdited")}</p>
                  )}
                  {versionId && (
                    <ChunkEditor
                      chunk={c}
                      versionId={versionId}
                      override={overrides.get(c.ordinal)}
                      onDone={() => void reloadEdits()}
                    />
                  )}
                </li>
              ))}
            </ul>
          )}

          {context && (
            <div className="panel context">
              <h3>{t("explore.context")}</h3>
              {context.context?.beforeText ? (
                <p className="neighbour">{context.context.beforeText}</p>
              ) : (
                <p className="neighbour empty">{t("explore.noBefore")}</p>
              )}
              <p className="chunk-text current">{context.context?.text}</p>
              {context.context?.afterText ? (
                <p className="neighbour">{context.context.afterText}</p>
              ) : (
                <p className="neighbour empty">{t("explore.noAfter")}</p>
              )}
              {context.citation ? (
                <Locator text={context.citation.locator} className="locator" />
              ) : (
                // A citation the user cannot open is not a citation, so its
                // absence is stated rather than rendered as a blank line.
                <p className="warn">{t("explore.noLocator")}</p>
              )}
            </div>
          )}
        </div>

        <div className="pane pane-semantic">
          <h3>
            {t("explore.concepts")} <Proposed />
          </h3>
          {concepts === null ? (
            <p className="notice">{t("explore.pickDocument")}</p>
          ) : concepts.length === 0 ? (
            // "Semantics were never extracted" and "this document has no ideas
            // in it" are different statements, and extracting them is a paid
            // stage a user can decline. Rendering "0 concepts" would say the
            // second when the first is true.
            <p className="notice">{t("explore.noConcepts")}</p>
          ) : (
            <ul className="concepts">
              {concepts.map((c) => (
                <li
                  key={c.id}
                  className={c.id === conceptId ? "concept active" : "concept"}
                >
                  <button
                    className="link"
                    onClick={() => void openConcept(c.id)}
                  >
                    {c.name}
                  </button>
                  {c.conceptType && (
                    <span className="model">{c.conceptType}</span>
                  )}
                  <span className="mentions">
                    {t("explore.mentions", { count: c.mentions })}
                  </span>
                  <Proposed confidence={c.confidence} />
                </li>
              ))}
            </ul>
          )}

          {conceptId && (
            <>
              <h3>
                {t("explore.claims")} <Proposed />
              </h3>
              {claims.length === 0 ? (
                <p className="notice">{t("explore.noClaims")}</p>
              ) : (
                <ul className="claims">
                  {claims.map((c) => (
                    <li key={c.id}>
                      <p>{c.text}</p>
                      {/* The document's own words, when the extractor found the
                          model's quote inside the chunk it claimed to come from.
                          The claim above it is a paraphrase; this is the text. */}
                      {c.quote ? <q className="claim-quote">{c.quote}</q> : null}
                      {/* What the document *does* with the claim. Without it, a
                          doctrine a text is about to rebut reads exactly like one
                          it holds. */}
                      <span className={`claim-status is-${c.status}`}>
                        {t(`claim.status.${c.status}`)}
                      </span>
                      {/* Every claim names the chunk a person can check it
                          against. A relation nobody can verify is worse than no
                          relation, because it still looks like evidence. */}
                      <button
                        className="link"
                        onClick={() => void openChunk(c.sourceChunkId)}
                      >
                        {t("explore.checkSource")}
                      </button>
                      <Proposed confidence={c.confidence} />
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}

          {versionId && (
            <>
              <h3>
                {t("explore.related")} <Proposed />
              </h3>
              {related.length === 0 ? (
                <p className="notice">{t("explore.noRelated")}</p>
              ) : (
                <ul className="related">
                  {related.map((d) => (
                    <li key={d.id}>
                      <button
                        className="link"
                        onClick={() => void openVersion(d.id)}
                      >
                        {d.title ?? d.id}
                      </button>
                      <span className="mentions">
                        {t("explore.shared", { count: d.sharedConcepts })}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>

      {/* Below the panes rather than in one: a probe is about a chunk, and the
          chunk whose context is open above is the one it offers to place. */}
      {libraryId && (
        <RetrievalProbe
          libraryId={libraryId}
          versionId={versionId}
          chunkId={context?.chunkId ?? null}
          onPick={(id) => void openChunk(id)}
        />
      )}
    </section>
  );
}
