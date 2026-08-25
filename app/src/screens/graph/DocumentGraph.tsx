/**
 * The graph, drawn.
 *
 * `ExploreScreen` already reads the same three endpoints and renders them as
 * lists, which is the better shape for reading one document. This screen exists
 * for the question a list cannot answer: *which concepts bridge which
 * documents*. That is the graph's whole reason to exist and it is invisible in a
 * column of names.
 *
 * **What is drawn is only what the API actually returns.** The centre is the
 * chosen version, the inner ring is the concepts it mentions (`MENTIONS`, one
 * edge each), and the outer ring is the documents that share concepts with it,
 * with edge weight from `sharedConcepts`. There is deliberately no edge between
 * an outer document and an inner concept: `/versions/{id}/related` returns a
 * *count* of shared concepts, never which ones, so any line drawn there would be
 * invented. Inventing an edge in a view whose purpose is to show evidence is the
 * worst thing this screen could do, so the omission is the feature.
 *
 * Inline SVG and no layout library, for two reasons: the webview runs under a
 * `default-src 'self'` CSP so nothing loads from a CDN anyway, and a radial
 * layout of two rings needs trigonometry, not a force simulation. Positions are
 * a pure function of the data, so the picture does not wander between renders —
 * and because they are a pure function, they live in `lib/radial.ts` where
 * `radial.test.ts` can prove the labels do not collide. jsdom computes no SVG
 * geometry, so a rendered test here can only see attributes; the picture is
 * checked as arithmetic or not at all.
 *
 * Two rules hold the picture together and neither is visible from any one line:
 *
 * 1. **Paint order is a contract.** Edges, then document cards, then the centre,
 *    then concepts and their labels last. Concepts are drawn on top, so a
 *    document can never cover one — which is the part `radial.ts`'s spacing
 *    cannot guarantee on its own, since a wide axis-aligned card on an arbitrary
 *    ray will sometimes reach further than the ring's angular gap allows.
 * 2. **Every label carries a halo** (`paint-order: stroke fill` in the
 *    stylesheet), so text stays legible over whatever passes behind it.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorMessage,
  type Claim,
  type Concept,
  type DocumentRow,
  type RelatedDocument,
} from "../../lib/api";
import { useLibraries } from "../../lib/libraries";
import {
  GEOM,
  halfStep,
  labelAnchor,
  LABEL,
  ring,
  scale,
  STAGGER,
  truncate,
} from "../../lib/radial";

/** Concepts drawn in the inner ring. Past roughly this many the labels collide
 *  and the picture stops being readable, which defeats the point of drawing it.
 *  The full list is what `ExploreScreen` is for. */
const CONCEPTS = 12;
/** Related documents in the outer ring, same reasoning. */
const RELATED = 8;

/** Which node the pointer or the keyboard is on. One piece of state for both,
 *  which is what makes keyboard focus behave identically to hover instead of
 *  being a second code path that drifts. */
type NodeKey = { kind: "concept" | "doc" | "centre"; id: string } | null;

/** The legend's entries, in reading order. Rendered from this rather than
 *  written out five times, and the i18n scanner accepts the resulting
 *  `graph.legend.${…}` lookup as using the whole group. */
export const LEGEND = ["concept", "doc", "size", "weight", "dashed"] as const;

/** What the active node touches. The centre touches every edge there is, so
 *  hovering it lights the whole picture rather than dimming it — anything else
 *  would be a lie about the topology. */
function incident(active: NodeKey, kind: "concept" | "doc", id: string): boolean {
  if (active === null || active.kind === "centre") return true;
  return active.kind === kind && active.id === id;
}

/**
 * The house badge for "a model proposed this".
 *
 * `styles.css` calls `.semantic` the load-bearing class on the Explore screen,
 * for the reason `api.ts` states beside these types: an edge a model suggested
 * and an edge read off a table of contents have different standing as evidence.
 * Every single thing on this canvas is the first kind, and this screen used to
 * be the only one showing that data without saying so — `semantic` and
 * `confidenceFloor` arrive on all three responses and were being discarded.
 *
 * The wording is `explore.proposed` rather than a second copy of it: the same
 * badge meaning the same thing, so two strings that could drift apart would be
 * worse than the borrowed namespace.
 */
function Proposed({ floor }: { floor: number }) {
  const { t } = useTranslation();
  const pct = Math.round(floor * 100);
  return (
    <span
      className="semantic"
      title={`${t("explore.proposedHelp")} ${t("graph.floor", { floor: pct })}`}
    >
      {t("explore.proposed")}
      <span className="confidence"> ≥{pct}%</span>
    </span>
  );
}

/** The legend's marks, drawn with the same classes as the canvas so they cannot
 *  disagree with it: change a colour in the stylesheet and both move.
 *
 *  Exported because the library overview needs four of the same five, and a
 *  second copy would be two legends drifting apart from one stylesheet. */
export function Swatch({ kind }: { kind: (typeof LEGEND)[number] }) {
  // `shape` on every mark, not just decoration: the fill and stroke rules are
  // keyed on it, and without it these paint SVG's default black.
  return (
    <svg className="graph-swatch graph-canvas" viewBox="0 0 26 16" aria-hidden="true">
      {kind === "concept" && (
        <g className="node concept is-active">
          <circle className="shape" cx={13} cy={8} r={6} />
        </g>
      )}
      {kind === "doc" && (
        <g className="node doc is-active">
          <rect className="shape" x={2} y={3} width={22} height={10} rx={2} />
        </g>
      )}
      {kind === "size" && (
        <g className="node concept is-active">
          <circle className="shape" cx={6} cy={8} r={3} />
          <circle className="shape" cx={18} cy={8} r={6} />
        </g>
      )}
      {kind === "weight" && (
        <>
          {/* Solid, so this swatch reads as "thin against thick" and the one
              below it reads as "dashed against solid". Drawn dashed, both said
              the same thing twice. */}
          <line className="edge mentions is-active" x1={1} y1={5} x2={25} y2={5} strokeWidth={1} />
          <line className="edge mentions is-active" x1={1} y1={12} x2={25} y2={12} strokeWidth={4} />
        </>
      )}
      {kind === "dashed" && (
        <>
          <line className="edge shares is-active" x1={1} y1={5} x2={25} y2={5} strokeWidth={2} />
          <line className="edge mentions is-active" x1={1} y1={12} x2={25} y2={12} strokeWidth={2} />
        </>
      )}
    </svg>
  );
}

export function DocumentGraph({
  focus = null,
}: {
  /** A version the overview asked to look inside, and the title to show while
   *  its own request is in flight. Centring is otherwise the picker's job; this
   *  is how "ver este libro" arrives from the library-wide canvas. */
  focus?: { versionId: string; title: string } | null;
} = {}) {
  const { t } = useTranslation();
  const { selected: libraryId } = useLibraries();

  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [versionId, setVersionId] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const [concepts, setConcepts] = useState<Concept[]>([]);
  const [related, setRelated] = useState<RelatedDocument[]>([]);
  /** The confidence floor the projection applied, kept so the badge can say it.
   *  `null` until something has been centred. */
  const [floor, setFloor] = useState<number | null>(null);
  const [openConcept, setOpenConcept] = useState<Concept | null>(null);
  const [claims, setClaims] = useState<Claim[]>([]);
  /** Separate from `claims.length === 0`, which conflated "still fetching" with
   *  "nothing clears the floor" and flashed the wrong answer at every click. */
  const [claimsBusy, setClaimsBusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [active, setActive] = useState<NodeKey>(null);
  /** The outer document held up against the centre, by version id.
   *
   *  A count answers "these two share 57 concepts" and leaves the only question
   *  a reader actually has unanswered. Selecting a document turns the canvas
   *  into that comparison: the shared concepts light up, the rest fade, and the
   *  pane lists all of them by name. */
  const [compare, setCompare] = useState<string | null>(null);
  /** The compared document's own top concepts, for the "only over there" group.
   *  Secondary: the comparison works without it, so a failure here is silent
   *  rather than an error banner over a picture that is still correct. */
  const [otherConcepts, setOtherConcepts] = useState<Concept[] | null>(null);
  const [compareBusy, setCompareBusy] = useState(false);

  // Two in-flight guards. Re-centring and opening a concept are both one click
  // now, so a slow first response landing after a fast second one is a real
  // sequence rather than a theoretical one.
  const centring = useRef<string | null>(null);
  const opening = useRef<string | null>(null);
  const comparing = useRef<string | null>(null);
  const detail = useRef<HTMLDivElement | null>(null);

  // The shelf, so the centre can be chosen and so a related document — which
  // arrives as a *version* id — can be re-centred on.
  useEffect(() => {
    if (!libraryId) return;
    let live = true;
    void (async () => {
      try {
        const library = await api.libraryDocuments(libraryId);
        if (!live) return;
        setDocuments(library.documents);
        setError(null);
      } catch (e) {
        if (live) setError(e);
      }
    })();
    return () => {
      live = false;
    };
  }, [libraryId]);

  const centre = useCallback(async (version: string, label: string) => {
    centring.current = version;
    setBusy(true);
    setError(null);
    setOpenConcept(null);
    setClaims([]);
    setActive(null);
    // A comparison names two versions; re-centring replaces one of them, so
    // keeping it open would show an overlap that is no longer on screen.
    setCompare(null);
    setOtherConcepts(null);
    try {
      // Both are free reads of the projection, so there is no reason to make
      // the user ask for them separately.
      const [c, r] = await Promise.all([
        api.exploreConcepts(version),
        api.exploreRelated(version),
      ]);
      if (centring.current !== version) return;
      setVersionId(version);
      setTitle(label);
      setConcepts(c.concepts.slice(0, CONCEPTS));
      setRelated(r.documents.slice(0, RELATED));
      // Both responses carry it and they describe the same projection; the
      // concepts one is taken because a version with no related documents still
      // has a floor.
      setFloor(c.confidenceFloor);
    } catch (e) {
      if (centring.current === version) setError(e);
    } finally {
      if (centring.current === version) setBusy(false);
    }
  }, []);

  const showClaims = useCallback(async (concept: Concept) => {
    // A second click on the open concept closes it. The node reports itself as
    // `aria-pressed`, so a toggle is what it has already promised.
    if (openConcept?.id === concept.id) {
      opening.current = null;
      setOpenConcept(null);
      setClaims([]);
      setClaimsBusy(false);
      return;
    }
    opening.current = concept.id;
    // One pane, one mode. Claims and a comparison both want the whole detail
    // pane, and two things fighting over it is how a panel starts lying about
    // which selection it describes.
    setCompare(null);
    setOtherConcepts(null);
    setOpenConcept(concept);
    setClaims([]);
    setClaimsBusy(true);
    try {
      const result = await api.exploreClaims(concept.id);
      if (opening.current !== concept.id) return;
      setClaims(result.claims);
    } catch (e) {
      if (opening.current === concept.id) setError(e);
    } finally {
      if (opening.current === concept.id) setClaimsBusy(false);
    }
  }, [openConcept]);

  /** Hold a related document up against the centre, or put it down again.
   *
   *  This is what a click on an outer card does now. Re-centring moved to an
   *  explicit button in the pane: one gesture, one meaning, and the question
   *  people actually arrive with — *which* concepts — is the one the click
   *  answers.
   */
  const selectDoc = useCallback(async (doc: RelatedDocument) => {
    if (compare === doc.id) {
      comparing.current = null;
      setCompare(null);
      setOtherConcepts(null);
      setCompareBusy(false);
      return;
    }
    comparing.current = doc.id;
    setCompare(doc.id);
    setOpenConcept(null);
    setClaims([]);
    setOtherConcepts(null);
    setCompareBusy(true);
    try {
      // The shared concepts already arrived with `related`. This second read is
      // only for what the *other* document has and the centre does not, which
      // no single response can know.
      const r = await api.exploreConcepts(doc.id);
      if (comparing.current !== doc.id) return;
      setOtherConcepts(r.concepts);
    } catch {
      // Deliberately swallowed: the shared list and the highlighting are intact
      // without it, and the pane simply omits the group it cannot fill.
      if (comparing.current === doc.id) setOtherConcepts(null);
    } finally {
      if (comparing.current === doc.id) setCompareBusy(false);
    }
  }, [compare]);

  /**
   * Bring the answer into view when a comparison opens.
   *
   * Below 76rem the pane stacks *under* the canvas, so on a laptop window a
   * click lit up the graph and put the list of shared concepts off-screen —
   * which reads as "nothing happened" to anyone who did not scroll. The canvas
   * still answers "which nodes", but the names are the half that needs showing.
   *
   * Optional-called: jsdom implements no scrolling, and `scrollIntoView` is
   * simply absent there. The same lesson as the shell's scroll reset, which
   * assigns `scrollTop` rather than calling `scrollTo`.
   */
  useEffect(() => {
    if (compare === null) return;
    detail.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  }, [compare]);

  // Centred from outside: the overview hands over a version and this view opens
  // on it. Keyed on the version rather than on the object, so a parent that
  // re-renders does not re-fetch — and guarded against the version already
  // shown, which is what a user pressing "back" and then "ver este libro" again
  // would otherwise pay for twice.
  useEffect(() => {
    if (focus === null || focus.versionId === versionId) return;
    void centre(focus.versionId, focus.title);
    // `versionId` is deliberately not a dependency: it changes *because* of this
    // effect, and listing it would re-run the guard against its own result.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.versionId, focus?.title, centre]);

  const withVersion = useMemo(
    () => documents.filter((d) => d.activeVersionId !== null),
    [documents],
  );

  /**
   * The picker's options.
   *
   * Clicking an outer-ring document re-centres on a *version* id, which is
   * generally not any shelf document's `activeVersionId` — so the `<select>`
   * found no matching `<option>` and silently reverted to "pick a document"
   * while the canvas showed one. The control was lying about what was drawn.
   * The centred version is added to the list when the shelf does not already
   * hold it.
   */
  const options = useMemo(() => {
    const rows = withVersion.map((d) => ({
      value: d.activeVersionId as string,
      label: d.title,
    }));
    if (versionId !== null && !rows.some((r) => r.value === versionId)) {
      rows.unshift({ value: versionId, label: title });
    }
    return rows;
  }, [withVersion, versionId, title]);

  /** Concept id → the related documents that also mention it.
   *
   *  Built once per response, so hovering a concept is a map lookup rather than
   *  an intersection recomputed on every mouse move. */
  const docsByConcept = useMemo(() => {
    const m = new Map<string, RelatedDocument[]>();
    for (const d of related) {
      for (const id of d.sharedConceptIds) {
        const found = m.get(id);
        if (found) found.push(d);
        else m.set(id, [d]);
      }
    }
    return m;
  }, [related]);

  const selectedDoc = useMemo(
    () => related.find((d) => d.id === compare) ?? null,
    [related, compare],
  );

  /** The selected document's shared concepts, as a Set. Null when nothing is
   *  selected, which is what every "are we comparing?" test reads. */
  const sharedIds = useMemo(
    () => (selectedDoc === null ? null : new Set(selectedDoc.sharedConceptIds)),
    [selectedDoc],
  );

  /**
   * The three groups the pane shows.
   *
   * `shared` is complete: it comes from the intersection the graph traversal
   * computed, capped at 200 and nowhere near it in this corpus.
   *
   * The two "only" groups are **sound but not exhaustive**. Each version returns
   * its top concepts, not all of them, so a concept listed here really is unique
   * to that side — it is absent from a complete shared list — but there may be
   * more that neither response mentioned. The pane says so rather than implying
   * the lists are the whole story.
   */
  const comparison = useMemo(() => {
    if (selectedDoc === null || sharedIds === null) return null;
    return {
      shared: selectedDoc.sharedConceptNames,
      truncated: selectedDoc.sharedConceptNames.length < selectedDoc.sharedConcepts,
      centreOnly: concepts.filter((c) => !sharedIds.has(c.id)),
      otherOnly:
        otherConcepts === null
          ? null
          : otherConcepts.filter((c) => !sharedIds.has(c.id)),
    };
  }, [selectedDoc, sharedIds, concepts, otherConcepts]);

  const maxMentions = Math.max(1, ...concepts.map((c) => c.mentions));
  const maxShared = Math.max(1, ...related.map((d) => d.sharedConcepts));

  const placedConcepts = useMemo(
    () => ring(concepts, GEOM.R_CONCEPT, { stagger: STAGGER }),
    [concepts],
  );
  // Half of the outer ring's own step, so no document sits on a concept's ray.
  // Sharing twelve o'clock put document 0 directly behind concept 0.
  const placedDocs = useMemo(
    () => ring(related, GEOM.R_DOC, { offset: halfStep(RELATED) }),
    [related],
  );

  /** The line under the canvas. Its job is that nothing essential is reachable
   *  only through a tooltip — a full name that only appears on hover is not
   *  available to anyone who cannot hover. */
  const probe = useMemo(() => {
    if (active === null) return t("graph.hint");
    if (active.kind === "centre") return t("graph.probe.centre", { title });
    if (active.kind === "concept") {
      const c = concepts.find((x) => x.id === active.id);
      if (c === undefined) return t("graph.hint");
      const base = t("graph.probe.concept", {
        name: c.name,
        mentions: c.mentions,
        confidence: Math.round(c.confidence * 100),
      });
      // Which of the documents on screen also mention it. Straight from the
      // shared sets that arrived with the ring, so it is the same data the
      // highlighting uses and cannot disagree with it.
      const also = docsByConcept.get(c.id) ?? [];
      return also.length === 0
        ? base
        : `${base} · ${t("graph.probe.alsoIn", {
            count: also.length,
            titles: also.map((d) => d.title ?? d.id).join(", "),
          })}`;
    }
    const d = related.find((x) => x.id === active.id);
    return d === undefined
      ? t("graph.hint")
      : t("graph.probe.doc", { title: d.title ?? d.id, count: d.sharedConcepts });
  }, [active, concepts, related, title, docsByConcept, t]);

  const conceptLabel = (c: Concept): string =>
    t("graph.node.concept", { name: c.name, mentions: c.mentions });
  const docLabel = (d: RelatedDocument): string =>
    t(compare === d.id ? "graph.node.docSelected" : "graph.node.doc", {
      title: d.title ?? d.id,
      count: d.sharedConcepts,
    });

  /** Enter and Space both activate, which is what a `role="button"` promises.
   *  Space is prevented because its default is to scroll the workspace, and the
   *  node you just pressed leaves the window. */
  const activate = (run: () => void) => (e: React.KeyboardEvent) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      run();
    }
  };

  // No heading and no intro: the shell owns both, because it is the shell that
  // knows which of the two views is showing and therefore which sentence
  // describes it.
  return (
    <div className="graph-document">
      <div className="graph-controls">
        <label className="field field-inline">
          <span>{t("graph.centre")}</span>
          <select
            value={versionId ?? ""}
            onChange={(e) => {
              const picked = options.find((o) => o.value === e.target.value);
              if (picked !== undefined) void centre(picked.value, picked.label);
            }}
          >
            <option value="">{t("graph.pick")}</option>
            {options.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        {floor !== null && <Proposed floor={floor} />}
      </div>

      {error !== null && (
        <p className="warn">
          {t("graph.failed")} — {errorMessage(error)}
        </p>
      )}

      {versionId === null ? (
        <p className="muted">{t("graph.empty")}</p>
      ) : busy ? (
        <p className="muted">{t("graph.loading")}</p>
      ) : (
        <div className="graph-layout">
          <div className="graph-main">
            <svg
              className={`graph-canvas ${active !== null ? "is-probing" : ""}`}
              viewBox={`0 0 ${GEOM.W} ${GEOM.H}`}
              role="group"
              aria-label={t("graph.alt", { title })}
            >
              {/* Paint order, top to bottom of this block: edges, document
                  cards, the centre, then concepts. Concepts are last so nothing
                  can cover one. Do not reorder. */}
              {placedConcepts.map(({ item, x, y }) => (
                <line
                  key={`e-${item.id}`}
                  x1={GEOM.CX}
                  y1={GEOM.CY}
                  x2={x}
                  y2={y}
                  className={`edge mentions ${
                    incident(active, "concept", item.id) ? "is-active" : ""
                  } ${
                    sharedIds !== null
                      ? sharedIds.has(item.id)
                        ? "is-shared"
                        : "is-faded"
                      : ""
                  }`}
                  strokeWidth={scale(item.mentions, maxMentions, 1, 3)}
                />
              ))}
              {placedDocs.map(({ item, x, y }) => (
                <line
                  key={`e-${item.id}`}
                  x1={GEOM.CX}
                  y1={GEOM.CY}
                  x2={x}
                  y2={y}
                  className={`edge shares ${
                    incident(active, "doc", item.id) ? "is-active" : ""
                  } ${compare === item.id ? "is-selected" : ""} ${
                    compare !== null && compare !== item.id ? "is-faded" : ""
                  }`}
                  strokeWidth={scale(item.sharedConcepts, maxShared, 1, 4)}
                />
              ))}

              {/* Related documents: clicking one re-centres the graph on it. */}
              {placedDocs.map(({ item, x, y }) => {
                const name = item.title ?? item.id;
                const label = docLabel(item);
                const open = () => void selectDoc(item);
                const isSelected = compare === item.id;
                return (
                  <g
                    key={item.id}
                    className={`node doc ${
                      incident(active, "doc", item.id) ? "is-active" : ""
                    } ${isSelected ? "is-selected" : ""} ${
                      compare !== null && !isSelected ? "is-faded" : ""
                    }`}
                    transform={`translate(${x} ${y})`}
                    onClick={open}
                    role="button"
                    tabIndex={0}
                    aria-label={label}
                    aria-pressed={isSelected}
                    onKeyDown={activate(open)}
                    onMouseEnter={() => setActive({ kind: "doc", id: item.id })}
                    onMouseLeave={() => setActive(null)}
                    onFocus={() => setActive({ kind: "doc", id: item.id })}
                    onBlur={() => setActive(null)}
                  >
                    <title>{label}</title>
                    <rect
                      className="shape"
                      x={-GEOM.DOC_W / 2}
                      y={-GEOM.DOC_H / 2}
                      width={GEOM.DOC_W}
                      height={GEOM.DOC_H}
                      rx={6}
                    />
                    <text y={-5}>{truncate(name, LABEL.DOC_CHARS)}</text>
                    <text y={9} className="meta">
                      {t("graph.shared", { count: item.sharedConcepts })}
                    </text>
                    {/* A standing affordance. `cursor: pointer` only says the
                        card is clickable once the pointer is already on it, and
                        the whole point of this screen is that the count is a
                        question — "which 57?" — the card can answer. The chevron
                        turns when it is open. Decoration only: the accessible
                        name already says both the count and the state. */}
                    <text
                      className="doc-affordance"
                      x={GEOM.DOC_W / 2 - 10}
                      y={4}
                      aria-hidden="true"
                    >
                      {isSelected ? "▾" : "▸"}
                    </text>
                  </g>
                );
              })}

              <g
                className="node centre is-active"
                transform={`translate(${GEOM.CX} ${GEOM.CY})`}
                onMouseEnter={() => setActive({ kind: "centre", id: "centre" })}
                onMouseLeave={() => setActive(null)}
              >
                <title>{title}</title>
                <rect
                  className="shape"
                  x={-GEOM.CENTRE_W / 2}
                  y={-GEOM.CENTRE_H / 2}
                  width={GEOM.CENTRE_W}
                  height={GEOM.CENTRE_H}
                  rx={8}
                />
                <text y={-8}>{truncate(title, LABEL.CENTRE_CHARS)}</text>
                <text y={12} className="meta">
                  {t("graph.centreMeta", {
                    concepts: concepts.length,
                    related: related.length,
                  })}
                </text>
              </g>

              {/* Concepts, last: clicking one lists the claims behind it. */}
              {placedConcepts.map(({ item, x, y, angle }) => {
                const r = scale(item.mentions, maxMentions, 9, 11);
                const a = labelAnchor(angle, r);
                const label = conceptLabel(item);
                const isOpen = openConcept?.id === item.id;
                const open = () => void showClaims(item);
                // Only meaningful while a comparison is open: `sharedIds` is
                // null otherwise and both classes stay off, which is what keeps
                // the ordinary graph looking exactly as it did.
                const isShared = sharedIds !== null && sharedIds.has(item.id);
                return (
                  <g
                    key={item.id}
                    className={`node concept ${isOpen ? "is-open" : ""} ${
                      incident(active, "concept", item.id) ? "is-active" : ""
                    } ${isShared ? "is-shared" : ""} ${
                      sharedIds !== null && !isShared ? "is-faded" : ""
                    }`}
                    transform={`translate(${x} ${y})`}
                    onClick={open}
                    role="button"
                    tabIndex={0}
                    aria-label={label}
                    aria-pressed={isOpen}
                    onKeyDown={activate(open)}
                    onMouseEnter={() => setActive({ kind: "concept", id: item.id })}
                    onMouseLeave={() => setActive(null)}
                    onFocus={() => setActive({ kind: "concept", id: item.id })}
                    onBlur={() => setActive(null)}
                  >
                    <title>{label}</title>
                    {/* A comfortable target: the circle itself is 18px across at
                        the bottom of the mention scale. */}
                    <circle className="hit" r={Math.max(r, GEOM.HIT_R)} />
                    <circle className="shape" r={r} />
                    {isOpen && <circle className="ring" r={r + 5} />}
                    <text
                      x={a.dx}
                      y={a.dy}
                      textAnchor={a.anchor}
                      dominantBaseline={a.baseline}
                    >
                      {truncate(item.name, LABEL.CONCEPT_CHARS)}
                    </text>
                  </g>
                );
              })}
            </svg>

            {/* Deliberately not an `aria-live` region: every node already
                announces itself through `aria-label` on focus, so a live region
                here would repeat it and would fire on every mouse move. This
                line is for people who can see the canvas but are not hovering
                it. */}
            <p className="graph-inspector">{probe}</p>

            <ul className="graph-legend">
              {LEGEND.map((k) => (
                <li key={k}>
                  <Swatch kind={k} />
                  <span>{t(`graph.legend.${k}`)}</span>
                </li>
              ))}
            </ul>

            <p className="muted small graph-note">{t("graph.proposed")}</p>
          </div>

          <div className="pane graph-detail" ref={detail}>
            {comparison !== null && selectedDoc !== null ? (
              <>
                <h3 className="pane-title">
                  {selectedDoc.title ?? selectedDoc.id}
                </h3>
                <p className="muted small">
                  {t("graph.compare.count", { count: selectedDoc.sharedConcepts })}
                  {floor !== null && (
                    <>
                      {" "}
                      <Proposed floor={floor} />
                    </>
                  )}
                </p>

                <div className="compare-actions">
                  <button
                    type="button"
                    onClick={() =>
                      void centre(selectedDoc.id, selectedDoc.title ?? selectedDoc.id)
                    }
                  >
                    {t("graph.compare.centreHere")}
                  </button>
                  <button
                    type="button"
                    className="link"
                    onClick={() => void selectDoc(selectedDoc)}
                  >
                    {t("graph.compare.close")}
                  </button>
                </div>

                {/* The answer to the question the count could not answer. Names
                    come from the same traversal that produced the count, so the
                    list and the number cannot disagree. */}
                <h4>{t("graph.compare.shared")}</h4>
                {comparison.truncated && (
                  <p className="warn small">
                    {t("graph.compare.truncated", {
                      shown: comparison.shared.length,
                      total: selectedDoc.sharedConcepts,
                    })}
                  </p>
                )}
                <ul className="compare-list is-shared">
                  {comparison.shared.map((name) => (
                    <li key={name}>{name}</li>
                  ))}
                </ul>

                <h4>{t("graph.compare.centreOnly", { title })}</h4>
                <ul className="compare-list">
                  {comparison.centreOnly.map((c) => (
                    <li key={c.id}>{c.name}</li>
                  ))}
                </ul>

                <h4>{t("graph.compare.otherOnly")}</h4>
                {compareBusy ? (
                  <p className="muted">{t("graph.loading")}</p>
                ) : comparison.otherOnly === null ? (
                  <p className="muted">{t("graph.compare.otherUnavailable")}</p>
                ) : (
                  <ul className="compare-list">
                    {comparison.otherOnly.map((c) => (
                      <li key={c.id}>{c.name}</li>
                    ))}
                  </ul>
                )}

                {/* Said plainly rather than implied by a scrollbar: the shared
                    list is the whole intersection, the two "only" lists are not
                    the whole of either side. */}
                <p className="muted small">{t("graph.compare.partial")}</p>
              </>
            ) : (
            <>
            <h3 className="pane-title">
              {openConcept === null ? t("graph.claims") : openConcept.name}
              {openConcept?.conceptType != null && (
                <span className="meta"> · {openConcept.conceptType}</span>
              )}
            </h3>
            {openConcept === null ? (
              <p className="muted">{t("graph.selectConcept")}</p>
            ) : (
              <>
                <p className="muted small">
                  {t("explore.mentions", { count: openConcept.mentions })} ·{" "}
                  {Math.round(openConcept.confidence * 100)}%
                  {floor !== null && (
                    <>
                      {" "}
                      <Proposed floor={floor} />
                    </>
                  )}
                </p>
                {/* Present only when a run condensed it, which is a paid stage
                    that is off by default. The panel has to read well without
                    it, so it is an addition rather than a slot. */}
                {openConcept.description ? (
                  <p className="concept-description">{openConcept.description}</p>
                ) : null}
                {claimsBusy ? (
                  <p className="muted">{t("graph.loading")}</p>
                ) : claims.length === 0 ? (
                  <p className="muted">{t("graph.noClaims")}</p>
                ) : (
                  <ul className="claims">
                    {claims.map((c) => (
                      <li key={c.id}>
                        <p>{c.text}</p>
                        {/* The document's own words, when the extractor found the
                            model's quote inside the chunk it claimed to come
                            from. This is the only line in the panel a reader can
                            check without opening anything: the claim above it is
                            a model's paraphrase, this is the text. */}
                        {c.quote ? <q className="claim-quote">{c.quote}</q> : null}
                        {/* What the document *does* with the claim. Without it, a
                            doctrine a text is about to rebut reads exactly like
                            one it holds. */}
                        <span className={`claim-status is-${c.status}`}>
                          {t(`claim.status.${c.status}`)}
                        </span>
                        <span className="meta">
                          {Math.round(c.confidence * 100)}%
                        </span>
                        {/* The id a person checks the claim against. Fetched
                            since the first version of this screen and never
                            shown; the panel is the first place with room. */}
                        <span className="locator">
                          {t("graph.sourceChunk", { id: c.sourceChunkId })}
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
            </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
