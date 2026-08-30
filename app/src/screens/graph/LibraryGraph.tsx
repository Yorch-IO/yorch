import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorGuidanceKey,
  errorMessage,
  type Claim,
  type GraphConcept,
  type GraphDocument,
  type LibraryGraph as LibraryGraphData,
} from "../../lib/api";
import { fit, settle, type ForceEdge, type ForceNode } from "../../lib/force";
import { useLibraries } from "../../lib/libraries";
import { scale, truncate } from "../../lib/radial";
import { Swatch } from "./DocumentGraph";
import { CONCEPT_RADIUS, GRAPH_ZOOM } from "./geometry";

/**
 * The whole library at once: every projected book, every concept that joins two
 * of them, and one weighted edge per pair.
 *
 * **What is drawn is not everything, and the control that decides it is degree.**
 * Measured on the real corpus (67 books, 2026-08-24): the library holds 10,835
 * concepts, of which 1,719 appear in more than one book. Raising the confidence
 * floor from 0.6 to 0.9 removes 3% of the edges — the extractor is confident
 * about nearly everything it proposes — while requiring two books removes 84%,
 * and everything it removes is a leaf that cannot connect anything. So the
 * threshold control is `minDocuments`, the floor is kept because the badge
 * promises one, and both refetch because both are server-side filters.
 *
 * Nothing becomes unreachable by being undrawn: the list beside the canvas holds
 * every node in the response, is filtered by the same search box, and is the
 * keyboard path to any of them. Setting the threshold to 1 draws the lot.
 *
 * **Paint order is a contract**, as in the document view: edges, then books,
 * then concepts, then labels. Labels last or a node covers them; concepts over
 * books because a concept is what a reader is chasing.
 */

/** Where the simulation's coordinates are drawn. Pan and zoom compose on top. */
const VIEW = { W: 1200, H: 820 } as const;

/** Below this zoom, only the labels a reader asked for are drawn. Above it,
 *  every book is named — books are few and their names are the map. */
const LABEL_ZOOM = 1.35;

/** Zoom bounds. Below 0.35 the graph is a texture; above 6 a single node fills
 *  the canvas and panning becomes the only navigation left. */
const ZOOM = GRAPH_ZOOM;

/** The thresholds the control offers. 1 is "everything", and it is reachable on
 *  purpose — the canvas slows down but nothing is hidden by the app's choice. */
const THRESHOLDS = [1, 2, 3, 5, 10] as const;

const FLOORS = [0.5, 0.6, 0.7, 0.8, 0.9] as const;

/** The document view's marks, minus `dashed`: there is one edge type here. */
const OVERVIEW_LEGEND = ["doc", "concept", "weight"] as const;

/** How many nodes the accessible list renders before it says "and N more".
 *  10,835 list items is not a keyboard path either. */
const LIST_CAP = 200;

type Selection =
  | { kind: "doc"; id: string }
  | { kind: "concept"; id: string }
  | null;

interface Placed {
  id: string;
  kind: "doc" | "concept";
  x: number;
  y: number;
  r: number;
  label: string;
}

/** The same badge as the document view and Explore, meaning the same thing. */
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

export function LibraryGraph({
  onOpenDocument,
}: {
  /** Called when the reader asks to look inside a book. The shell switches to
   *  the document view; this component does not know that view exists. */
  onOpenDocument?: (doc: GraphDocument) => void;
} = {}) {
  const { t } = useTranslation();
  const { selected: libraryId } = useLibraries();

  const [data, setData] = useState<LibraryGraphData | null>(null);
  const [minDocuments, setMinDocuments] = useState<number>(2);
  const [floor, setFloor] = useState<number>(0.6);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const [query, setQuery] = useState("");
  const [showBooks, setShowBooks] = useState(true);
  const [showConcepts, setShowConcepts] = useState(true);
  const [selected, setSelected] = useState<Selection>(null);
  const [hoveredConcept, setHoveredConcept] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });

  const [claims, setClaims] = useState<Claim[] | null>(null);
  const [claimsBusy, setClaimsBusy] = useState(false);

  const canvas = useRef<SVGSVGElement | null>(null);
  const dragging = useRef<{ x: number; y: number } | null>(null);
  const fetching = useRef(0);
  const opening = useRef<string | null>(null);

  // -- the data ------------------------------------------------------------

  useEffect(() => {
    if (!libraryId) return;
    const token = fetching.current + 1;
    fetching.current = token;
    setBusy(true);
    void (async () => {
      try {
        const next = await api.libraryGraph(libraryId, floor, minDocuments);
        if (fetching.current !== token) return;
        setData(next);
        setError(null);
        setSelected(null);
        setHoveredConcept(null);
        setClaims(null);
      } catch (e) {
        // The previous graph is deliberately left on screen: a failed refetch at
        // a new threshold should not blank a picture that was correct.
        if (fetching.current === token) setError(e);
      } finally {
        if (fetching.current === token) setBusy(false);
      }
    })();
  }, [libraryId, floor, minDocuments]);

  // -- the layout ----------------------------------------------------------

  const layout = useMemo(() => {
    if (data === null) return null;
    const maxDocWeight = Math.max(
      1,
      ...data.documents.map(
        (d) => data.edges.filter((e) => e.versionId === d.versionId).length,
      ),
    );
    const nodes: ForceNode[] = [
      ...data.documents.map((d) => ({
        id: d.versionId,
        kind: "doc" as const,
        weight: maxDocWeight,
      })),
      ...data.concepts.map((c) => ({
        id: c.id,
        kind: "concept" as const,
        weight: c.documents,
      })),
    ];
    const edges: ForceEdge[] = data.edges.map((e) => ({
      source: e.versionId,
      target: e.conceptId,
      weight: e.mentions,
    }));

    const settled = settle(nodes, edges);
    const t = fit(settled, { width: VIEW.W, height: VIEW.H, margin: 40 });

    const placed: Placed[] = [];
    for (const d of data.documents) {
      const p = settled.positions.get(d.versionId);
      if (p === undefined) continue;
      placed.push({
        id: d.versionId,
        kind: "doc",
        x: p.x * t.scale + t.x,
        y: p.y * t.scale + t.y,
        // Books are a fixed size: there are dozens of them and they are the
        // landmarks, so varying them buys nothing and costs legibility.
        r: 9,
        label: d.title ?? d.versionId,
      });
    }
    for (const c of data.concepts) {
      const p = settled.positions.get(c.id);
      if (p === undefined) continue;
      placed.push({
        id: c.id,
        kind: "concept",
        x: p.x * t.scale + t.x,
        y: p.y * t.scale + t.y,
        r: CONCEPT_RADIUS,
        label: c.name,
      });
    }
    return placed;
  }, [data]);

  const byId = useMemo(() => {
    const map = new Map<string, Placed>();
    for (const p of layout ?? []) map.set(p.id, p);
    return map;
  }, [layout]);

  const documentsById = useMemo(() => {
    const map = new Map<string, GraphDocument>();
    for (const d of data?.documents ?? []) map.set(d.versionId, d);
    return map;
  }, [data]);

  const conceptsById = useMemo(() => {
    const map = new Map<string, GraphConcept>();
    for (const c of data?.concepts ?? []) map.set(c.id, c);
    return map;
  }, [data]);

  /** Which nodes the current selection lights up: a book's concepts, or a
   *  concept's books. Null when nothing is selected, which is what tells the
   *  paint code to fade nothing. */
  const neighbours = useMemo(() => {
    if (selected === null || data === null) return null;
    const ids = new Set<string>([selected.id]);
    for (const e of data.edges) {
      if (selected.kind === "doc" && e.versionId === selected.id) ids.add(e.conceptId);
      if (selected.kind === "concept" && e.conceptId === selected.id) ids.add(e.versionId);
    }
    return ids;
  }, [selected, data]);

  const needle = query.trim().toLocaleLowerCase();
  const matches = useMemo(() => {
    if (needle === "") return null;
    const ids = new Set<string>();
    for (const p of layout ?? []) {
      if (p.label.toLocaleLowerCase().includes(needle)) ids.add(p.id);
    }
    return ids;
  }, [needle, layout]);

  const visible = useCallback(
    (p: Placed) => (p.kind === "doc" ? showBooks : showConcepts),
    [showBooks, showConcepts],
  );

  /** Every node the reader could reach right now, which is what the list shows
   *  and what the "N of M" line counts. */
  const listed = useMemo(() => {
    const rows = (layout ?? []).filter(
      (p) => visible(p) && (matches === null || matches.has(p.id)),
    );
    rows.sort((a, b) => a.label.localeCompare(b.label));
    return rows;
  }, [layout, visible, matches]);

  // -- interaction ---------------------------------------------------------

  const reset = useCallback(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, []);

  const zoomBy = useCallback((factor: number) => {
    setZoom((z) => Math.min(ZOOM.MAX, Math.max(ZOOM.MIN, z * factor)));
  }, []);

  // Wheel is bound imperatively because React's `onWheel` is passive: it cannot
  // call `preventDefault`, so the page would scroll out from under the canvas
  // on every zoom.
  useEffect(() => {
    const node = canvas.current;
    if (node === null) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomBy(e.deltaY < 0 ? ZOOM.STEP : 1 / ZOOM.STEP);
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [zoomBy]);

  const onPointerDown = (e: React.PointerEvent<SVGSVGElement>) => {
    dragging.current = { x: e.clientX - pan.x, y: e.clientY - pan.y };
  };
  const onPointerMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const from = dragging.current;
    if (from === null) return;
    setPan({ x: e.clientX - from.x, y: e.clientY - from.y });
  };
  const endDrag = () => {
    dragging.current = null;
  };

  const openConcept = useCallback(async (concept: GraphConcept) => {
    setSelected({ kind: "concept", id: concept.id });
    opening.current = concept.id;
    setClaims(null);
    setClaimsBusy(true);
    try {
      const answer = await api.exploreClaims(concept.id);
      if (opening.current !== concept.id) return;
      setClaims(answer.claims);
    } catch {
      // Secondary to the picture, which is still correct without it.
      if (opening.current === concept.id) setClaims([]);
    } finally {
      if (opening.current === concept.id) setClaimsBusy(false);
    }
  }, []);

  const pick = useCallback(
    (p: Placed) => {
      if (p.kind === "doc") {
        setSelected({ kind: "doc", id: p.id });
        setClaims(null);
        return;
      }
      const concept = conceptsById.get(p.id);
      if (concept !== undefined) void openConcept(concept);
    },
    [conceptsById, openConcept],
  );

  const activate = (p: Placed) => (e: React.KeyboardEvent) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    // Space scrolls the workspace out from under the node otherwise.
    e.preventDefault();
    pick(p);
  };

  // -- render --------------------------------------------------------------

  const selectedDoc =
    selected?.kind === "doc" ? documentsById.get(selected.id) ?? null : null;
  const selectedConcept =
    selected?.kind === "concept" ? conceptsById.get(selected.id) ?? null : null;

  const maxEdge = useMemo(
    () => Math.max(1, ...(data?.edges ?? []).map((e) => e.mentions)),
    [data],
  );

  const guidance = error === null ? null : errorGuidanceKey(error);

  return (
    <div className="graph-overview">
      <div className="graph-controls">
        <label className="field field-inline">
          <span>{t("graph.search")}</span>
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("graph.searchPlaceholder")}
          />
        </label>

        <label className="field field-inline">
          <span>{t("graph.shared")}</span>
          <select
            value={minDocuments}
            onChange={(e) => setMinDocuments(Number(e.target.value))}
          >
            {THRESHOLDS.map((n) => (
              <option key={n} value={n}>
                {n === 1 ? t("graph.sharedAll") : t("graph.sharedAtLeast", { n })}
              </option>
            ))}
          </select>
        </label>

        <label className="field field-inline">
          <span>{t("graph.confidence")}</span>
          <select value={floor} onChange={(e) => setFloor(Number(e.target.value))}>
            {FLOORS.map((f) => (
              <option key={f} value={f}>
                ≥{Math.round(f * 100)}%
              </option>
            ))}
          </select>
        </label>

        <fieldset className="field-inline graph-filters">
          <legend className="visually-hidden">{t("graph.show")}</legend>
          <label>
            <input
              type="checkbox"
              checked={showBooks}
              onChange={(e) => setShowBooks(e.target.checked)}
            />
            {t("graph.filterBooks")}
          </label>
          <label>
            <input
              type="checkbox"
              checked={showConcepts}
              onChange={(e) => setShowConcepts(e.target.checked)}
            />
            {t("graph.filterConcepts")}
          </label>
        </fieldset>

        <Proposed floor={data?.confidenceFloor ?? floor} />
      </div>

      <div className="actions graph-zoom">
        <button type="button" onClick={() => zoomBy(ZOOM.STEP)}>
          {t("graph.zoomIn")}
        </button>
        <button type="button" onClick={() => zoomBy(1 / ZOOM.STEP)}>
          {t("graph.zoomOut")}
        </button>
        <button type="button" onClick={reset}>
          {t("graph.reset")}
        </button>
      </div>

      {error !== null && (
        <div className="error">
          <strong>{t("graph.failed")}</strong>
          {guidance && <p>{t(guidance)}</p>}
          <pre className="detail">{errorMessage(error)}</pre>
        </div>
      )}

      {busy && data === null && <p className="muted">{t("graph.loading")}</p>}

      {data !== null && data.documents.length === 0 && (
        <p className="muted">{t("graph.libraryEmpty")}</p>
      )}

      {layout !== null && data !== null && data.documents.length > 0 && (
        <div className="graph-layout">
          <div className="graph-main">
            <svg
              ref={canvas}
              className={`graph-canvas ${selected !== null ? "is-probing" : ""}`}
              viewBox={`0 0 ${VIEW.W} ${VIEW.H}`}
              role="group"
              aria-label={t("graph.libraryAlt", {
                books: data.documents.length,
                concepts: data.concepts.length,
              })}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerLeave={endDrag}
            >
              <g transform={`translate(${pan.x} ${pan.y}) scale(${zoom})`}>
                {/* Edges first. Anything drawn after covers them, which is the
                    point: a line under a node reads as attached to it. */}
                {data.edges.map((e) => {
                  const a = byId.get(e.versionId);
                  const b = byId.get(e.conceptId);
                  if (a === undefined || b === undefined) return null;
                  if (!showBooks || !showConcepts) return null;
                  const lit =
                    neighbours === null ||
                    (neighbours.has(e.versionId) && neighbours.has(e.conceptId));
                  return (
                    <line
                      key={`${e.versionId}:${e.conceptId}`}
                      className={`edge mentions ${lit ? "" : "is-faded"}`}
                      x1={a.x}
                      y1={a.y}
                      x2={b.x}
                      y2={b.y}
                      strokeWidth={scale(e.mentions, maxEdge, 0.6, 2.4)}
                    />
                  );
                })}

                {layout.filter((p) => visible(p) && p.kind === "doc").map((p) => {
                  const dimmed = neighbours !== null && !neighbours.has(p.id);
                  const found = matches !== null && matches.has(p.id);
                  const chosen = selected?.id === p.id;
                  const named =
                    chosen || found || (p.kind === "doc" && zoom >= LABEL_ZOOM);
                  return (
                    <g
                      key={p.id}
                      className={`node ${p.kind} ${chosen ? "is-active" : ""} ${
                        found ? "is-shared" : ""
                      } ${dimmed && !found ? "is-faded" : ""}`}
                      role="button"
                      tabIndex={-1}
                      aria-label={p.label}
                      aria-pressed={chosen}
                      onClick={() => pick(p)}
                      onKeyDown={activate(p)}
                    >
                      <title>{p.label}</title>
                      <rect
                        className="shape"
                        x={p.x - p.r}
                        y={p.y - p.r * 0.7}
                        width={p.r * 2}
                        height={p.r * 1.4}
                        rx={2}
                      />
                      {named && (
                        <text x={p.x} y={p.y - p.r - 4} textAnchor="middle">
                          {truncate(p.label, 24)}
                        </text>
                      )}
                    </g>
                  );
                })}
                {layout.filter((p) => visible(p) && p.kind === "concept").map((p) => {
                  const dimmed = neighbours !== null && !neighbours.has(p.id);
                  const found = matches !== null && matches.has(p.id);
                  const chosen = selected?.id === p.id;
                  const hovered = hoveredConcept === p.id;
                  // The parent scales the position; this inverse scale keeps
                  // the node and its label at their screen-space size.
                  return (
                    <g
                      key={p.id}
                      className={`node concept ${chosen ? "is-active" : ""} ${
                        found ? "is-shared" : ""
                      } ${dimmed && !found ? "is-faded" : ""} ${
                        hovered ? "is-hovered" : ""
                      }`}
                      transform={`translate(${p.x} ${p.y}) scale(${1 / zoom})`}
                      role="button"
                      tabIndex={-1}
                      aria-label={p.label}
                      aria-pressed={chosen}
                      onClick={() => pick(p)}
                      onKeyDown={activate(p)}
                      onMouseEnter={() => setHoveredConcept(p.id)}
                      onMouseLeave={() => setHoveredConcept(null)}
                    >
                      <title>{p.label}</title>
                      <circle className="shape" r={CONCEPT_RADIUS} />
                      {(chosen || found || hovered) && (
                        <text
                          className="concept-label"
                          x={CONCEPT_RADIUS + 8}
                          dominantBaseline="middle"
                        >
                          {hovered ? p.label : truncate(p.label, 24)}
                        </text>
                      )}
                    </g>
                  );
                })}
              </g>
            </svg>

            <p className="graph-inspector">
              {t("graph.libraryCounts", {
                books: data.documents.length,
                concepts: data.concepts.length,
                edges: data.edges.length,
              })}
              {(data.truncated.edges || data.truncated.documents) &&
                ` · ${t("graph.truncated")}`}
              {busy && ` · ${t("graph.loading")}`}
            </p>

            {/* Three of the document view's four marks, drawn from its own
            component so one stylesheet change moves both legends. `dashed`
                is left out: there is no second edge type here — every line is a
                mention. `weight` is a mention count rather than a number of
                shared concepts. */}
            <ul className="graph-legend">
              {OVERVIEW_LEGEND.map((kind) => (
                <li key={kind}>
                  <Swatch kind={kind} />
                  <span>{t(`graph.overviewLegend.${kind}`)}</span>
                </li>
              ))}
            </ul>

            <p className="muted small graph-note">{t("graph.libraryProposed")}</p>
          </div>

          <div className="pane graph-detail">
            {selectedDoc !== null && (
              <>
                <h3 className="pane-title">{selectedDoc.title ?? selectedDoc.versionId}</h3>
                <p className="muted small">
                  {t("graph.bookConcepts", {
                    count:
                      neighbours === null ? 0 : Math.max(0, neighbours.size - 1),
                  })}
                </p>
                {onOpenDocument && (
                  <div className="actions">
                    <button type="button" onClick={() => onOpenDocument(selectedDoc)}>
                      {t("graph.openDocument")}
                    </button>
                  </div>
                )}
                <ul className="compare-list is-shared">
                  {[...(neighbours ?? [])]
                    .filter((id) => id !== selectedDoc.versionId)
                    .map((id) => conceptsById.get(id))
                    .filter((c): c is GraphConcept => c !== undefined)
                    .sort((a, b) => b.mentions - a.mentions)
                    .slice(0, 60)
                    .map((c) => (
                      <li key={c.id}>
                        <button type="button" onClick={() => void openConcept(c)}>
                          {c.name}
                        </button>
                        <span className="muted small"> · {c.documents}</span>
                      </li>
                    ))}
                </ul>
              </>
            )}

            {selectedConcept !== null && (
              <>
                <h3 className="pane-title">{selectedConcept.name}</h3>
                <p className="muted small">
                  {t("graph.conceptBooks", { count: selectedConcept.documents })}
                </p>
                <ul className="compare-list is-shared">
                  {[...(neighbours ?? [])]
                    .filter((id) => id !== selectedConcept.id)
                    .map((id) => documentsById.get(id))
                    .filter((d): d is GraphDocument => d !== undefined)
                    .map((d) => (
                      <li key={d.versionId}>
                        <button
                          type="button"
                          onClick={() => setSelected({ kind: "doc", id: d.versionId })}
                        >
                          {d.title ?? d.versionId}
                        </button>
                      </li>
                    ))}
                </ul>
                <h4 className="pane-title">{t("graph.claims")}</h4>
                {claimsBusy ? (
                  <p className="muted">{t("graph.loading")}</p>
                ) : claims !== null && claims.length > 0 ? (
                  <ul className="claims">
                    {claims.slice(0, 20).map((c) => (
                      <li key={c.id}>
                        <span className={`badge claim-${c.status}`}>
                          {t(`claim.status.${c.status}`)}
                        </span>{" "}
                        {c.text}
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="muted">{t("graph.noClaims")}</p>
                )}
              </>
            )}

            {selected === null && (
              <>
                <h3 className="pane-title">{t("graph.allNodes")}</h3>
                {/* The keyboard path, and the reason nothing is unreachable for
                    having been left off the canvas: every node in the response
                    is here, filtered by the same search box. */}
                <p className="muted small">
                  {t("graph.listing", {
                    shown: Math.min(listed.length, LIST_CAP),
                    total: listed.length,
                  })}
                </p>
                <ul className="graph-index">
                  {listed.slice(0, LIST_CAP).map((p) => (
                    <li key={p.id}>
                      <button type="button" onClick={() => pick(p)}>
                        <span className={`dot node-dot node-${p.kind}`} aria-hidden="true" />
                        {p.label}
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
