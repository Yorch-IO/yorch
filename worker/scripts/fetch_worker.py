#!/usr/bin/env python3
"""A worker that does nothing but talk to YouTube, from an address it will answer.

**Why this process exists.** Measured 2026-09-05 against production: `yt-dlp
extract_info` succeeds from a residential IP in 2.6 s and is refused from the
EC2 egress IP `34.218.169.144` with "Sign in to confirm you're not a bot". The
same host fetches the `youtube.com` watch page at 200 and every signed caption
URL at 200 — so it is the player API that is bot-checked, not the network, and
the fix is to make that one call somewhere else rather than to move the product.

It therefore registers **two activities and no workflows**:

- ``resolve_video`` — the `extract_info` call. Writes nothing, returns a few
  kilobytes of `VideoInfo`.
- ``fetch_audio`` — only reached for a video with no captions. It has to run
  here too, and for a different measured reason: a `googlevideo` media URL
  carries the address that resolved it (`ip=…`) and answers **403** from
  anywhere else. It writes no artifact — a transient file it deletes in a
  `finally` — and returns an S3 URI, so nothing crosses hosts.

Everything else stays on the worker that owns the workspace. In particular the
**caption download does not run here**: a caption URL carries `ip=0.0.0.0` and
was served to the blocked host at 200, and a real caption file is 582,176 bytes
on a 76-minute video — bytes that would otherwise have to cross a payload.

    # one terminal: the loopback-only Temporal, forwarded over SSM
    ~/yorch-aws-platform/scripts/deploy-brain.sh tunnel 7333 7333

    # another: this
    cd ~/yorch/worker
    BRAIN_TEMPORAL_TARGET=127.0.0.1:7333 \\
    BRAIN_FETCH_TASK_QUEUE=brain-fetch \\
      uv run python scripts/fetch_worker.py

It needs **no** Postgres, Qdrant, Memgraph or ADC. It needs AWS credentials only
to serve `fetch_audio`, which only runs for a video that has no captions at all.

The cost, stated plainly because it is real: **a video import only gets past
`probing` while this is running.** That is bounded rather than silent —
`FETCH_START_TIMEOUT` fails an unclaimed task after ten minutes and the run says
`fetch_worker_unavailable` in the import queue, which it can only do because the
run row is now opened before the first activity.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from temporalio.worker import Worker

from brainworker import config
from brainworker.activities import video as videoacts
from brainworker.runner import connect

log = logging.getLogger("fetch_worker")

#: Exactly the activities that must run on an address YouTube will answer.
FETCH_ACTIVITIES = [videoacts.resolve_video, videoacts.fetch_audio]


async def main() -> None:
    settings = config.configure()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    queue = settings.fetch_task_queue
    if not queue:
        # Refused rather than defaulted. Falling back to `task_queue` would start
        # a second worker on the main queue that can serve only two activities,
        # which is a subtle way to make half the pipeline unavailable.
        print(
            "BRAIN_FETCH_TASK_QUEUE is unset. This process is only useful on a "
            "queue of its own — set it to the same value the control plane "
            "starting the video runs uses.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    client = await connect(settings)
    log.info(
        "fetch worker starting: queue=%s namespace=%s activities=%s",
        queue,
        settings.temporal_namespace,
        ", ".join(a.__name__ for a in FETCH_ACTIVITIES),
    )
    # No `workflows=`: this process must never be handed a workflow task. It has
    # two activities and none of the stores.
    await Worker(client, task_queue=queue, activities=FETCH_ACTIVITIES).run()


if __name__ == "__main__":
    asyncio.run(main())
