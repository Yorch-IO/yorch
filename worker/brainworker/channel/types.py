"""What crosses a Temporal payload for the two channel passes.

Small on purpose, and for the reason every payload in this codebase is: history
persists to Postgres for the whole retention period, so a transcript in a
request would be bulk data in the one place this repository has a standing rule
against. What travels here is ids and a topic; the transcripts are read from the
video runs' own artifacts by the activity that needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..graph.schema import LEGACY_TENANT_ID


@dataclass
class DiscoverRequest:
    """Judge a channel's metadata against a topic.

    `limit` and `deep_limit` are both here even though only the first is used by
    the preselection: the estimate this run persists has to quote *both* passes,
    because that is the figure the person was shown before they pressed the
    button, and a receipt that quoted half of what was offered would be worse
    than no receipt.
    """

    channel_id: str
    topic: str
    library_id: str
    limit: int = 100
    deep_limit: int = 10
    tenant_id: str = LEGACY_TENANT_ID
    #: The screen's title-keyword filter: judge only these videos. `None` is the
    #: whole catalogue. **Appended and defaulted**, so a payload that never
    #: carried it decodes as `None` and the replay's sequence is unchanged —
    #: the same shape as `build_epub`, and why no `workflow.patched` is needed.
    video_ids: list[str] | None = None


@dataclass
class TopicsRequest:
    """Read the uncorrected transcripts of video runs that have already probed.

    `video_runs` are workflow ids, and nothing else — deliberately not
    `(workflow_id, video_id)` pairs. The video id is derived from the run's own
    document, because a client-supplied pairing is exactly the shape of mistake
    `videosource.check_resolved` exists to refuse: one video's words filed under
    another video's identity, failing nowhere.
    """

    channel_id: str
    topic: str
    library_id: str
    video_runs: list[str] = field(default_factory=list)
    tenant_id: str = LEGACY_TENANT_ID


@dataclass
class DiscoveryOutcome:
    """What a discovery run concluded, for the route that started it.

    Every field declared: this is returned through Temporal and rendered, and
    `asdict` writes declared fields and nothing else.
    """

    run_id: str
    evaluated: int = 0
    relevant: int = 0
    doubtful: int = 0
    discarded: int = 0
    unevaluated: int = 0
    invented: int = 0
    malformed: int = 0
    shortlist: list[str] = field(default_factory=list)
    total_usd: float | None = None


@dataclass
class TopicsOutcome:
    """What a topic pass read."""

    run_id: str
    videos: int = 0
    answering: int = 0
    #: Topics whose quotation was found in the transcript, across every video.
    verified: int = 0
    #: Topics the model produced that were not found. A reading nobody can check
    #: must not look like one that can, so the two are counted apart.
    unverified: int = 0
    failed: int = 0
    total_usd: float | None = None
