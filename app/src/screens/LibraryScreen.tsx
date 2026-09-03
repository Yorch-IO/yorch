import { Fragment, useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { VersionAudit } from "../RunAudit";

import {
  api,
  errorMessage,
  type DocumentDetail,
  type DocumentRow,
  type Removal,
  type RunScores,
} from "../lib/api";
import { useLibraries } from "../lib/libraries";

/**
 * The Library, and the three verbs it owes the user.
 *
 * They cost three different things, which is why they are three buttons and not
 * one menu. **Eliminar** is free and irreversible. **Reindexar** re-runs the
 * whole pipeline and lands on the normal approval gate, which re-quotes before
 * spending. **Reconstruir** replays artifacts already paid for, so only
 * embedding can spend.
 *
 * Deletion confirms inline rather than through the dialog plugin. Capabilities
 * are deny-by-default and the grant is deliberately minimal; asking for
 * `dialog:allow-confirm` would widen the app's permission surface to save a
 * component. It also lets the confirmation name what is about to be destroyed
 * *and* what survives, which "are you sure?" cannot.
 */
/** What one version's index can be asked.
 *
 * Rendered only when a run measured it, never as zeros: every version on this
 * installation predates the stage, and "recall 0.00" would send somebody to fix
 * an index that is fine.
 *
 * The five figures go together on purpose. `recallAt5` alone flatters the index,
 * because the questions were generated *from* the chunks they must find and
 * therefore leak vocabulary to the lexical leg — the gap to `recallAt5DenseOnly`
 * is that leakage. And the noise floor is what a *wrong* answer scores, without
 * which a reader cannot tell an index that discriminates from one that returns
 * everything at a similar distance.
 */
function Scores({ scores }: { scores: RunScores }) {
  const { t } = useTranslation();
  const pct = (n: number) => `${(n * 100).toFixed(0)}%`;
  return (
    <dl className="scores">
      <div>
        <dt>{t("library.scores.recall5")}</dt>
        <dd>{pct(scores.recallAt5)}</dd>
      </div>
      <div>
        <dt>{t("library.scores.recall1")}</dt>
        <dd>{pct(scores.recallAt1)}</dd>
      </div>
      <div>
        <dt>{t("library.scores.mrr")}</dt>
        <dd>{scores.mrrAt10.toFixed(3)}</dd>
      </div>
      <div>
        <dt>{t("library.scores.denseOnly")}</dt>
        <dd>{pct(scores.recallAt5DenseOnly)}</dd>
      </div>
      <div>
        <dt>{t("library.scores.noiseFloor")}</dt>
        <dd>{scores.noiseFloor.toFixed(3)}</dd>
      </div>
      <p className="muted basis">
        {t("library.scores.basis", {
          questions: scores.evalQuestions,
          chunks: scores.chunks,
          misses: scores.misses,
        })}
      </p>
    </dl>
  );
}

export function LibraryScreen() {
  const { t } = useTranslation();

  const { selected: libraryId } = useLibraries();
  const [rows, setRows] = useState<DocumentRow[]>([]);
  // A file that disappeared keeps its history but should not clutter the shelf.
  // Showing them is how a user finds a document whose folder was unmounted,
  // which is otherwise indistinguishable from one never imported.
  const [includeAbsent, setIncludeAbsent] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  /** Which version's audit is open, if any. One at a time: two ledgers side by
   *  side in a table cell is unreadable, and the question is about one version. */
  const [auditing, setAuditing] = useState<string | null>(null);
  const [removed, setRemoved] = useState<Removal | null>(null);
  const [started, setStarted] = useState<{ kind: string; id: string } | null>(
    null,
  );

  const load = useCallback(async () => {
    // No library selected yet — either the list is still loading or it failed.
    // Requesting anyway builds `/libraries//documents`, whose error names an
    // empty id and sends the reader looking for the wrong fault.
    if (!libraryId) {
      setRows([]);
      return;
    }
    setBusy(true);
    try {
      const library = await api.libraryDocuments(libraryId, includeAbsent);
      setRows(library.documents);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }, [libraryId, includeAbsent]);

  useEffect(() => {
    void load();
  }, [load]);

  const open = useCallback(
    async (id: string) => {
      // Collapsing clears the confirmation too: a confirm left standing behind
      // a closed row is one an unrelated click could answer.
      if (openId === id) {
        setOpenId(null);
        setDetail(null);
        setConfirming(null);
        return;
      }
      setOpenId(id);
      setDetail(null);
      setConfirming(null);
      setRemoved(null);
      try {
        setDetail(await api.documentDetail(libraryId, id));
      } catch (e) {
        setError(errorMessage(e));
      }
    },
    [libraryId, openId],
  );

  const remove = useCallback(
    async (documentId: string) => {
      setBusy(true);
      try {
        const result = await api.documentRemove(libraryId, documentId);
        setRemoved(result);
        setConfirming(null);
        setOpenId(null);
        setDetail(null);
        await load();
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [libraryId, load],
  );

  const removeVersion = useCallback(
    async (versionId: string) => {
      setBusy(true);
      try {
        setRemoved(await api.versionRemove(libraryId, versionId));
        if (openId) setDetail(await api.documentDetail(libraryId, openId));
        await load();
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [libraryId, load, openId],
  );

  /** Promote a version that is indexed and not the active one.
   *
   *  Two things reach it: an ingest that withheld activation on a structural
   *  collision, and a plain rollback — the previous version stays in the graph
   *  deactivated rather than deleted, precisely so a bad re-index can be undone
   *  by flipping the flag instead of paying for the pipeline again.
   *
   *  Not behind a confirm, unlike removal: this is reversible by pressing the
   *  same button on the other version. */
  const activateVersion = useCallback(
    async (versionId: string) => {
      setBusy(true);
      try {
        await api.versionActivate(libraryId, versionId);
        if (openId) setDetail(await api.documentDetail(libraryId, openId));
        await load();
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [libraryId, load, openId],
  );

  const run = useCallback(
    async (kind: "reindex" | "rebuild", documentId: string) => {
      setBusy(true);
      try {
        const started =
          kind === "reindex"
            ? await api.documentReindex(libraryId, documentId)
            : await api.documentRebuild(libraryId, documentId);
        setStarted({ kind, id: started.workflowId });
        setError(null);
      } catch (e) {
        setError(errorMessage(e));
      } finally {
        setBusy(false);
      }
    },
    [libraryId],
  );

  return (
    <section className="screen">
      <h2>{t("library.title")}</h2>
      <p className="intro">{t("library.intro")}</p>

      <label className="inline">
        <input
          type="checkbox"
          checked={includeAbsent}
          onChange={() => setIncludeAbsent((v) => !v)}
        />
        <span>{t("library.includeAbsent")}</span>
      </label>

      <button type="button" onClick={() => void load()} disabled={busy}>
        {busy ? t("library.loading") : t("library.refresh")}
      </button>

      {error && <pre className="detail">{error}</pre>}

      {started && (
        <p className="notice">
          {t(
            started.kind === "reindex"
              ? "library.startedReindex"
              : "library.startedRebuild",
            {
              id: started.id,
            },
          )}
        </p>
      )}

      {removed && (
        <div className="notice removed">
          <p>
            {t("library.removed", {
              points: removed.qdrantPoints,
              chunks: removed.graph.chunks ?? 0,
              versions: removed.versionsRemoved.length,
            })}
          </p>
          {removed.versionsKept.length > 0 && (
            <p>
              {t("library.removedKeptVersions", {
                count: removed.versionsKept.length,
              })}
            </p>
          )}
          {/* Stated, not implied: an irreversible act reported only as "done"
              leaves the user guessing whether their cost history went too. */}
          <p className="model">{t("library.removedKept")}</p>
        </div>
      )}

      {rows.length === 0 && !busy ? (
        <p className="notice">{t("library.empty")}</p>
      ) : (
        <table className="documents">
          <thead>
            <tr>
              <th>{t("library.columns.title")}</th>
              <th>{t("library.columns.format")}</th>
              <th>{t("library.columns.state")}</th>
              <th>{t("library.columns.source")}</th>
              <th>{t("library.columns.actions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((d) => (
              // A keyed Fragment, not `<>`: a shorthand fragment cannot take a key,
              // and each document renders two sibling rows.
              <Fragment key={d.id}>
                <tr className={d.present ? "" : "absent"}>
                  <td>{d.title}</td>
                  <td>{d.format}</td>
                  <td>
                    {!d.present
                      ? t("library.state.absent")
                      : d.activeVersionId
                        ? t("library.state.indexed")
                        : t("library.state.pending")}
                  </td>
                  <td className="model">{d.sourceKey}</td>
                  <td>
                    <button type="button" onClick={() => void open(d.id)}>
                      {t(
                        openId === d.id
                          ? "library.actions.hide"
                          : "library.actions.details",
                      )}
                    </button>
                  </td>
                </tr>

                {openId === d.id && (
                  <tr className="detail-row">
                    <td colSpan={5}>
                      {!detail ? (
                        <p className="notice">{t("library.loading")}</p>
                      ) : confirming === d.id ? (
                        <div className="confirm">
                          <h4>
                            {t("library.confirm.title", {
                              title: detail.title,
                            })}
                          </h4>
                          <p>
                            {t("library.confirm.body", {
                              versions: detail.versions.filter(
                                (v) => v.alsoHeldBy.length === 0,
                              ).length,
                            })}
                          </p>
                          <p className="model">{t("library.confirm.kept")}</p>
                          <button
                            type="button"
                            className="danger"
                            disabled={busy}
                            onClick={() => void remove(d.id)}
                          >
                            {t("library.confirm.yes")}
                          </button>
                          <button
                            type="button"
                            onClick={() => setConfirming(null)}
                          >
                            {t("library.confirm.no")}
                          </button>
                        </div>
                      ) : (
                        <div className="verbs">
                          <div className="actions">
                            <button
                              type="button"
                              disabled={busy || !detail.canReindex}
                              title={
                                detail.canReindex
                                  ? ""
                                  : t("library.cannotReindex")
                              }
                              onClick={() => void run("reindex", d.id)}
                            >
                              {t("library.actions.reindex")}
                            </button>
                            <button
                              type="button"
                              disabled={busy || !detail.canRebuild}
                              title={
                                detail.canRebuild
                                  ? ""
                                  : t("library.cannotRebuild")
                              }
                              onClick={() => void run("rebuild", d.id)}
                            >
                              {t("library.actions.rebuild")}
                            </button>
                            <button
                              type="button"
                              className="danger"
                              disabled={busy}
                              onClick={() => setConfirming(d.id)}
                            >
                              {t("library.actions.remove")}
                            </button>
                          </div>

                          {!detail.canReindex && (
                            <p className="warn">{t("library.cannotReindex")}</p>
                          )}
                          {!detail.canRebuild && (
                            <p className="warn">{t("library.cannotRebuild")}</p>
                          )}

                          <h4>{t("library.detail.versions")}</h4>
                          <ul className="versions">
                            {detail.versions.map((v) => (
                              <li key={v.id}>
                                <span className="model">{v.id}</span>{" "}
                                <span>{v.state}</span>
                                {v.active && (
                                  <strong>
                                    {" "}
                                    · {t("library.detail.active")}
                                  </strong>
                                )}
                                {v.alsoHeldBy.length > 0 && (
                                  <span>
                                    {" "}
                                    ·{" "}
                                    {t("library.detail.sharedWith", {
                                      count: v.alsoHeldBy.length,
                                    })}
                                  </span>
                                )}
                                {!v.rebuildRunId && (
                                  <span>
                                    {" "}
                                    · {t("library.detail.noArtifacts")}
                                  </span>
                                )}
                                {!v.active && v.state === "indexed" && (
                                  <button
                                    type="button"
                                    disabled={busy}
                                    onClick={() => void activateVersion(v.id)}
                                  >
                                    {t("library.detail.activateVersion")}
                                  </button>
                                )}
                                <button
                                  type="button"
                                  disabled={busy}
                                  onClick={() => void removeVersion(v.id)}
                                >
                                  {t("library.detail.removeVersion")}
                                </button>
                                {/* The index status a person opens to ask what
                                    actually happened: every stage, its duration,
                                    its charges, and underneath them the raw
                                    workflow history. */}
                                <button
                                  type="button"
                                  onClick={() =>
                                    setAuditing(auditing === v.id ? null : v.id)
                                  }
                                >
                                  {auditing === v.id
                                    ? t("library.detail.hideAudit")
                                    : t("library.detail.showAudit")}
                                </button>
                                {v.scores && <Scores scores={v.scores} />}
                                {auditing === v.id && <VersionAudit versionId={v.id} />}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
