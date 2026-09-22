"""Every activity the worker registers must carry its own definition.

The failure this exists for is a **worker that will not start**, and nothing
else in the suite can see it: activities are called as plain functions
everywhere in these tests, so a decorator attached to the wrong function is
invisible until `Worker(...)` raises at boot — in a container, after a deploy.

It happened: extracting a pure helper out of the top of `group_transcript` put
the new function directly under that activity's `@activity.defn`, which silently
made the *helper* the activity and left the activity bare. The whole worker
refused to start with `Activity group_transcript missing attributes`, and the
run in flight sat there until somebody read the container log.

The mirror-image failure is in the same family and is recorded beside
`promote_version`: two activities registered under one name, where a worker
accepts the task and then fails it.
"""

from __future__ import annotations

import pytest
from temporalio.activity import _Definition

from brainworker.runner import ACTIVITIES, FETCH_ACTIVITIES


@pytest.mark.parametrize(
    "registry,name",
    [(ACTIVITIES, "ACTIVITIES"), (FETCH_ACTIVITIES, "FETCH_ACTIVITIES")],
)
def test_every_registered_activity_is_decorated(registry, name):
    bare = [fn.__name__ for fn in registry if _Definition.from_callable(fn) is None]
    assert bare == [], f"{name} holds functions Temporal cannot register: {bare}"


@pytest.mark.parametrize(
    "registry,name",
    [(ACTIVITIES, "ACTIVITIES"), (FETCH_ACTIVITIES, "FETCH_ACTIVITIES")],
)
def test_no_two_activities_claim_one_name(registry, name):
    """A collision does not fail at boot — the worker accepts the task and then
    fails it, which is why `promote_version` is not called `activate_version`."""
    names = [_Definition.must_from_callable(fn).name for fn in registry]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert duplicates == [], f"{name} registers a name twice: {duplicates}"
