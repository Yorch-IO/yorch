import { useCallback, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorGuidanceKey,
  errorMessage,
  type ChunkOverrideRow,
  type ChunkRow,
  type EditOutcome,
} from "../lib/api";

/**
 * Rewriting a chunk, hiding it from retrieval, or undoing either.
 *
 * **What this gives up, on the one screen where somebody does it.** An edited
 * chunk's text is no longer a byte-exact slice of any stream this product
 * holds, so it has no verifiable `char_span` — the property the engine is
 * built around. The panel says so rather than letting a person discover it
 * from a citation that stopped printing a byte range.
 *
 * Three verbs, and they are genuinely different. *Hiding* needs no new words
 * and breaks no span: it is the answer to a table of contents indexed as
 * chapters. *Rewriting* does both. *Undoing* deletes the override, which is
 * why the original is never overwritten anywhere and an undo costs nothing but
 * the re-embedding of what the run already produced.
 *
 * `busy` is cleared in a `finally` on every path, for the recorded reason: a
 * button latched on a request that can fail is a panel nobody can retry.
 */
export function ChunkEditor({
  chunk,
  versionId,
  override,
  onDone,
}: {
  chunk: ChunkRow;
  versionId: string;
  override: ChunkOverrideRow | undefined;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(chunk.text);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [outcome, setOutcome] = useState<EditOutcome | null>(null);

  const disabled = override?.disabled ?? false;
  const edited = (override?.text ?? null) !== null;

  const apply = useCallback(
    async (args: { text?: string | null; disabled?: boolean }) => {
      setBusy(true);
      try {
        setOutcome(
          await api.exploreEditChunk({
            versionId,
            chunkIndex: chunk.ordinal,
            ...args,
          }),
        );
        setError(null);
        onDone();
      } catch (e) {
        setError(e);
      } finally {
        setBusy(false);
      }
    },
    [versionId, chunk.ordinal, onDone],
  );

  const guidance = error ? errorGuidanceKey(error) : undefined;

  return (
    <div className="chunk-editor">
      <div className="chunk-editor-actions">
        <button
          className="link"
          disabled={busy}
          onClick={() => {
            setDraft(chunk.text);
            setOpen((v) => !v);
          }}
        >
          {open ? t("edit.close") : t("edit.rewrite")}
        </button>
        <button
          className="link"
          disabled={busy}
          onClick={() =>
            void apply({ text: override?.text ?? null, disabled: !disabled })
          }
        >
          {disabled ? t("edit.show") : t("edit.hide")}
        </button>
        {(edited || disabled) && (
          <button
            className="link"
            disabled={busy}
            onClick={() => void apply({ text: null, disabled: false })}
          >
            {t("edit.undo")}
          </button>
        )}
      </div>

      {open && (
        <>
          {/* Said before the edit, not discovered from a citation afterwards. */}
          <p className="warn">{t("edit.spanWarning")}</p>
          <label className="field">
            <span>{t("edit.text")}</span>
            <textarea
              rows={6}
              value={draft}
              disabled={busy}
              onChange={(e) => setDraft(e.target.value)}
            />
          </label>
          <div className="actions">
            <button
              disabled={busy || !draft.trim() || draft === chunk.text}
              onClick={() => void apply({ text: draft, disabled })}
            >
              {busy ? t("edit.saving") : t("edit.save")}
            </button>
          </div>
        </>
      )}

      {error !== null && (
        <div className="error">
          <strong>{t("edit.failed")}</strong>
          {guidance && <p>{t(guidance)}</p>}
          <p className="detail">{errorMessage(error)}</p>
        </div>
      )}

      {outcome && error === null && (
        <p className="notice">
          {t("edit.done", { usd: outcome.usd.toFixed(6) })}
          {/* A quote that no longer checks out costs the claim its span, not
              its existence — and this is how a reader learns it happened. */}
          {outcome.claimsUnverified > 0 &&
            " " + t("edit.claimsLostTheirSpan", { n: outcome.claimsUnverified })}
        </p>
      )}
    </div>
  );
}
