import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { EstimateTable } from "../GateReview";
import {
  api,
  errorGuidanceKey,
  errorMessage,
  DEFAULT_STAGES,
  type ChannelDetail,
  type ChannelReading,
  type ChannelSummary,
  type StageOptions,
  type Synthesis,
  type VideoGateReport,
} from "../lib/api";
import {
  buildRows,
  initialPicked,
  PROBE_POLL_MS,
  toProbe,
  type Row,
} from "../lib/channel";
import { ChannelCandidates } from "./ChannelCandidates";
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
 * Four acts, and each button is a decision somebody makes with a figure in
 * front of them:
 *
 * 1. **Sync** — the Data API catalogue. Costs quota, never money.
 * 2. **Discover** — one cheap call per twenty-five titles, quoted first.
 *    Its verdict is a *hypothesis about a title*, never proof.
 * 3. **Probe** — a free video run per candidate: resolves, downloads the
 *    captions, groups them, previews the chunks, quotes the bill and parks at
 *    its own gate. Nothing here spends.
 * 4. **Read** — one call per probed video over the **uncorrected** transcript,
 *    which is the first pass that has read the words. Quoted with act 2.
 *
 * Then the table, one checkbox a row and one total, and approving it signals
 * each run's own gate. **The batch is a sum on a screen and not a gate of its
 * own**: every video is already a run with its own estimate, its own artifacts
 * and its own seven-day gate, so a half-finished batch leaves the rest parked
 * rather than lost, and re-running it costs nothing — an already-indexed video
 * short-circuits at `registering` and spends $0.
 */
export function ChannelScreen() {
  const { t } = useTranslation();

  const [channels, setChannels] = useState<ChannelSummary[]>([]);
  const [channelId, setChannelId] = useState<string>("");
  const [detail, setDetail] = useState<ChannelDetail | null>(null);
  const [url, setUrl] = useState("");

  const [topic, setTopic] = useState("");
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

  const loadChannels = useCallback(async () => {
    try {
      const list = await api.channels();
      if (!alive.current) return;
      setChannels(list.channels);
      setChannelId((current) => current || (list.channels[0]?.channel.channelId ?? ""));
    } catch (e) {
      if (alive.current) setError(e);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    void loadChannels();
    return () => {
      alive.current = false;
    };
  }, [loadChannels]);

  // Changing channel throws away everything derived from the previous one.
  // Carrying a verdict or a probe across would attach one channel's reading to
  // another channel's video, which is the shape of mistake nothing downstream
  // can see.
  useEffect(() => {
    setDetail(null);
    setDiscovery(null);
    setReading(null);
    setRuns({});
    setGates({});
    setPicked(new Set());
    seeded.current.clear();
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

  // Each row is ticked once, the moment *its own* gate arrives.
  //
  // Not once for the whole table: the probes park one at a time and the poll
  // records each gate as it lands, so seeding on the first would leave every
  // later row unticked — with no way to tell that from a row somebody had
  // deliberately cleared. And not on every render either, which would undo a
  // person's own choice on the next poll. A row is seeded exactly once, and
  // after that the selection is theirs.
  const seeded = useRef(new Set<string>());
  useEffect(() => {
    const fresh = rows.filter(
      (r) => r.gate !== null && !seeded.current.has(r.video.videoId),
    );
    if (fresh.length === 0) return;
    for (const row of fresh) seeded.current.add(row.video.videoId);
    const add = initialPicked(fresh);
    if (add.size === 0) return;
    setPicked((current) => new Set([...current, ...add]));
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

  const sync = () =>
    guard("sync", async () => {
      const summary = await api.channelSync(url.trim(), 500);
      await loadChannels();
      setChannelId(summary.channel.channelId);
      setUrl("");
    });

  const getQuote = () =>
    guard("quote", async () => {
      const q = await api.channelQuote(channelId, topic.trim(), limit, deepLimit);
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
      );
      setDiscovery(await awaitRun(started.workflowId));
    });

  const probe = () =>
    guard("probe", async () => {
      const wanted = toProbe(rows, discovery?.preselection ?? null, deepLimit);
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
      seeded.current.clear();
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
  const probable = toProbe(rows, discovery?.preselection ?? null, deepLimit).length;

  return (
    <section className="screen">
      <h2>{t("channel.title")}</h2>
      <p className="intro">{t("channel.intro")}</p>

      <div className="panel">
        <h3>{t("channel.sync")}</h3>
        <p className="caveat">{t("channel.syncCaveat")}</p>
        <div className="field-inline">
          <input
            type="text"
            value={url}
            placeholder={t("channel.urlPlaceholder")}
            onChange={(e) => setUrl(e.target.value)}
          />
          <button type="button" disabled={busy || url.trim() === ""} onClick={sync}>
            {busy && step === "sync" ? t("channel.syncing") : t("channel.syncStart")}
          </button>
        </div>

        {channels.length > 0 && (
          <label className="field">
            <span>{t("channel.pick")}</span>
            <select value={channelId} onChange={(e) => setChannelId(e.target.value)}>
              {channels.map((c) => (
                <option key={c.channel.channelId} value={c.channel.channelId}>
                  {c.channel.title} · {t("channel.videoCount", { count: c.videoCount })}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {detail && (
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
          {/* The cap is the caption throttle, not the bill: a batch wide enough
              to meet it turns a free pass into a transcription nobody asked
              for. */}
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
              <EstimateTable
                estimate={{ ...quote, stages: quote.stages }}
              />
              <p className="caveat">{t("channel.quoteCaveat")}</p>
            </>
          )}
        </div>
      )}

      {discovery?.preselection && (
        <div className="panel">
          <h3>{t("channel.probe")}</h3>
          <p className="caveat">{t("channel.probeCaveat")}</p>
          {discovery.preselection.invented > 0 && (
            <p className="warn">
              {t("channel.invented", { count: discovery.preselection.invented })}
            </p>
          )}
          {discovery.preselection.unevaluated > 0 && (
            <p className="warn">
              {t("channel.unevaluated", {
                count: discovery.preselection.unevaluated,
              })}
            </p>
          )}
          <fieldset className="stages">
            <legend>{t("channel.stages")}</legend>
            <label>
              <input
                type="checkbox"
                checked={semantics}
                disabled={busy || Object.keys(runs).length > 0}
                onChange={() => setSemantics((s) => !s)}
              />
              <span>{t("channel.stageSemantics")}</span>
            </label>
          </fieldset>
          {/* Ticked before the probes, never after: each probe's gate quotes the
              options its run was started with, and approving past a quote is
              the under-reporting failure this product refuses outright. */}
          <p className="caveat">{t("channel.stageSemanticsCaveat")}</p>
          <div className="actions">
            <button type="button" disabled={busy || probable === 0} onClick={probe}>
              {busy && step === "probe"
                ? t("channel.probing")
                : t("channel.probeStart", { count: probable })}
            </button>
            <button
              type="button"
              disabled={busy || Object.keys(gates).length === 0}
              onClick={read}
            >
              {busy && step === "read" ? t("channel.reading_") : t("channel.readStart")}
            </button>
          </div>
        </div>
      )}

      {rows.length > 0 && detail && (
        <>
          <ChannelCandidates
            rows={rows}
            picked={picked}
            onPicked={setPicked}
            busy={busy}
          />
          <div className="actions">
            <button
              type="button"
              disabled={busy || Object.keys(gates).length === 0}
              onClick={approve}
            >
              {busy && step === "approve"
                ? t("channel.approving")
                : t("channel.approve", { count: picked.size })}
            </button>
          </div>
          <p className="caveat">{t("channel.approveCaveat")}</p>
        </>
      )}

      {detail && (
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
      )}

      {synthesis_ && <ChannelSynthesis result={synthesis_} />}

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
    </section>
  );
}
