#!/usr/bin/env python3
"""Give every concept the spelling the corpus actually uses. Writes two fields.

    cd worker
    export BRAIN_WORKSPACE_DIR=~/.local/share/io.yorch.companybrain/workspace
    export BRAIN_MEMGRAPH_URL=bolt://127.0.0.1:7789
    export BRAIN_DATABASE_URL="postgresql://brain:$BRAIN_PG_PASSWORD@127.0.0.1:5532/brain"
    uv run python scripts/retally_spellings.py [--dry-run]

`Concept.name` was set under `ON CREATE SET` and never again, so whichever
document reached a shared concept first owned its label for the whole corpus
with no tie-break. `project_concepts` now keeps a per-version tally and picks
the majority; this replays that tally out of the `semantics.json` artifacts a
version already produced, so the graph does not have to wait for a re-index.

**It costs nothing.** No model is called and no vector is touched: the spelling
counts are in artifacts already on disk, and the only writes are
`Concept.spellings` and `Concept.name`.

**What it cannot do, and says rather than hides.** Only 41 of this
installation's runs still hold a `semantics.json` — artifacts are pruned, and
79 of 238 were found missing once already — so a concept whose spellings come
from ten documents may be decided here by the three whose artifacts survive.
That is strictly more evidence than the one arbitrary vote it replaces, and it
is *not* the whole corpus. The report prints the coverage so the figure is
never mistaken for a complete tally; every re-index adds its version's vote
back, and the tally converges from there.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from brainworker import config  # noqa: E402
from brainworker.catalog import Catalog  # noqa: E402
from brainworker.graph import Graph  # noqa: E402
from brainworker.graph.projection import _TALLY_SEP, _majority_spelling  # noqa: E402
from brainworker.graph.schema import canonical_concept, concept_id  # noqa: E402

_TALLY = """
UNWIND $rows AS row
MATCH (k:Concept {id: row.id})
SET k.spellings =
    [e IN coalesce(k.spellings, []) WHERE NOT e STARTS WITH row.prefix]
    + row.entries
RETURN k.id AS id, k.spellings AS spellings, k.name AS name
"""

_RENAME = """
UNWIND $rows AS row
MATCH (k:Concept {id: row.id}) SET k.name = row.name
"""


def _runs(database_url: str) -> dict[str, tuple[str, str]]:
    """Which version and organisation each run belongs to.

    The vote has to be keyed by *version*, exactly as the projection keys it,
    or replaying twice would stack two votes for one document.
    """
    with Catalog(database_url, pooled=False) as catalog:
        with catalog._conn() as conn:  # noqa: SLF001 — a read-only script
            rows = conn.execute(
                "SELECT id, version_id, tenant_id FROM run "
                "WHERE version_id IS NOT NULL"
            ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _name_everything(graph: Graph) -> int:
    """Pick every tallied concept's majority spelling, once, at the end."""
    renamed = 0
    rows = graph.write(
        "MATCH (k:Concept) WHERE k.spellings IS NOT NULL "
        "RETURN k.id AS id, k.spellings AS spellings, k.name AS name"
    )
    changes = [
        {"id": r["id"], "name": best}
        for r in rows
        if (best := _majority_spelling(r["spellings"] or [])) and best != r["name"]
    ]
    for i in range(0, len(changes), 500):
        graph.write(_RENAME, {"rows": changes[i:i + 500]})
        renamed += len(changes[i:i + 500])
    return renamed


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change and write nothing")
    args = p.parse_args()

    settings = config.load()
    runs = _runs(settings.database_url)
    files = sorted(
        glob.glob(str(settings.workspace / "runs" / "*" / "semantics.json")),
        key=lambda f: pathlib.Path(f).stat().st_mtime,
    )

    renamed, seen_versions = 0, set()
    with Graph(settings.memgraph_url) as graph:
        for path in files:
            run = pathlib.Path(path).parent.name
            if run not in runs:
                continue
            version, tenant = runs[run]
            seen_versions.add(version)
            per: dict[str, collections.Counter] = collections.defaultdict(
                collections.Counter
            )
            for c in json.load(open(path, encoding="utf-8")).get("concepts") or []:
                name = (c.get("name") or "").strip()
                if name:
                    per[canonical_concept(name)][name] += 1
            rows = [
                {
                    "id": concept_id(key, tenant),
                    "prefix": f"{version}{_TALLY_SEP}",
                    "entries": [
                        f"{version}{_TALLY_SEP}{n}{_TALLY_SEP}{spelling}"
                        for spelling, n in sorted(counts.items())
                    ],
                }
                for key, counts in per.items()
            ]
            for i in range(0, len(rows), 500):
                if args.dry_run:
                    continue
                graph.write(_TALLY, {"rows": rows[i:i + 500]})

        # **Two phases, and the second cannot be folded into the first.** A
        # rename decided inside the loop is decided from the votes counted *so
        # far*, so the answer depends on the order the artifacts were replayed
        # in and a second run of this script kept correcting more — 1,480 then
        # 440. Naming every concept once, after every vote is in, reaches the
        # same fixpoint the projection reaches document by document, and makes
        # re-running this a genuine no-op.
        if not args.dry_run:
            renamed = _name_everything(graph)

        total = graph.write("MATCH (k:Concept) RETURN count(k) AS n")[0]["n"]
        tallied = graph.write(
            "MATCH (k:Concept) WHERE k.spellings IS NOT NULL RETURN count(k) AS n"
        )[0]["n"]

    print(f"artifacts replayed : {len(seen_versions)} versions")
    print(f"display names fixed: {renamed}")
    print(f"coverage           : {tallied} of {total} concepts carry a tally "
          f"({100 * tallied / max(1, total):.0f}%)")
    print("a concept with no tally keeps the name it has until its document is "
          "re-indexed — the artifacts for it were pruned, not lost here")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
