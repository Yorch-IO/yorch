import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorGuidanceKey,
  errorMessage,
  type Claim,
  type GraphConcept,
  type GraphDocument,
} from "../../lib/api";
import { ATLAS_VIEW } from "../../lib/graphAtlas";
// The thresholds and the default live beside the derivation that reads them,
// so the control and the filter cannot disagree about what the stops are.
import {
  DEFAULT_THRESHOLD,
  matchesQuery,
  neighbours,
  visibleIn,
  subgraphAt,
  THRESHOLDS,
} from "../../lib/graphModel";
import { useLibraryGraph } from "../../lib/libraryGraphStore";
import { chooseLayout, useAtlas } from "../../lib/useAtlas";
import { useEdgeCanvas } from "../../lib/useEdgeCanvas";
import { useLayoutTween } from "../../lib/useLayoutTween";
import { clientToView, panBy, zoomAt, type View } from "../../lib/viewport";
import { useLibraries } from "../../lib/libraries";
import { truncate } from "../../lib/radial";
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


/** Zoom bounds. Below 0.35 the graph is a texture; above 6 a single node fills
 *  the canvas and panning becomes the only navigation left. */
const ZOOM = GRAPH_ZOOM;


const FLOORS = [0.5, 0.6, 0.7, 0.8, 0.9] as const;

/** The document view's marks, minus `dashed`: there is one edge type here. */
const OVERVIEW_LEGEND = ["doc", "concept", "weight"] as const;

/** How many nodes of each kind the accessible list renders at first. Growing
 *  this on demand, rather than rendering all 10,835 at once, is what infinite
 *  scroll buys: the keyboard path stays complete without ever laying out a
 *  list item nobody has scrolled to. */
const LIST_CAP = 200;

/** How many more rows a scroll near the bottom of the list reveals. */
const LIST_STEP = 200;

/** Distinct colours the concept-type legend cycles through. Matches the number
 *  of `--graph-type-N` custom properties defined in `styles.css`. */
const TYPE_PALETTE_SIZE = 8;

type Selection =
  | { kind: "doc"; id: string }
  | { kind: "concept"; id: string }
  | null;

interface Placed {
  id: string;
  /** The node's index in the envelope, which is what the position arrays are
   *  keyed by. Carried on the element as `data-node` so the tween can find its
   *  coordinates without a second lookup. */
  index: number;
  kind: "doc" | "concept";
  x: number;
  y: number;
  r: number;
  label: string;
  conceptType: string | null;
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
  active,
}: {
  /** Called when the reader asks to look inside a book. The shell switches to
   *  the document view; this component does not know that view exists. */
  onOpenDocument?: (doc: GraphDocument) => void;
  /** Whether this view is the one on screen. Required and undefaulted, unlike
   *  `GraphScreen`'s: this is the component that spends, and a silent default
   *  is how that gets spent on a tab nobody opened. */
  active: boolean;
}) {
  const { t } = useTranslation();
  const { selected: libraryId } = useLibraries();

  const [minDocuments, setMinDocuments] = useState<number>(DEFAULT_THRESHOLD);
  const [floor, setFloor] = useState<number>(0.6);

  const [query, setQuery] = useState("");
  const [showBooks, setShowBooks] = useState(true);
  const [showConcepts, setShowConcepts] = useState(true);
  const [onlyBook, setOnlyBook] = useState<string | null>(null);
  const [onlyFound, setOnlyFound] = useState(false);
  const [hideIsolated, setHideIsolated] = useState(false);
  // How much of each kind's list is rendered, grown by `LIST_STEP` when the
  // reader scrolls near the bottom rather than fetched — the whole envelope is
  // already in memory, so "loading more" is `slice`, not a request.
  const [listCap, setListCap] = useState(LIST_CAP);
  const [selected, setSelected] = useState<Selection>(null);
  const [hoverLabel, setHoverLabel] = useState<SVGGElement | null>(null);
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });

  const [claims, setClaims] = useState<Claim[] | null>(null);
  const [claimsBusy, setClaimsBusy] = useState(false);

  const canvas = useRef<SVGSVGElement | null>(null);
  const dragging = useRef<{ x: number; y: number; pan: { x: number; y: number } } | null>(null);
  const wheelHandler = useRef<((e: WheelEvent) => void) | null>(null);
  // State, not a ref: the tween below has to re-run when the element arrives,
  // and a ref assignment does not render. Without that the very first
  // transition — the cold default being re-derived from the base — would
  // have nothing recorded to move from, and would snap.
  const [panGroup, setPanGroup] = useState<SVGGElement | null>(null);
  // State rather than refs: the canvas hook has to re-run when they arrive.
  const [stage, setStage] = useState<HTMLDivElement | null>(null);
  const [edgeCanvas, setEdgeCanvas] = useState<HTMLCanvasElement | null>(null);
  const opening = useRef<string | null>(null);

  // -- the data ------------------------------------------------------------

  // One download per (plane, library, floor), kept in memory and shared with
  // whatever else asks for it. The degree threshold is *not* part of that
  // request: it is derived here from the widest envelope, which
  // `graphModel.ts` shows to be the same answer the server would have given.
  // Measured on the real library: the round trip was 183 ms and the derivation
  // below is 0.88 ms.
  const graph = useLibraryGraph({ libraryId: libraryId ?? "", floor, active });
  const { index, adjacency, counts, concepts: conceptsById, documents: documentsById } = graph;
  const busy = graph.status === "loading";
  const error = graph.status === "failed" ? graph.error : null;
  const drawn = counts.find((row) => row.threshold === minDocuments) ?? null;

  // -- the layout ----------------------------------------------------------

  /** Which nodes and edges this threshold draws. Pure, and cheap enough that
   *  the control can move as fast as a pointer. */
  const sub = useMemo(
    () => (index === null ? null : subgraphAt(index, minDocuments)),
    [index, minDocuments],
  );

  // The layouts, computed once per envelope in a worker rather than in this
  // render. `settle` used to run here, synchronously, on every change of
  // threshold: 209 ms at the default and 3,859 ms at the widest, measured on
  // the real library. Now the threshold picks an array that already exists.
  const atlas = useAtlas(index, active);
  const positions = chooseLayout(atlas.layouts, minDocuments);

  const needle = query.trim().toLocaleLowerCase();
  const matches = useMemo(() => {
    if (needle === "" || index === null) return null;
    const ids = new Set<string>();
    for (const i of matchesQuery(index, needle)) ids.add(index.ids[i] as string);
    return ids;
  }, [needle, index]);

  /** Everything the client-side controls decide, in one pass over the
   *  threshold's subgraph. Measured on the real library: 0.88 ms. */
  const shown = useMemo(() => {
    if (index === null || sub === null) return null;
    return visibleIn(index, sub, {
      books: showBooks,
      concepts: showConcepts,
      onlyBook,
      matched: matches,
      onlyFound,
      hideIsolated,
    });
  }, [index, sub, showBooks, showConcepts, onlyBook, matches, onlyFound, hideIsolated]);

  /** What the slider says under itself, live. The counts come out of the
   *  histogram built once per envelope, so moving it derives nothing. */
  const stopLabel = useMemo(() => {
    const total = counts.find((row) => row.threshold === 1)?.concepts ?? 0;
    const params = { n: minDocuments, shown: shown?.concepts ?? 0, total };
    return minDocuments === 1
      ? t("graph.degreeValueAll", params)
      : t("graph.degreeValueSome", params);
  }, [counts, minDocuments, shown, t]);

  const layout = useMemo(() => {
    if (index === null || sub === null || positions === null) return null;
    const placed: Placed[] = [];
    for (const i of sub.nodes) {
      if (shown !== null && shown.nodes[i] === 0) continue;
      const x = positions[i * 2] as number;
      const y = positions[i * 2 + 1] as number;
      // A node the standing layout has no place for is left out rather than
      // drawn at the origin. It arrives when its own threshold does.
      if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
      placed.push({
        id: index.ids[i] as string,
        index: i,
        kind: i < index.docCount ? "doc" : "concept",
        x,
        y,
        // Books are a fixed size: there are dozens of them and they are the
        // landmarks, so varying them buys nothing and costs legibility.
        r: i < index.docCount ? 9 : CONCEPT_RADIUS,
        label: index.label[i] as string,
        conceptType: index.conceptType[i] ?? null,
      });
    }
    return placed;
  }, [index, sub, positions, shown]);

  /** Which nodes the current selection lights up: a book's concepts, or a
   *  concept's books. Null when nothing is selected, which is what tells the
   *  paint code to fade nothing.
   *
   *  Read off the adjacency index rather than by scanning every edge, so this
   *  is proportional to the node's own degree and not to the 18,056 edges the
   *  library holds. */
  const lit = useMemo(() => {
    if (selected === null || index === null || adjacency === null) return null;
    const at = index.ids.indexOf(selected.id);
    if (at < 0) return null;
    const ids = new Set<string>([selected.id]);
    for (const other of neighbours(adjacency, at)) {
      ids.add(index.ids[other] as string);
    }
    return ids;
  }, [selected, index, adjacency]);

  /** The same selection, as a mask the canvas can index by node. Built here
   *  rather than in the painter because the painter takes numbers, not ids. */
  const litMask = useMemo(() => {
    if (lit === null || index === null) return null;
    const mask = new Uint8Array(index.ids.length);
    for (let i = 0; i < index.ids.length; i += 1) {
      if (lit.has(index.ids[i] as string)) mask[i] = 1;
    }
    return mask;
  }, [lit, index]);


  /** Every node the reader could reach right now, which is what the list shows
   *  and what the "N of M" line counts.
   *
   *  **Not filtered by the degree threshold.** The list is the promise that
   *  nothing becomes unreachable for being undrawn, so it holds every concept
   *  in the envelope — including the ones the current threshold leaves off the
   *  canvas. */
  const listed = useMemo(() => {
    if (index === null) return [];
    const rows: { id: string; kind: "doc" | "concept"; label: string; conceptType: string | null }[] = [];
    for (let i = 0; i < index.ids.length; i += 1) {
      const kind = i < index.docCount ? ("doc" as const) : ("concept" as const);
      if (kind === "doc" ? !showBooks : !showConcepts) continue;
      const id = index.ids[i] as string;
      if (matches !== null && !matches.has(id)) continue;
      rows.push({ id, kind, label: index.label[i] as string, conceptType: index.conceptType[i] ?? null });
    }
    rows.sort((a, b) => a.label.localeCompare(b.label));
    return rows;
  }, [index, showBooks, showConcepts, matches]);

  /** Every distinct `conceptType` in the envelope, alphabetised so the colour a
   *  type gets does not depend on the order concepts happened to arrive in —
   *  the same determinism `force.ts` seeds positions for. Palette wraps past
   *  `TYPE_PALETTE_SIZE` rather than growing unboundedly: a corpus with more
   *  than eight named types would stop being a legend and start being noise. */
  const conceptTypeOrder = useMemo(() => {
    if (index === null) return [];
    const seen = new Set<string>();
    for (const type of index.conceptType) {
      if (type !== null) seen.add(type);
    }
    return [...seen].sort((a, b) => a.localeCompare(b));
  }, [index]);

  const typeSlot = useCallback(
    (type: string | null): number | null =>
      type === null ? null : conceptTypeOrder.indexOf(type) % TYPE_PALETTE_SIZE,
    [conceptTypeOrder],
  );

  // A new filter or search is a different list, so the reader should not land
  // on it already scrolled three pages down a list that no longer exists.
  useEffect(() => {
    setListCap(LIST_CAP);
  }, [libraryId, showBooks, showConcepts, needle]);

  const onIndexScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 64) {
      setListCap((c) => c + LIST_STEP);
    }
  }, []);


  // -- interaction ---------------------------------------------------------

  const reset = useCallback(() => {
    setZoom(1);
    setPan({ x: 0, y: 0 });
  }, []);

  /** The current view, read by the imperative handlers without re-subscribing
   *  them on every change. */
  const view = useRef<View>({ zoom: 1, pan: { x: 0, y: 0 } });
  view.current = { zoom, pan };

  const apply = useCallback((next: View) => {
    setZoom(next.zoom);
    setPan(next.pan);
  }, []);

  /** The buttons zoom about the middle of the canvas, through the same function
   *  the wheel uses, so there is one behaviour and one thing to assert. */
  const zoomBy = useCallback(
    (factor: number) => {
      apply(zoomAt(view.current, { x: ATLAS_VIEW.W / 2, y: ATLAS_VIEW.H / 2 }, factor));
    },
    [apply],
  );

  /**
   * The canvas element, and the wheel listener that hangs off it.
   *
   * **A callback ref, not an effect over `canvas.current`.** The effect that
   * used to do this depended on `[zoomBy]`, whose identity never changed, so it
   * ran once at mount — when `data` was still null and the `<svg>` had not been
   * rendered at all, because it sits behind a guard further down. It returned
   * early on a null ref and never ran again, so **the wheel has never zoomed
   * this canvas.** A callback ref cannot be scheduled before its node exists.
   *
   * Still imperative rather than `onWheel`, for the original reason: React's
   * wheel handler is passive and cannot `preventDefault`, so the page would
   * scroll out from under the canvas on every turn of the wheel.
   */
  const attachCanvas = useCallback(
    (node: SVGSVGElement | null) => {
      const previous = canvas.current;
      if (previous !== null && wheelHandler.current !== null) {
        previous.removeEventListener("wheel", wheelHandler.current);
        wheelHandler.current = null;
      }
      canvas.current = node;
      if (node === null) return;
      const onWheel = (e: WheelEvent) => {
        e.preventDefault();
        const rect = node.getBoundingClientRect();
        const at = clientToView(
          { x: e.clientX, y: e.clientY },
          rect,
          { width: ATLAS_VIEW.W, height: ATLAS_VIEW.H },
        );
        apply(zoomAt(view.current, at, e.deltaY < 0 ? ZOOM.STEP : 1 / ZOOM.STEP));
      };
      wheelHandler.current = onWheel;
      node.addEventListener("wheel", onWheel, { passive: false });
    },
    [apply],
  );

  /**
   * Dragging, entirely outside React.
   *
   * This is the line the rest of the screen is drawn along: a *continuous*
   * interaction never goes through state, and a *discrete* one may. A drag
   * fires a pointermove per frame, and each `setPan` re-executed the render
   * function and reconciled every node on the canvas — so the picture moved by
   * rebuilding the tree that draws it. What a frame costs now is one
   * `setAttribute` on the group and one canvas repaint, whatever the graph
   * holds. The state is set once, on release, so React's idea of the view and
   * the DOM's agree again the moment the gesture ends.
   *
   * Zoom stays in state on purpose: it is discrete — a button, a wheel click —
   * and each concept cancels the scale on itself, which is a write per node
   * that a render already does correctly. Making it imperative would mean
   * moving the node radius into a CSS custom property, and whether WebKitGTK
   * resolves `r` from `calc()` is not something this project can find out from
   * a test.
   */
  const drag = useRef({ pan: { x: 0, y: 0 }, frame: 0 });

  const onPointerDown = (e: React.PointerEvent<SVGSVGElement>) => {
    dragging.current = { x: e.clientX, y: e.clientY, pan };
    drag.current.pan = pan;
    e.currentTarget.setPointerCapture?.(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const origin = dragging.current;
    const node = canvas.current;
    if (origin === null || node === null) return;
    // Divided by the drawn scale, because `pan` is applied inside the group
    // whose units are viewBox units. Fed raw client pixels — which is what
    // shipped — a drag ran 1.9x fast at the narrowest two-column width.
    drag.current.pan = panBy(
      view.current,
      { x: e.clientX, y: e.clientY },
      origin,
      node.getBoundingClientRect(),
      { width: ATLAS_VIEW.W, height: ATLAS_VIEW.H },
    );
    if (drag.current.frame !== 0) return;
    // Coalesced: a pointer can outrun the display, and painting twice for one
    // frame is work nobody sees.
    drag.current.frame = requestAnimationFrame(() => {
      drag.current.frame = 0;
      const next = { zoom: view.current.zoom, pan: drag.current.pan };
      panGroup?.setAttribute(
        "transform",
        `translate(${next.pan.x} ${next.pan.y}) scale(${next.zoom})`,
      );
      repaint(next);
    });
  };

  const endDrag = () => {
    if (dragging.current === null) return;
    dragging.current = null;
    if (drag.current.frame !== 0) {
      cancelAnimationFrame(drag.current.frame);
      drag.current.frame = 0;
    }
    // One render at the end, so the tree and the attribute agree again.
    setPan(drag.current.pan);
  };

  /**
   * Hover, delegated, and outside React entirely.
   *
   * Hovering is continuous — a pointer sweeping the canvas crosses dozens of
   * nodes a second — so by the rule this screen is built on it may not touch
   * state. What it costs now is independent of how much the graph holds: a
   * class off the node that was hovered, a class on the one that is, and the
   * overlay's transform and text.
   */
  useLayoutEffect(() => {
    if (panGroup === null || hoverLabel === null) return;
    const text = hoverLabel.querySelector("text");
    let current: SVGGElement | null = null;

    const show = (node: SVGGElement | null) => {
      if (node === current) return;
      current?.classList.remove("is-hovered");
      current = node;
      if (node === null || text === null) {
        hoverLabel.classList.remove("is-showing");
        return;
      }
      node.classList.add("is-hovered");
      text.textContent = node.dataset.label ?? "";
      // Position only: a book's own transform carries no scale, a concept's
      // does, and the overlay's own child group already applies the current
      // 1/zoom counter-scale — copying the node's transform wholesale would
      // leave a hovered book's label scaling with zoom instead of staying a
      // fixed screen size.
      const [translate] = node.getAttribute("transform")?.match(/translate\([^)]*\)/) ?? [];
      hoverLabel.setAttribute("transform", translate ?? "");
      hoverLabel.classList.add("is-showing");
    };

    const onOver = (e: Event) => {
      // Suppressed mid-drag: the pointer crosses everything on the way, and
      // what the reader is doing is moving the picture, not reading a name.
      if (dragging.current !== null) return show(null);
      const target = e.target as Element | null;
      const node = target?.closest?.(".node") as SVGGElement | null;
      // A chosen or matched node already draws its own inline label (below),
      // so showing the overlay too would draw the same name twice on top of
      // itself — this is the bug the two screenshots caught.
      if (node?.classList.contains("is-active") || node?.classList.contains("is-shared")) {
        return show(null);
      }
      show(node ?? null);
    };
    const onLeave = () => show(null);

    panGroup.addEventListener("pointerover", onOver);
    panGroup.addEventListener("pointerleave", onLeave);
    return () => {
      panGroup.removeEventListener("pointerover", onOver);
      panGroup.removeEventListener("pointerleave", onLeave);
      show(null);
    };
  }, [panGroup, hoverLabel, layout]);

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
    (p: Pick<Placed, "id" | "kind">) => {
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

  const activate = (p: Pick<Placed, "id" | "kind">) => (e: React.KeyboardEvent) => {
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

  const { repaint } = useEdgeCanvas({
    canvas: edgeCanvas,
    stage,
    index,
    sub: shown === null ? sub : { nodes: sub?.nodes ?? new Int32Array(0), edges: shown.edges },
    positions,
    lit: litMask,
    view: { zoom, pan },
  });

  // The one movement a reader sees per library: the default view is drawn cold
  // at ~226 ms and re-derived from the base a few seconds later, 63 px away on
  // the real corpus. And every later change of threshold, 40-48 px.
  useLayoutTween({
    group: panGroup,
    positions,
    zoom,
    docCount: index?.docCount ?? 0,
    onFrame: (at) => repaint({ zoom, pan }, at),
  });

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

        <Proposed floor={index?.confidenceFloor ?? floor} />
      </div>

      {/* Everything below decides what is *drawn* and asks the server nothing.
          Separated from the row above, which holds the one control that still
          spends a request, because a reader who cannot tell which is which
          learns to distrust both. */}
      <fieldset className="graph-tuner">
        <legend>{t("graph.viewFilters")}</legend>

        <div className="graph-degree">
          <label htmlFor="graph-degree">{t("graph.degreeLabel")}</label>
          <input
            id="graph-degree"
            className="graph-degree-slider"
            type="range"
            min={0}
            max={THRESHOLDS.length - 1}
            step={1}
            value={Math.max(0, THRESHOLDS.indexOf(minDocuments as (typeof THRESHOLDS)[number]))}
            // The slider's value is the *index* into `THRESHOLDS`, because a
            // range input needs a uniform step and the stops are 1, 2, 3, 5, 10.
            onChange={(e) =>
              setMinDocuments(THRESHOLDS[Number(e.target.value)] ?? DEFAULT_THRESHOLD)
            }
            aria-valuetext={stopLabel}
          />
          {/* Real elements rather than a `<datalist>`: a range input renders
              its tick marks in Blink and not in WebKitGTK, so the labels cannot
              live there. */}
          <ul className="graph-degree-stops" aria-hidden="true">
            {THRESHOLDS.map((n) => (
              <li key={n}>{n === 1 ? t("graph.degreeStopAll") : t("graph.degreeStopEach", { n })}</li>
            ))}
          </ul>
          <output className="graph-degree-count" htmlFor="graph-degree">
            {stopLabel}
          </output>
        </div>

        <label className="field field-inline">
          <span>{t("graph.onlyBook")}</span>
          <select value={onlyBook ?? ""} onChange={(e) => setOnlyBook(e.target.value || null)}>
            <option value="">{t("graph.everyBook")}</option>
            {[...documentsById.values()].map((d) => (
              <option key={d.versionId} value={d.versionId}>
                {d.title ?? d.versionId}
              </option>
            ))}
          </select>
        </label>

        <div className="graph-toggles">
          <label>
            <input
              type="checkbox"
              checked={onlyFound}
              onChange={(e) => setOnlyFound(e.target.checked)}
              disabled={needle === ""}
            />
            {t("graph.searchOnly")}
          </label>
          <label>
            <input
              type="checkbox"
              checked={hideIsolated}
              onChange={(e) => setHideIsolated(e.target.checked)}
            />
            {t("graph.hideIsolated")}
          </label>
          <button type="button" onClick={graph.refresh}>
            {t("graph.refresh")}
          </button>
        </div>
      </fieldset>

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

      {busy && index === null && <p className="muted">{t("graph.loading")}</p>}

      {index !== null && index.docCount === 0 && (
        <p className="muted">{t("graph.libraryEmpty")}</p>
      )}

      {layout !== null && index !== null && sub !== null && index.docCount > 0 && (
        <div className="graph-layout">
          <div className="graph-main">
            {/* The stage establishes the containing block and takes its height
                from the SVG, which stays in flow. Both are positioned: an
                absolutely positioned canvas paints *above* a static sibling, so
                giving only the canvas a position puts it over the nodes and the
                picture becomes a flat wash. */}
            <div className="graph-stage" ref={setStage}>
              <canvas className="graph-edges" aria-hidden="true" ref={setEdgeCanvas} />
            <svg
              ref={attachCanvas}
              className={`graph-canvas ${selected !== null ? "is-probing" : ""}`}
              viewBox={`0 0 ${ATLAS_VIEW.W} ${ATLAS_VIEW.H}`}
              role="group"
              aria-label={t("graph.libraryAlt", {
                books: index.docCount,
                concepts: drawn?.concepts ?? 0,
              })}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endDrag}
              onPointerLeave={endDrag}
            >
              <g
                ref={setPanGroup}
                transform={`translate(${pan.x} ${pan.y}) scale(${zoom})`}
              >
                {/* Edges are painted on the canvas underneath, not drawn
                    here. They carry no click, no hover and no accessible name,
                    so nothing is lost by it — and at the widest threshold this
                    is 17,814 elements that no longer exist. Paint order is
                    still a contract; it is a stacking order now. */}

                {layout.filter((p) => p.kind === "doc").map((p) => {
                  const dimmed = lit !== null && !lit.has(p.id);
                  const found = matches !== null && matches.has(p.id);
                  const chosen = selected?.id === p.id;
                  // Persistent only when chosen or search-matched; otherwise a
                  // book's name appears only on hover, through the delegated
                  // overlay below — a canvas full of titles read as noise once
                  // a library grew past a handful of books.
                  const named = chosen || found;
                  return (
                    <g
                      key={p.id}
                      // The position lives in one `transform`, not spread over
                      // the children's own coordinates. That is what lets the
                      // tween below move a node with a single attribute write,
                      // and it is why the shapes are drawn about the origin.
                      data-node={p.index}
                      data-label={p.label}
                      transform={`translate(${p.x} ${p.y})`}
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
                        x={-p.r}
                        y={-p.r * 0.7}
                        width={p.r * 2}
                        height={p.r * 1.4}
                        rx={2}
                      />
                      {named && (
                        <text y={-p.r - 4} textAnchor="middle">
                          {truncate(p.label, 24)}
                        </text>
                      )}
                    </g>
                  );
                })}
                {layout.filter((p) => p.kind === "concept").map((p) => {
                  const dimmed = lit !== null && !lit.has(p.id);
                  const found = matches !== null && matches.has(p.id);
                  const chosen = selected?.id === p.id;
                  const slot = typeSlot(p.conceptType);
                  // The parent scales the position; this inverse scale keeps
                  // the node and its label at their screen-space size.
                  //
                  // No hover handlers here. Every `onMouseEnter` was a
                  // `setState`, so sweeping the pointer across the canvas
                  // re-rendered the whole screen once per node crossed — and
                  // during a drag it did that *as well as* the pan. One
                  // delegated listener on the layer does the same work in four
                  // attribute writes, and the name goes in a single overlay.
                  return (
                    <g
                      key={p.id}
                      data-node={p.index}
                      data-label={p.label}
                      className={`node concept ${chosen ? "is-active" : ""} ${
                        found ? "is-shared" : ""
                      } ${dimmed && !found ? "is-faded" : ""} ${
                        slot !== null ? `type-${slot}` : ""
                      }`}
                      transform={`translate(${p.x} ${p.y}) scale(${1 / zoom})`}
                      role="button"
                      tabIndex={-1}
                      aria-label={p.label}
                      aria-pressed={chosen}
                      onClick={() => pick(p)}
                      onKeyDown={activate(p)}
                    >
                      <title>{p.label}</title>
                      <circle className="shape" r={CONCEPT_RADIUS} />
                      {(chosen || found) && (
                        <text
                          className="concept-label"
                          x={CONCEPT_RADIUS + 8}
                          dominantBaseline="middle"
                        >
                          {truncate(p.label, 24)}
                        </text>
                      )}
                    </g>
                  );
                })}

                {/* The hovered node's full name. One element for the whole
                    canvas, moved and filled imperatively — see the delegated
                    listener above. Painted last, or a concept drawn after it
                    would cover the label of a hovered book, and a neighbouring
                    concept would cover the label of a hovered concept. Inside
                    the panned group so it travels with the picture, and
                    inverse-scaled so it keeps its size on screen regardless of
                    which kind of node it is labelling. */}
                <g className="graph-hover" ref={setHoverLabel} aria-hidden="true">
                  <g transform={`scale(${1 / zoom})`}>
                    <text className="concept-label" x={CONCEPT_RADIUS + 8} dominantBaseline="middle" />
                  </g>
                </g>
              </g>
            </svg>
            </div>

            <p className="graph-inspector">
              {t("graph.libraryCounts", {
                books: shown?.books ?? index.docCount,
                concepts: shown?.concepts ?? drawn?.concepts ?? 0,
                edges: shown?.edges.length ?? drawn?.edges ?? 0,
              })}
              {/* Per threshold, not copied from the envelope: a view whose rows
                  all fit is complete even when the response as a whole was
                  cut, and saying otherwise puts "recortado" on a whole
                  picture. */}
              {((drawn?.truncated ?? false) || graph.truncatedDocuments) &&
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
                      lit === null ? 0 : Math.max(0, lit.size - 1),
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
                  {[...(lit ?? [])]
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
                  {[...(lit ?? [])]
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
                    shown:
                      Math.min(listed.filter((p) => p.kind === "doc").length, listCap) +
                      Math.min(listed.filter((p) => p.kind === "concept").length, listCap),
                    total: listed.length,
                  })}
                </p>
                {conceptTypeOrder.length > 0 && (
                  <ul className="graph-legend graph-type-legend">
                    {conceptTypeOrder.map((type, i) => (
                      <li key={type}>
                        <span
                          className={`dot node-dot type-${i % TYPE_PALETTE_SIZE}`}
                          aria-hidden="true"
                        />
                        <span>{type}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {/* Grown by `LIST_STEP` on scroll rather than paginated with
                    buttons: the whole envelope is already in memory, so there
                    is nothing to wait for and no page number worth naming. */}
                <div className="graph-index-groups" onScroll={onIndexScroll}>
                  {(["doc", "concept"] as const).map((kind) => {
                    const rows = listed.filter((p) => p.kind === kind).slice(0, listCap);
                    if (rows.length === 0) return null;
                    return (
                      <div className="graph-index-group" key={kind}>
                        <h4 className="graph-index-heading">
                          {t(kind === "doc" ? "graph.overviewLegend.doc" : "graph.overviewLegend.concept")}
                        </h4>
                        <ul className="graph-index">
                          {rows.map((p) => {
                            const slot = typeSlot(p.conceptType);
                            return (
                              <li key={p.id}>
                                <button type="button" onClick={() => pick(p)}>
                                  <span
                                    className={`dot node-dot node-${p.kind} ${
                                      slot !== null ? `type-${slot}` : ""
                                    }`}
                                    aria-hidden="true"
                                  />
                                  {p.label}
                                </button>
                              </li>
                            );
                          })}
                        </ul>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
