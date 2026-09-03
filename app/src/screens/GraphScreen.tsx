import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";

import type { GraphDocument } from "../lib/api";
import { DocumentGraph } from "./graph/DocumentGraph";
import { LibraryGraph } from "./graph/LibraryGraph";

/**
 * Two views of the same graph, and which one you get is a navigation decision
 * rather than a setting.
 *
 * The overview opens on the whole library — every book, every concept that
 * joins two of them — and answers "what is in here, and what connects?".
 * Selecting a book there and pressing "ver este libro" hands its version to the
 * document view, which is the radial picture that already existed: one book at
 * the centre, its concepts on an inner ring, the documents it shares them with
 * on an outer one. That view answers "what is *this* about, and who else says
 * it?", which the overview cannot: at 1,700 concepts the neighbourhood of one
 * book is a smudge.
 *
 * **Both stay mounted**, for the same reason the tabs do. Re-entering the
 * overview must not re-run the simulation, and leaving it must not throw away a
 * pan the reader spent time on. `hidden` is what keeps them alive, and nothing
 * in the stylesheet may set `display` on these wrappers.
 */
type Mode = "overview" | "document";

export function GraphScreen({ active = true }: { active?: boolean } = {}) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<Mode>("overview");
  const [focus, setFocus] = useState<{ versionId: string; title: string } | null>(
    null,
  );

  const open = useCallback((doc: GraphDocument) => {
    setFocus({ versionId: doc.versionId, title: doc.title ?? doc.versionId });
    setMode("document");
  }, []);

  return (
    <section className="screen graph">
      <h2>{t("graph.title")}</h2>
      <p className="intro">
        {mode === "overview" ? t("graph.libraryIntro") : t("graph.intro")}
      </p>

      <div className="actions graph-modes">
        <button
          type="button"
          className={mode === "overview" ? "active" : ""}
          aria-pressed={mode === "overview"}
          onClick={() => setMode("overview")}
        >
          {t("graph.modeLibrary")}
        </button>
        <button
          type="button"
          className={mode === "document" ? "active" : ""}
          aria-pressed={mode === "document"}
          onClick={() => setMode("document")}
        >
          {t("graph.modeDocument")}
        </button>
      </div>

      <div hidden={mode !== "overview"}>
        {/* Only the view that is showing, on the tab that is showing. Both
            views stay mounted for the reason above, which is exactly why the
            expensive one has to be told when nobody is looking. `GraphScreen`
            defaults to active so a test that renders it alone still mounts. */}
        <LibraryGraph onOpenDocument={open} active={active && mode === "overview"} />
      </div>
      <div hidden={mode !== "document"}>
        <DocumentGraph focus={focus} />
      </div>
    </section>
  );
}
