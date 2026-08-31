"""Runtime configuration, read once from the environment.

Two rules this module exists to enforce:

* **The workspace is the working directory.** ``docagent`` resolves
  ``profiles/``, ``cache/`` and ``state/`` relative to the process CWD, so
  chdir'ing into the workspace relocates all three with no change to its code.
  ``configure()`` does that, which is why it must run before any docagent import
  touches those paths.
* **Secrets never travel through Temporal.** Workflow history is persisted to
  Postgres and kept for the namespace's whole retention period, so an API key
  that entered a workflow argument would outlive the run that used it by days.
  Keys are read here, from a file the app writes 0600 and mounts read-only, and
  reach activities as process state. Workflows pass a ``provider_id`` and
  nothing else.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass, field

#: The shape `graph.schema` mints and the query binder accepts.
_TENANT_ID = re.compile(r"tnt_[0-9a-f]{24}")

DEFAULT_SECRETS_FILE = "/run/secrets/providers.env"


def _env(key: str, default: str) -> str:
    value = os.environ.get(key, "").strip()
    return value or default


@dataclass(frozen=True)
class Paths:
    """Everything the pipeline writes, rooted at the mounted workspace volume."""

    root: pathlib.Path

    def for_tenant(self, tenant: str) -> "Paths":
        """This organisation's corner of the volume — its *inbox*, not its artifacts.

        The legacy tenant keeps the root itself, and that is not a shortcut: a
        `run_artifact` row stores a **workspace-relative** path, so rerooting an
        existing corpus would invalidate every one of them at once — the same
        reasoning that keeps its derived ids unsalted. Everything minted since
        lands under `tenants/<id>/`.

        **What this scopes is the inbox.** `stage_source` calls `contains()`
        before it hashes, so a `source_path` must fall inside the caller's own
        tree; that is the boundary this function exists to draw, and the second
        clause of `contains()` is what keeps the legacy root from swallowing it.

        **Run artifacts are not scoped by it.** This docstring used to claim the
        opposite — that rows written under a tenant were relative to that tenant's
        root — and reading it that way is how a migration on 2026-08-31 moved 34
        run directories out of reach of the code that opens them, leaving 157 of
        159 `chunks`/`semantics` files unreachable and `rebuild` broken until they
        were moved back. All twelve sites that build an `ArtifactStore` pass
        `settings.workspace`, so `ArtifactStore._relative` measures from the
        volume root and a run's artifacts live at `<workspace>/runs/<run_id>/…`
        whoever owns them. `test_a_reference_is_relative_to_the_workspace` is what
        fixes that, and it is the behaviour to trust over any prose.

        That leaves a known isolation gap, the third beside `profiles/` and the
        correction cache: one organisation's run artifacts sit where another could
        read them. Unlike the cache — content-addressed, so sharing an entry means
        already holding that paragraph — this one is a real crossing. Closing it is
        those twelve call sites plus moving the artifacts each organisation already
        has, and it has not been judged to block anything yet.
        """
        from .graph.schema import LEGACY_TENANT_ID

        if tenant == LEGACY_TENANT_ID:
            return self
        if not _TENANT_ID.fullmatch(tenant):
            # A path segment built from an unchecked string is how a workspace
            # gets escaped. Refused here rather than at the filesystem.
            raise ValueError(f"not a tenant id: {tenant!r}")
        return Paths(root=self.root / "tenants" / tenant)

    def contains(self, path: pathlib.Path) -> bool:
        """Whether `path` is inside this organisation's tree.

        Used to refuse an ingest whose `source_path` points somewhere else —
        including another tenant's inbox, which is the case a plain "is it under
        the workspace" check would wave through.

        **The legacy tenant needs the second clause, and it is not symmetry for
        its own sake.** Its root is the volume itself, so `tenants/<other>/…` is
        under it: without excluding that subtree the one organisation that
        predates tenancy could read every organisation that came after. A test
        found this after the first version shipped the containment check alone.
        """
        try:
            resolved = path.resolve()
            root = self.root.resolve()
            if not resolved.is_relative_to(root):
                return False
            # Everything below `tenants/` belongs to somebody more specific.
            others = root / "tenants"
            return not resolved.is_relative_to(others)
        except (OSError, ValueError):
            return False

    @property
    def inbox(self) -> pathlib.Path:
        return self.root / "inbox"

    @property
    def runs(self) -> pathlib.Path:
        return self.root / "runs"

    @property
    def profiles(self) -> pathlib.Path:
        return self.root / "profiles"

    @property
    def cache(self) -> pathlib.Path:
        return self.root / "cache"

    @property
    def snapshots(self) -> pathlib.Path:
        return self.root / "snapshots"

    def run_dir(self, workflow_id: str) -> pathlib.Path:
        """Artifacts for one run.

        ``workflow_id`` is used as a path segment, so it must not be able to
        escape the runs directory — an id of ``../../etc`` would otherwise write
        outside the volume. Ids are generated by the API, but this is the last
        place that can cheaply refuse a bad one.
        """
        if not workflow_id or "/" in workflow_id or "\\" in workflow_id or workflow_id.startswith("."):
            raise ValueError(f"unusable workflow id for a path segment: {workflow_id!r}")
        return self.runs / workflow_id

    def ensure(self) -> None:
        for d in (self.inbox, self.runs, self.profiles, self.cache, self.snapshots):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Gemini:
    """Gemini Enterprise (Agent Platform) settings. Note what is *not* here: a credential.

    The SDK reaches Agent Platform through ``Client(vertexai=True, …)``, whose
    flag keeps the old product's name while addressing
    ``aiplatform.googleapis.com``. That service **rejects API keys outright** —
    ``401 UNAUTHENTICATED / CREDENTIALS_MISSING, "API keys are not supported by
    this API"``, verified against an unrestricted key on 2026-08-11 — so
    Application Default Credentials are not a preference here, they are the only
    auth that works. Nothing in this dataclass is secret, which is why it is safe
    to log, to show in the UI and to put in a Temporal payload; the credential is
    none of those things and never travels.
    """

    project_id: str = ""
    #: ``global``, not a region, and this is load-bearing rather than tidy:
    #: Gemini 3.x publishes **only** to the global endpoint. On ``us-central1``
    #: every 3.x id answers 404 while the 2.5 family answers 200, so
    #: regionalising this silently pins the whole app to Gemini 2.5.
    location: str = "global"
    #: Chosen over `gemini-3.5-flash` for its output price: $7.50 per million
    #: against $9.00, a 17% cut, on a workload whose bill is dominated by output
    #: (correction returns text of the length it was given, and semantic
    #: extraction emits JSON per chunk).
    #:
    #: This is a deliberate divergence from `yorchio`, which standardised on
    #: 3.5-flash — so the two products no longer fail and improve together on the
    #: same model, and a regression seen here may not reproduce there.
    model: str = "gemini-3.6-flash"
    #: ``gemini-embedding-001`` is **no longer served** — listing the endpoint's
    #: models on 2026-08-19 returned 23 ids and this is the only embedding one.
    embedding_model: str = "gemini-embedding-2"
    #: Reasoning budget in tokens, or None for the model's own default.
    #:
    #: The largest single cost lever in the product, and invisible unless you go
    #: looking. Gemini 3.x reasons before answering and bills those tokens at the
    #: **output** rate; on a real page, correction reported 3184 output tokens of
    #: which roughly 85% was reasoning. Measured A/B on one page: $0.047584 with
    #: reasoning against $0.015459 without, a 68% saving.
    #:
    #: This is the fallback. Per-stage values in `stage_thinking` win, because
    #: the right answer differs by stage rather than by product — see there.
    thinking_budget: int | None = None

    #: Per-stage reasoning budget. A stage absent here falls back to
    #: `thinking_budget`.
    #:
    #: Set deliberately, not uniformly:
    #:
    #: * ``planning`` — off. The planner picks an id from a fixed list. That is
    #:   classification, not judgement, and it was measured spending 654 output
    #:   tokens to do it.
    #: * ``correction`` — off. Orthotypographic fixes are mechanical, and
    #:   `docagent.correct.verify()` already discards any correction that loses a
    #:   scripture reference, a number or a proper noun. Reasoning is paying
    #:   twice for a guarantee the deterministic gate already gives.
    #: * ``answering`` — **on, and deliberately so against the measurement.**
    #:   Four attempts each way on the same question gave the same 3.5 citations
    #:   on average and the same substance, at 3.3x the price
    #:   ($0.015561 against $0.004745). One attempt without reasoning did claim
    #:   an answer it could not support and was caught by the citation guard —
    #:   1 failure in 5 against 0 in 5, which is not a difference that sample
    #:   supports. So the evidence is neutral, and the choice is a deliberate
    #:   bias toward caution in the one stage where a wrong output is worst:
    #:   answering is where the product either cites the corpus or fabricates.
    #:
    #:   Kept as a fallback rather than an entry in the map on purpose, so that
    #:   setting `BRAIN_THINKING_BUDGET=0` globally still reaches it — someone
    #:   turning off every reasoning cost means it. `BRAIN_THINKING_ANSWERING`
    #:   overrides either way.
    #: * ``profile`` — off, and this one is **reasoned rather than measured**.
    #:   The argument is correction's: `docagent.rules.validate` applies a
    #:   proposal to the whole document and demands an independent signal, so a
    #:   bad rule is caught deterministically and fed back for a refine round
    #:   whether or not the model reasoned its way to it. Paying the output rate
    #:   for a guarantee the checker already gives is paying twice. No A/B has
    #:   been run; if one ever is, say so here and replace this paragraph.
    #: * ``semantics`` — off, decided by measurement rather than by argument.
    #:   A/B on the same page: with reasoning, 8 concepts and 9 claims for
    #:   $0.028549; without, **13 and 13** for $0.011239. Turning it off
    #:   extracted more, not less, and the extra concepts were real entities in
    #:   the text ("templo de Dios", "malos hábitos"). Its claims also came back
    #:   more granular, which suits a citation better than one compound
    #:   assertion. Every edge carries a confidence and the floor filters the
    #:   weak ones, so the downside is bounded.
    stage_thinking: dict[str, int | None] = field(
        default_factory=lambda: {
            "planning": 0,
            "correction": 0,
            "semantics": 0,
            "profile": 0,
        }
    )

    #: Stage names the engine uses, mapped to ours. `docagent.correct` passes
    #: ``stage="correct"``, and a silent miss here would leave correction paying
    #: for reasoning while the config said it was off.
    _STAGE_ALIASES = {
        "correct": "correction",
        "embed": "embedding",
        # `docagent.rules.propose` passes stage="propose".
        "propose": "profile",
    }

    def thinking_for(self, stage: str | None) -> int | None:
        if stage is None:
            return self.thinking_budget
        key = self._STAGE_ALIASES.get(stage, stage)
        if key in self.stage_thinking:
            return self.stage_thinking[key]
        return self.thinking_budget
    #: 3072, measured from a real call rather than assumed: the model's native
    #: width is not exposed by ``models.get``. Unchanged from
    #: ``gemini-embedding-001``, which is the only reason existing Qdrant
    #: collections survive the model change — a collection's vector size is
    #: fixed at creation.
    embedding_dimensions: int = 3072
    #: Extra extraction passes over the *same* chunk, in the same conversation,
    #: asking the model to add what it missed. The pattern is ported from
    #: nano-graphrag and microsoft/graphrag, which both default to one.
    #:
    #: **Zero here, deliberately.** Their measurement is on their corpora with
    #: their prompts, and this pipeline's own measurement went the other way once
    #: already: semantics came out *better* with reasoning off — 13 concepts
    #: against 8, at 40% of the cost. A pass multiplies the call count of the
    #: stage whose estimate already missed by 2.6x, so the default changes when
    #: there is a number, not before. See `doc/COMPANY_BRAIN.md`.
    max_gleaning: int = 0

    @property
    def configured(self) -> bool:
        """Whether a project has been named. ADC availability is a separate
        question, answered by actually calling the API — a mounted credential
        file can exist and still be expired."""
        return bool(self.project_id)


@dataclass(frozen=True)
class Settings:
    workspace: pathlib.Path
    temporal_target: str
    temporal_namespace: str
    task_queue: str
    qdrant_url: str
    #: One collection holds every library, filtered by payload. Configurable so
    #: a test suite can write somewhere disposable: the alternative is what
    #: happened before it existed — activity tests writing points into the
    #: user's real index, where they stayed.
    qdrant_collection: str
    memgraph_url: str
    database_url: str
    log_level: str
    secrets_file: pathlib.Path
    gemini: "Gemini" = field(default_factory=lambda: Gemini())
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def paths(self) -> Paths:
        return Paths(self.workspace)

    def secret(self, name: str) -> str | None:
        """A provider credential, or None when it was never configured.

        Never log the result. `repr` of this object omits the secrets dict for
        the same reason.
        """
        return self.secrets.get(name)


def _read_secrets(path: pathlib.Path) -> dict[str, str]:
    """Parse the KEY=value file the app materializes from the OS keychain.

    Absent is normal: the app writes it only once a provider has been
    configured, and the stack must start before that happens.
    """
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


#: Stages whose reasoning budget can be set independently.
THINKING_STAGES = (
    "planning",
    "correction",
    "semantics",
    "answering",
    "evalset",
    "profile",
)


def _stage_thinking() -> dict[str, int | None]:
    """Read `BRAIN_THINKING_<STAGE>` overrides, keeping the measured defaults.

    An unset variable leaves the default in place rather than clearing it: the
    per-stage choices are the product's position, and forgetting to export five
    variables should not silently revert them.

    The defaults come from `Gemini`'s own field rather than a literal here. They
    were duplicated once and drifted immediately — a stage added to the dataclass
    was missing from this copy, so a directly-constructed `Gemini` reported
    reasoning off for it while the one `load()` returns reported the model
    default. Two copies of one rule drifting apart is the mistake this whole
    repository is organised against.
    """
    out: dict[str, int | None] = dict(Gemini().stage_thinking)
    for stage in THINKING_STAGES:
        raw = _env(f"BRAIN_THINKING_{stage.upper()}", "")
        if raw:
            out[stage] = int(raw)
    return out


def load() -> Settings:
    workspace = pathlib.Path(_env("BRAIN_WORKSPACE_DIR", "/workspace")).resolve()
    secrets_file = pathlib.Path(_env("BRAIN_SECRETS_FILE", DEFAULT_SECRETS_FILE))
    return Settings(
        workspace=workspace,
        temporal_target=_env("BRAIN_TEMPORAL_TARGET", "127.0.0.1:7233"),
        temporal_namespace=_env("BRAIN_TEMPORAL_NAMESPACE", "default"),
        task_queue=_env("BRAIN_TASK_QUEUE", "brain-ingest"),
        qdrant_url=_env("BRAIN_QDRANT_URL", "http://127.0.0.1:6333"),
        qdrant_collection=_env("BRAIN_QDRANT_COLLECTION", "brain"),
        # Bolt, not HTTP: Memgraph speaks the Bolt protocol and the driver
        # parses scheme, host and port out of this one string. The loopback
        # default matches host dev mode, where the app publishes the container's
        # 7687 on 7788.
        memgraph_url=_env("BRAIN_MEMGRAPH_URL", "bolt://127.0.0.1:7788"),
        database_url=_env(
            "BRAIN_DATABASE_URL", "postgresql://brain:brain@127.0.0.1:5432/brain"
        ),
        log_level=_env("BRAIN_LOG_LEVEL", "INFO").upper(),
        secrets_file=secrets_file,
        gemini=Gemini(
            project_id=_env("BRAIN_GEMINI_PROJECT_ID", ""),
            location=_env("BRAIN_GEMINI_LOCATION", "global"),
            model=_env("BRAIN_GEMINI_MODEL", "gemini-3.6-flash"),
            embedding_model=_env("BRAIN_EMBEDDING_MODEL", "gemini-embedding-2"),
            embedding_dimensions=int(_env("BRAIN_EMBEDDING_DIMENSIONS", "3072")),
            thinking_budget=(
                int(raw) if (raw := _env("BRAIN_THINKING_BUDGET", "")) else None
            ),
            stage_thinking=_stage_thinking(),
            max_gleaning=max(0, int(_env("BRAIN_MAX_GLEANING", "0"))),
        ),
        # Loopback by default. The API has no authentication — it is reachable
        # only because nothing off the machine can route to it — so binding all
        # interfaces has to be a deliberate act, not what you get by forgetting.
        # The container sets this to 0.0.0.0 because Docker's published port
        # maps 127.0.0.1 on the host to the container's own interface; running
        # the same code directly on the host with that value would put an
        # unauthenticated control plane on the network.
        api_host=_env("BRAIN_API_HOST", "127.0.0.1"),
        api_port=int(_env("BRAIN_API_PORT", "8000")),
        secrets=_read_secrets(secrets_file),
    )


def configure() -> Settings:
    """Load settings, create the workspace layout, and chdir into it."""
    settings = load()
    settings.paths.ensure()
    os.chdir(settings.workspace)
    return settings
