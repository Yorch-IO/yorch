import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Locator } from "../Locator";
import { Passage } from "./AskScreen";
import {
  api,
  controlErrorKind,
  errorGuidanceKey,
  errorMessage,
  isAppError,
  type Conversation,
  type ConversationTurn,
} from "../lib/api";
import { ASK_EFFORTS, loadEffort, saveEffort, type AskEffort } from "../lib/askEffort";
import {
  actionFor,
  chatReducer,
  citedPassage,
  citedTurnOf,
  emptyChat,
  loadSelected,
  saveSelected,
} from "../lib/chatSession";
import { useBackend } from "../lib/backend";
import { useLibraries } from "../lib/libraries";

/**
 * Conversations: asking that remembers what was already asked.
 *
 * The one thing to understand before changing anything here: **what streams is
 * a draft.** Prose arrives token by token while the answer is being written,
 * and the `done` event carries the settled turn — which can be a refusal, with
 * an empty answer, after a paragraph has been on screen for ten seconds.
 * `citas` is the last field in the answering schema, so verification cannot run
 * until the envelope closes. `chatSession`'s reducer is where that replacement
 * happens and where it is asserted; this component only has to not fight it.
 *
 * The transcript is the server's. There is no client-side history here — unlike
 * the Ask screen, whose entries live in localStorage because a question's
 * answer exists only in Temporal until its retention expires. A conversation's
 * turns are catalog rows, so the only thing worth remembering locally is which
 * conversation you were reading.
 */

/** How a turn's state reads in the transcript. */
const STATE_KEY: Record<string, string> = {
  answered: "chat.answered",
  insufficient_evidence: "chat.insufficient",
  off_corpus: "chat.offCorpus",
  failed: "chat.failed",
};

export function ChatScreen() {
  const { t } = useTranslation();
  const { selected: libraryId } = useLibraries();
  // The screen is remounted when the plane changes (see `App`), so the identity
  // is read once and the lazy initialiser below cannot see a stale one.
  const { identity } = useBackend();

  const [state, dispatch] = useReducer(chatReducer, emptyChat);
  const [effort, setEffort] = useState<AskEffort>(() => loadEffort(identity));
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [confirming, setConfirming] = useState<string | null>(null);

  const busy = state.streaming !== null;
  const transcript = useRef<HTMLDivElement | null>(null);

  // -- loading ------------------------------------------------------------

  const refresh = useCallback(async () => {
    try {
      const { conversations } = await api.chatList();
      dispatch({ type: "conversations", conversations });
      return conversations;
    } catch (e) {
      setError(e);
      return [] as Conversation[];
    }
  }, []);

  const open = useCallback(async (conversationId: string) => {
    dispatch({ type: "select", conversationId });
    try {
      const detail = await api.chatRead(conversationId);
      dispatch({ type: "open", conversationId, turns: detail.turnsDetail });
    } catch (e) {
      setError(e);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const rows = await refresh();
      if (cancelled) return;
      // Reopen what was being read, but only if it is still there and still
      // belongs to the shelf the picker is on — a remembered id from another
      // library would silently show a transcript the picker says nothing about.
      const remembered = loadSelected(identity);
      const match = rows.find((c) => c.id === remembered && c.libraryId === libraryId);
      if (match) void open(match.id);
    })();
    return () => {
      cancelled = true;
    };
  }, [refresh, open, identity, libraryId]);

  // Only a *chosen* conversation is remembered, never the absence of one.
  //
  // This effect and the restore above both run after mount, and this one is
  // synchronous while that one awaits a fetch — so writing `null` here ran
  // first and deleted the id before the restore could read it. The feature
  // never worked, and no test could see it: nothing in the suite remounts the
  // screen with a populated localStorage. Found by relaunching the real window.
  //
  // Clearing is therefore explicit, in `startNew`, which is the one gesture
  // that means "no conversation" rather than "not yet".
  useEffect(() => {
    if (state.selected !== null) saveSelected(state.selected, identity);
  }, [state.selected, identity]);

  // The transcript grows downwards, so a new turn is off-screen without this.
  // `scrollIntoView` is absent in jsdom rather than throwing, so it is called
  // optionally and a test asserts the click does not throw.
  useEffect(() => {
    transcript.current?.scrollTo?.({ top: transcript.current.scrollHeight });
  }, [state.turns]);

  // -- asking -------------------------------------------------------------

  const follow = useCallback(async (conversationId: string, seq: number) => {
    try {
      await api.chatStream(conversationId, seq, 0, (event) => {
        const action = actionFor(event, seq);
        if (action) dispatch(action);
      });
    } catch (e) {
      // The stream broke; the turn itself is still landing in the catalog. Say
      // so rather than implying the answer was lost — and never ask again,
      // which would pay for it twice.
      dispatch({
        type: "failed",
        seq,
        // The error *kind*, not its guidance key: this is stored on the turn and
        // is what the transcript keys advice on later. `errorGuidanceKey` maps a
        // kind to a phrase and would have put the phrase where the tag belongs.
        kind: controlErrorKind(e) ?? (isAppError(e) ? e.kind : "chat_failed"),
        message: errorMessage(e),
      });
    }
    void refresh();
  }, [refresh]);

  const send = useCallback(async () => {
    const text = draft.trim();
    if (!text || busy || !libraryId) return;
    setError(null);
    setDraft("");
    try {
      let conversationId = state.selected;
      if (conversationId === null) {
        const started = await api.chatCreate(libraryId);
        conversationId = started.conversationId;
        dispatch({ type: "select", conversationId });
        dispatch({ type: "open", conversationId, turns: [] });
      }
      const { turnSeq } = await api.chatTurn(conversationId, text, effort);
      dispatch({ type: "submit", seq: turnSeq, question: text, effort });
      await follow(conversationId, turnSeq);
    } catch (e) {
      setError(e);
      // Put the question back rather than losing what somebody typed.
      setDraft(text);
    }
  }, [draft, busy, libraryId, state.selected, effort, follow]);

  const startNew = useCallback(() => {
    dispatch({ type: "select", conversationId: null });
    saveSelected(null, identity);
    setError(null);
  }, [identity]);

  const remove = useCallback(
    async (conversationId: string) => {
      setConfirming(null);
      try {
        await api.chatDelete(conversationId);
        if (state.selected === conversationId) startNew();
        await refresh();
      } catch (e) {
        setError(e);
      }
    },
    [state.selected, startNew, refresh],
  );

  // -- rendering ----------------------------------------------------------

  const shelf = state.conversations.filter((c) => c.libraryId === libraryId);
  const cited = citedTurnOf(state);
  const passage = citedPassage(cited, state.citation);
  const guidance = error ? errorGuidanceKey(error) : null;

  return (
    <section className="screen chat">
      <h2>{t("chat.title")}</h2>
      <p className="intro">{t("chat.intro")}</p>

      <div className="chat-columns">
        <div className="pane pane-conversations">
          <h3>{t("chat.conversations")}</h3>
          <button type="button" className="secondary" onClick={startNew} disabled={busy}>
            {t("chat.new")}
          </button>
          {shelf.length === 0 ? (
            <p className="notice">{t("chat.listEmpty")}</p>
          ) : (
            <ul className="history">
              {shelf.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    className={`link${c.id === state.selected ? " active" : ""}`}
                    onClick={() => void open(c.id)}
                    disabled={busy}
                  >
                    {c.title || t("chat.untitled")}
                  </button>
                  <span className="muted small">
                    {t("chat.turnCount", { count: c.turns })}
                  </span>
                  {confirming === c.id ? (
                    <span className="confirm">
                      <button type="button" className="danger small" onClick={() => void remove(c.id)}>
                        {t("chat.confirmDelete")}
                      </button>
                      <button type="button" className="link small" onClick={() => setConfirming(null)}>
                        {t("chat.cancel")}
                      </button>
                    </span>
                  ) : (
                    <button
                      type="button"
                      className="link small"
                      onClick={() => setConfirming(c.id)}
                      disabled={busy}
                    >
                      {t("chat.delete")}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="pane pane-transcript">
          <div className="transcript" ref={transcript}>
            {state.turns.length === 0 ? (
              <p className="notice">{t("chat.nothingYet")}</p>
            ) : (
              state.turns.map((turn) => (
                <Turn
                  key={turn.seq}
                  turn={turn}
                  streaming={state.streaming?.seq === turn.seq ? state.streaming : null}
                  onCitations={() => dispatch({ type: "citation", turnSeq: turn.seq, index: 0 })}
                />
              ))
            )}
          </div>

          {error !== null && (
            <div className="error">
              <p>{t("chat.failed")}</p>
              {guidance && <p className="guidance">{t(guidance)}</p>}
              <pre className="detail">{errorMessage(error)}</pre>
            </div>
          )}

          <label className="field">
            <span>{t("chat.message")}</span>
            <textarea
              value={draft}
              rows={3}
              placeholder={t("chat.placeholder")}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                // Enter sends, shift+enter breaks the line. A composer that
                // needed the mouse for every turn would not be a conversation.
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  void send();
                }
              }}
              disabled={busy || !libraryId}
            />
          </label>

          <fieldset className="stages">
            <legend>{t("chat.effortLegend")}</legend>
            {ASK_EFFORTS.map((level) => (
              <label key={level}>
                <input
                  type="radio"
                  name="chat-effort"
                  value={level}
                  checked={effort === level}
                  onChange={() => {
                    setEffort(level);
                    saveEffort(level, identity);
                  }}
                  disabled={busy}
                />
                <span>{t(`ask.effort.${level}`)}</span>{" "}
                <span className="muted small">{t(`ask.effortHint.${level}`)}</span>
              </label>
            ))}
          </fieldset>

          <button type="button" onClick={() => void send()} disabled={busy || !draft.trim() || !libraryId}>
            {busy ? t("chat.sending") : t("chat.send")}
          </button>
        </div>

        <div className="pane pane-citations">
          <h3>{t("chat.citations")}</h3>
          {cited === null || cited.citations.length === 0 ? (
            <p className="notice">{t("chat.noCitations")}</p>
          ) : (
            <>
              <ol className="citations">
                {cited.citations.map((c, i) => (
                  <li key={`${c.chunkId}:${i}`}>
                    <button
                      type="button"
                      className={`link${i === state.citation ? " active" : ""}`}
                      onClick={() => dispatch({ type: "citation", turnSeq: cited.seq, index: i })}
                    >
                      {c.claim}
                    </button>
                    <Locator text={c.locator} className="locator" />
                  </li>
                ))}
              </ol>
              {passage ? <Passage item={passage} /> : <p className="notice">{t("chat.noPassage")}</p>}
            </>
          )}
        </div>
      </div>
    </section>
  );
}

/**
 * What the turn is doing, while it is doing it.
 *
 * Falls back to the generic line when no stage has arrived yet — the first
 * event still has to cross a worker, a table and an SSE hop — and when a worker
 * newer than this build names a stage it has no label for. `defaultValue` is
 * what makes the second case a readable sentence rather than a raw key, which
 * is the failure the sidebar showed when an eighth tab arrived without one.
 */
function Progress({
  streaming,
}: {
  streaming: {
    stage: string | null;
    evidence: { chunks: number; dense: number | null } | null;
  } | null;
}) {
  const { t } = useTranslation();
  const stage = streaming?.stage;
  const evidence = streaming?.evidence;

  // `dense` absent means the count was never measured, which is not the same as
  // none clearing the floor — a zero is a real fact about the corpus and says
  // so, where a missing figure must simply not be printed.
  const label =
    stage === "evidence" && evidence
      ? evidence.dense === null
        ? t("chat.stage.evidenceUnmeasured", { chunks: evidence.chunks })
        : t("chat.stage.evidence", { chunks: evidence.chunks, dense: evidence.dense })
      : stage
        ? t(`chat.stage.${stage}`, { defaultValue: t("chat.thinking") })
        : t("chat.thinking");

  return (
    <p className="progress-stage">
      <span className="thinking">{label}</span>
      {evidence && stage !== "evidence" && (
        <span className="muted small">
          {evidence.dense === null
            ? t("chat.stage.evidenceUnmeasured", { chunks: evidence.chunks })
            : t("chat.stage.evidence", { chunks: evidence.chunks, dense: evidence.dense })}
        </span>
      )}
    </p>
  );
}

/** The advice that goes with an error kind, when there is any. */
function Guidance({ kind }: { kind: string }) {
  const { t } = useTranslation();
  const key = errorGuidanceKey({ kind, message: "" });
  return key ? <p className="guidance">{t(key)}</p> : null;
}

/** One exchange. */
function Turn({
  turn,
  streaming,
  onCitations,
}: {
  turn: ConversationTurn;
  /** Non-null only for the turn being answered right now. */
  streaming: {
    stage: string | null;
    evidence: { chunks: number; dense: number | null } | null;
  } | null;
  onCitations: () => void;
}) {
  const { t } = useTranslation();
  const stateKey = STATE_KEY[turn.state];

  return (
    <article className={`turn ${turn.state}`}>
      <p className="asked">{turn.question}</p>

      {/* The substitution, shown rather than hidden. A follow-up is answered
          against a question the person did not type, and an answer that quietly
          addresses something adjacent is indistinguishable from a bad answer
          unless this is visible. Only when it actually differs — on a first
          turn the rewrite makes no call and the two are equal. */}
      {turn.searched && turn.searched !== turn.question && (
        <p className="searched">{t("chat.searchedAs", { question: turn.searched })}</p>
      )}

      {turn.state === "running" ? (
        <>
          {/* Where it has got to, which is what covers the wait: streamed prose
              was measured at about 8% of a turn. Shown only until prose starts
              arriving — a stage label under text that is already growing is
              answering a question the reader can now see for themselves. */}
          {turn.answer === "" && <Progress streaming={streaming} />}
          {turn.answer !== "" && <p className="prose streaming">{turn.answer}</p>}
        </>
      ) : (
        <>
          {stateKey && <p className="muted small">{t(stateKey)}</p>}
          {turn.answer ? (
            <p className="prose">{turn.answer}</p>
          ) : (
            <>
              <p className="reason">{turn.error?.message || t("chat.noAnswer")}</p>
              {/* A failed turn stored its kind, so the advice that goes with it
                  is available here rather than only on the live error panel —
                  which is gone as soon as the next turn is asked. */}
              {turn.error && <Guidance kind={turn.error.kind} />}
            </>
          )}
          {turn.citations.length > 0 && (
            <button type="button" className="link small" onClick={onCitations}>
              {t("chat.showCitations", { count: turn.citations.length })}
            </button>
          )}
        </>
      )}
      {streaming !== null && turn.answer !== "" && (
        <span className="muted small">{t("chat.writing")}</span>
      )}
    </article>
  );
}
