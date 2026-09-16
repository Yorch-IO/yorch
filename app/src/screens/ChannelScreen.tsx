import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { EstimateTable } from "../GateReview";
import {
  api,
  errorGuidanceKey,
  errorMessage,
  DEFAULT_STAGES,
  type ChannelDetail,
  type ChannelReading,
  type StageOptions,
  type Synthesis,
  type VideoGateReport,
} from "../lib/api";
import {
  buildRows,
  filterRows,
  overCap,
  probeBudget,
  PROBE_POLL_MS,
  seedFromDiscovery,
  selectable,
  toProbe,
  unpickOnGate,
  type Row,
} from "../lib/channel";
import { useChannels } from "../lib/channels";
import { titleKeywords, topicOf } from "../lib/keywords";
import { ChannelCandidates, KeywordChips } from "./ChannelCandidates";
import { ChannelSynthesis } from "./ChannelSynthesis";
import { exportName, toCsv, toJson } from "../lib/synthesisExport";

/** What a channel run does *not* get, whatever the document defaults say.
 *
 *  A video run has no profile, eval-set or tuning stage at all, so offering
 *  them would quote work that cannot happen. `extractSemantics` is deliberately
 *  **not** on this list — the video path grew a semantics stage, and forcing it
 *  off is what made the first real video indexed, citable and invisible on the
 *  Graph screen. It is a switch here instead, ticked *before* the probes run so
 *  that each gate quotes the choice rather than being approved past it. */
const VIDEO_STAGES: Partial<StageOptions> = {
  learnProfile: false,
  generateEvalset: false,
  tune: false,
  ignoreProfile: false,
};

/**
 * Read a YouTube channel, and decide what of it is worth indexing.
 *
 * The channel itself is picked — and synced — in the app's top bar, because a
 * channel *is* a library and that is where the library is picked on every
 * other tab. What is left here is three acts, and each button is a decision
 * somebody makes with a figure in front of them:
 *
 * 1. **Topic** — typed, or seeded from the words the channel's own titles
 *    repeat. The keyword chips are free and also narrow the table, so a
 *    person can shortlist a channel without paying for the metadata pass.
 *    *Discover* is one cheap call per twenty-five titles over the filtered
 *    set, quoted first; its verdict is a *hypothesis about a title*, never
 *    proof, and it pre-ticks the rows it finds.
 * 2. **Probe and read** — a free video run per ticked video: resolves,
 *    downloads the captions, groups them, previews the chunks, quotes the bill
 *    and parks at its own gate. Then one call per probed video over the
 *    **uncorrected** transcript, which is the first pass that has read the
 *    words. Quoted with act 1.
 * 3. **Ask** — retrieval and a five-section synthesis over what is indexed.
 *
 * The tick is one choice for the whole flow: what to probe, and then what to
 * approve. Approving signals each run's own gate. **The batch is a sum on a
 * screen and not a gate of its own**: every video is already a run with its
 * own estimate, its own artifacts and its own seven-day gate, so a
 * half-finished batch leaves the rest parked rather than lost, and re-running
 * it costs nothing — an already-indexed video short-circuits at `registering`
 * and spends $0.
 */
export function ChannelScreen() {
  const { t } = useTranslation();
  const { selected: channelId } = useChannels();

  const [detail, setDetail] = useState<ChannelDetail | null>(null);

  const [topic, setTopic] = useState("");
  const [keys, setKeys] = useState<Set<string>>(new Set());
  const [limit, setLimit] = useState(100);
  const [deepLimit, setDeepLimit] = useState(10);
  const [quote, setQuote] = useState<ChannelReading["estimate"] | null>(null);
  const [quoteFor, setQuoteFor] = useState<{ evaluated: number; read: number } | null>(
    null,
  );

  const [discovery, setDiscovery] = useState<ChannelReading | null>(null);
  const [reading, setReading] = useState<ChannelReading | null>(null);
  const [runs, setRuns] = useState<Record<string, string>>({});
  const [gates, setGates] = useState<Record<string, VideoGateReport>>({});
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [semantics, setSemantics] = useState(false);

  const [synthesis_, setSynthesis] = useState<Synthesis | null>(null);
  const [effort, setEffort] = useState("thorough");
  const [saved, setSaved] = useState<string | null>(null);

  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState<string>("");
  const [error, setError] = useState<unknown>(null);
  // A ref, not state: the poll below must not re-subscribe when an answer lands.
  const alive = useRef(true);

  const stages = (): StageOptions => ({
    ...DEFAULT_STAGES,
    ...VIDEO_STAGES,
    extractSemantics: semantics,
  });

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Changing channel throws away everything derived from the previous one.
  // Carrying a verdict or a probe across would attach one channel's reading to
  // another channel's video, which is the shape of mistake nothing downstream
  // can see. The keyword filter goes too: it was computed from the other
  // channel's titles.
  useEffect(() => {
    setDetail(null);
    setDiscovery(null);
    setReading(null);
    setRuns({});
    setGates({});
    setPicked(new Set());
    setKeys(new Set());
    unpicked.current.clear();
    seededFrom.current = null;
    setQuote(null);
    setQuoteFor(null);
    setSynthesis(null);
    setSaved(null);
    if (!channelId) return;
    let cancelled = false;
    void (async () => {
      try {
        const d = await api.channelDetail(channelId);
        if (!cancelled) setDetail(d);
      } catch (e) {
        if (!cancelled) setError(e);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [channelId]);

  // The quote is the figure the person sees before the request that starts the
  // run, and it is priced from exactly what would be judged and read. Any of
  // these four changes what that is — the filter most of all — so the quote
  // goes, and Discover and Read stay disabled until it is asked for again.
  // Asking again is free.
  useEffect(() => {
    setQuote(null);
    setQuoteFor(null);
  }, [topic, keys, limit, deepLimit]);

  /** Poll the probes until each has parked at its gate.
   *
   *  The same shape as `useImportQueue`: a gate already fetched is not
   *  re-asked, because a run parked for seven days cannot change its report,
   *  and a failure to read one run is swallowed so the rest still arrive. */
  useEffect(() => {
    const pending = Object.values(runs).filter((id) => !(id in gates));
    if (pending.length === 0) return;
    let stop = false;
    const tick = async () => {
      for (const workflowId of pending) {
        try {
          const report = await api.videoGate(workflowId);
          if (stop || report === null) continue;
          setGates((current) => ({ ...current, [workflowId]: report }));
        } catch {
          // A probe that failed keeps its run row and its error in the import
          // queue, which is where a failed run is read. Dropping it here would
          // stop the other probes arriving.
        }
      }
    };
    void tick();
    const timer = window.setInterval(() => void tick(), PROBE_POLL_MS);
    return () => {
      stop = true;
      window.clearInterval(timer);
    };
  }, [runs, gates]);

  const rows: Row[] = buildRows(
    detail,
    discovery?.preselection ?? null,
    reading?.topics ?? null,
    runs,
    gates,
  );
  const keywords = useMemo(
    () => titleKeywords((detail?.videos ?? []).map((v) => v.title)),
    [detail],
  );
  const visible = filterRows(rows, keys);
  const budget = probeBudget(deepLimit, runs);

  // Discover's verdicts tick their rows once per discovery, and after that the
  // selection is the person's: a verdict is a hypothesis about a title, and a
  // second seeding on a later render would undo a tick they removed on purpose.
  const seededFrom = useRef<string | null>(null);
  useEffect(() => {
    const preselection = discovery?.preselection;
    if (!preselection || !discovery || seededFrom.current === discovery.workflowId) return;
    if (rows.length === 0) return;
    seededFrom.current = discovery.workflowId;
    const add = seedFromDiscovery(rows, preselection, budget);
    if (add.size === 0) return;
    setPicked((current) => new Set([...current, ...add]));
  }, [discovery, rows, budget]);

  // Each row's gate is read once, the moment *it* arrives, and takes the tick
  // away from a video with no captions — not once for the whole table, because
  // the probes park one at a time and reading only the first would leave the
  // later ones ticked at twelve times the price; and not on every render, which
  // would undo a person who re-ticked one on purpose with the figure in front
  // of them.
  const unpicked = useRef(new Set<string>());
  useEffect(() => {
    const fresh = rows.filter(
      (r) => r.gate !== null && !unpicked.current.has(r.video.videoId),
    );
    if (fresh.length === 0) return;
    for (const row of fresh) unpicked.current.add(row.video.videoId);
    const drop = unpickOnGate(fresh);
    if (drop.size === 0) return;
    setPicked((current) => new Set([...current].filter((id) => !drop.has(id))));
  }, [rows]);

  const guard = async (label: string, fn: () => Promise<void>) => {
    setBusy(true);
    setStep(label);
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
      setStep("");
    }
  };

  /** The chips are a filter *and* a starting point for the topic. Toggling
   *  rewrites the topic field from the selection; typing in it afterwards is
   *  free and leaves the chips alone, because the chips decide what is on the
   *  table and the topic decides what the model is asked. */
  const toggleKey = (key: string) => {
    const next = new Set(keys);
    if (!next.delete(key)) next.add(key);
    setKeys(next);
    setTopic(topicOf(keywords.filter((k) => next.has(k.key)).map((k) => k.label)));
  };
  const clearKeys = () => {
    setKeys(new Set());
    setTopic("");
  };

  /** The ids the quote and the run are restricted to, or nothing when no chip
   *  is pressed — the server reads an absent list as the whole catalogue. A
   *  row shown only because it carries a run is not in the judged set: it is
   *  already probed. */
  const filteredIds = (): string[] | undefined =>
    keys.size === 0
      ? undefined
      : visible.filter((r) => selectable(r) && r.runId === null).map((r) => r.video.videoId);

  const getQuote = () =>
    guard("quote", async () => {
      const q = await api.channelQuote(
        channelId,
        topic.trim(),
        limit,
        deepLimit,
        filteredIds(),
      );
      setQuote(q.estimate);
      setQuoteFor({ evaluated: q.evaluated, read: q.read });
    });

  /** Run a channel workflow to completion and read back what it wrote.
   *
   *  Read from the run's artifacts rather than from the workflow's return
   *  value, because Temporal forgets a run when its retention expires and this
   *  is the record a person comes back to. */
  const awaitRun = async (workflowId: string): Promise<ChannelReading> => {
    for (;;) {
      const state = await api.runStatus(workflowId);
      // `null` means nobody could say — never "failed". A run whose history has
      // aged out, or a Temporal that blinked, must read as "keep waiting".
      if (state.state && state.state !== "running") break;
      await new Promise((r) => setTimeout(r, 2000));
    }
    return api.channelReading(channelId, workflowId);
  };

  const discover = () =>
    guard("discover", async () => {
      const started = await api.channelDiscover(
        channelId,
        topic.trim(),
        limit,
        deepLimit,
        filteredIds(),
      );
      setDiscovery(await awaitRun(started.workflowId));
    });

  const probe = () =>
    guard("probe", async () => {
      const wanted = toProbe(visible, picked, budget);
      const started: Record<string, string> = {};
      const refused: string[] = [];
      // Sequentially, and not only for tidiness: the caption endpoint was
      // measured refusing this address six times across twenty-five minutes,
      // and a caption download that gives up is the branch that pays Amazon.
      for (const row of wanted) {
        try {
          const run = await api.videoStart(
            { libraryId: detail!.libraryId, url: row.video.url, title: row.video.title },
            stages(),
            () => {},
          );
          started[row.video.videoId] = run.workflowId;
        } catch (e) {
          refused.push(`${row.video.title}: ${errorMessage(e)}`);
        }
      }
      setRuns((current) => ({ ...current, ...started }));
      // Per-item failures do not fail the batch, the same rule the Import
      // screen's own batch follows.
      if (refused.length > 0 && Object.keys(started).length === 0) {
        throw new Error(refused.join("\n"));
      }
    });

  const read = () =>
    guard("read", async () => {
      const started = await api.channelTopics(
        channelId,
        topic.trim(),
        Object.values(runs),
      );
      setReading(await awaitRun(started.workflowId));
    });

  const approve = () =>
    guard("approve", async () => {
      for (const row of rows) {
        if (row.runId === null || row.gate === null) continue;
        const wanted = picked.has(row.video.videoId);
        await api.ingestApprove(row.runId, {
          approved: wanted,
          // The recommendation per row, echoed back: the probe knows whether
          // this video's captions are automatic, which is what decides whether
          // correction is worth its bill. The stage switches come from the
          // options the gate was *quoted* with, so nothing runs that was not
          // priced.
          options: {
            ...stages(),
            correct: row.gate.recommended?.correct ?? false,
            embed: row.gate.recommended?.embed ?? true,
          },
          reason: wanted ? "" : t("channel.notChosen"),
        });
      }
      // Answered gates are gone; the runs live on in the import queue.
      setRuns({});
      setGates({});
      setPicked(new Set());
      unpicked.current.clear();
      const d = await api.channelDetail(channelId);
      if (alive.current) setDetail(d);
    });

  /** Ask this channel's indexed sermons. Two calls, like `/ask`: the second
   *  collects, because a real question once outran a 180 s client timeout, was
   *  computed, was billed, and was reported as an unreachable API. */
  const ask = () =>
    guard("ask", async () => {
      setSynthesis(null);
      setSaved(null);
      const started = await api.channelAsk(channelId, topic.trim(), effort);
      for (;;) {
        const out = await api.channelAskResult(channelId, started.questionId);
        if (out.state === "done" && out.synthesis) {
          setSynthesis(out.synthesis);
          return;
        }
        if (out.state === "failed") {
          throw new Error(out.error?.message ?? out.state);
        }
        await new Promise((r) => setTimeout(r, 2000));
      }
    });

  const save = (kind: "json" | "csv") =>
    guard("save", async () => {
      if (!synthesis_) return;
      const body = kind === "csv" ? toCsv(synthesis_) : toJson(synthesis_);
      // `null` means the person dismissed the dialog, which is the ordinary way
      // to leave one and must not paint a red panel.
      setSaved(await api.saveText(exportName(synthesis_.topic, kind), body));
    });

  const ready = detail !== null && topic.trim().length >= 3;
  const probable = toProbe(visible, picked, budget).length;
  const beyond = overCap(visible, picked, budget);
  const approvable = Object.keys(gates).length > 0;

  return (
    <section className="screen">
      <h2>{t("channel.title")}</h2>
      <p className="intro">{t("channel.intro")}</p>

      {error !== null && (
        <div className="error">
          <strong>{t("error.title")}</strong>
          {errorGuidanceKey(error) && <p>{t(errorGuidanceKey(error)!)}</p>}
          <pre className="detail">{errorMessage(error)}</pre>
          <button type="button" onClick={() => setError(null)}>
            {t("error.dismiss")}
          </button>
        </div>
      )}

      {!detail && !channelId && <p className="muted">{t("channel.none")}</p>}

      {detail && (
        <div className="channel-columns">
          {/* Left: the videos. The chips narrow the table, and the table is
              where the tick lives — what to probe and then what to approve. */}
          <div className="channel-videos">
            <KeywordChips
              keywords={keywords}
              selected={keys}
              onToggle={toggleKey}
              onClear={clearKeys}
            />
            {keys.size > 0 && (
              <p className="muted channel-filtered">
                {t("channel.filtered", { shown: visible.length, total: rows.length })}
              </p>
            )}
            <ChannelCandidates
              rows={visible}
              keys={keys}
              picked={picked}
              onPicked={setPicked}
              busy={busy}
            />
            <div className="actions">
              <button type="button" disabled={busy || !approvable} onClick={approve}>
                {busy && step === "approve"
                  ? t("channel.approving")
                  : t("channel.approve", { count: picked.size })}
              </button>
            </div>
            <p className="caveat">{t("channel.approveCaveat")}</p>
          </div>

          {/* Right: everything that decides, in the order the work goes. */}
          <div className="channel-controls">
            <div className="panel">
              <h3>{t("channel.topic")}</h3>
              <p className="caveat">{t("channel.topicCaveat")}</p>
              <label className="field">
                <span>{t("channel.topicLabel")}</span>
                <input
                  type="text"
                  value={topic}
                  placeholder={t("channel.topicPlaceholder")}
                  onChange={(e) => setTopic(e.target.value)}
                />
              </label>
              <p className="caveat">{t("channel.topicFromKeywords")}</p>
              <div className="channel-limits">
                <label className="field">
                  <span>{t("channel.limit")}</span>
                  <input
                    type="number"
                    min={1}
                    max={500}
                    value={limit}
                    onChange={(e) => setLimit(Number(e.target.value) || 1)}
                  />
                </label>
                <label className="field">
                  <span>{t("channel.deepLimit")}</span>
                  <input
                    type="number"
                    min={0}
                    max={25}
                    value={deepLimit}
                    onChange={(e) => setDeepLimit(Number(e.target.value) || 0)}
                  />
                </label>
              </div>
              {/* The cap is the caption throttle, not the bill: a batch wide
                  enough to meet it turns a free pass into a transcription
                  nobody asked for. */}
              <p className="caveat">{t("channel.deepLimitCaveat")}</p>

              <div className="actions">
                <button type="button" disabled={busy || !ready} onClick={getQuote}>
                  {t("channel.quote")}
                </button>
                <button
                  type="button"
                  disabled={busy || !ready || quote === null}
                  onClick={discover}
                >
                  {busy && step === "discover"
                    ? t("channel.discovering")
                    : t("channel.discover")}
                </button>
              </div>

              {quote && quoteFor && (
                <>
                  <p>{t("channel.quoteFor", quoteFor)}</p>
                  <EstimateTable estimate={{ ...quote, stages: quote.stages }} />
                  <p className="caveat">{t("channel.quoteCaveat")}</p>
                </>
              )}
              {discovery?.preselection && discovery.preselection.invented > 0 && (
                <p className="warn">
                  {t("channel.invented", { count: discovery.preselection.invented })}
                </p>
              )}
              {discovery?.preselection && discovery.preselection.unevaluated > 0 && (
                <p className="warn">
                  {t("channel.unevaluated", {
                    count: discovery.preselection.unevaluated,
                  })}
                </p>
              )}
            </div>

            <div className="panel">
              <h3>{t("channel.probe")}</h3>
              <p className="caveat">{t("channel.probeCaveat")}</p>
              <fieldset className="stages">
                <legend>{t("channel.stages")}</legend>
                <label htmlFor="channel-semantics">
                  <input
                    id="channel-semantics"
                    type="checkbox"
                    checked={semantics}
                    disabled={busy || Object.keys(runs).length > 0}
                    onChange={() => setSemantics((s) => !s)}
                  />
                  <span>{t("channel.stageSemantics")}</span>
                </label>
              </fieldset>
              {/* Ticked before the probes, never after: each probe's gate quotes
                  the options its run was started with, and approving past a
                  quote is the under-reporting failure this product refuses
                  outright. */}
              <p className="caveat">{t("channel.stageSemanticsCaveat")}</p>
              <div className="actions">
                <button type="button" disabled={busy || probable === 0} onClick={probe}>
                  {busy && step === "probe"
                    ? t("channel.probing")
                    : t("channel.probeStart", { count: probable })}
                </button>
                <button
                  type="button"
                  disabled={busy || !approvable || quote === null}
                  onClick={read}
                >
                  {busy && step === "read" ? t("channel.reading_") : t("channel.readStart")}
                </button>
              </div>
              {beyond > 0 && (
                <p className="warn">{t("channel.overCap", { count: beyond })}</p>
              )}
            </div>

            <div className="panel">
              <h3>{t("channel.ask")}</h3>
              <p className="caveat">{t("channel.askCaveat")}</p>
              <label className="field">
                <span>{t("channel.effort")}</span>
                <select value={effort} onChange={(e) => setEffort(e.target.value)}>
                  {["brief", "standard", "thorough"].map((level) => (
                    <option key={level} value={level}>
                      {t(`ask.effort.${level}`, { defaultValue: level })}
                    </option>
                  ))}
                </select>
              </label>
              <div className="actions">
                <button type="button" disabled={busy || !ready} onClick={ask}>
                  {busy && step === "ask" ? t("channel.asking") : t("channel.askStart")}
                </button>
                <button
                  type="button"
                  disabled={busy || synthesis_ === null}
                  onClick={() => save("json")}
                >
                  {t("channel.saveJson")}
                </button>
                <button
                  type="button"
                  disabled={busy || synthesis_ === null}
                  onClick={() => save("csv")}
                >
                  {t("channel.saveCsv")}
                </button>
              </div>
              {saved && <p className="notice">{t("channel.saved", { path: saved })}</p>}
            </div>

            {synthesis_ && <ChannelSynthesis result={synthesis_} />}
          </div>
        </div>
      )}
    </section>
  );
}
