#!/usr/bin/env python3
"""Recompute every point's BM25 sparse vector in place, from the text already
stored in its payload.

Why this exists: `docagent.bm25.tokenize` changes over time — the scripture
compound token is the current example — and a change there only reaches the
index by re-indexing. A full re-index costs embedding money and needs Vertex.
BM25 does not: it is computed locally from `chunk.text`, and `avgdl` is
per-document (`graph.py`'s embed node), so the pipeline's own arithmetic can be
reproduced exactly from what Qdrant already holds. Only the named `bm25` vector
is written; dense vectors, payloads and tenant ids are never touched.

Two safety properties, both of which have earned their place:

* **It verifies before it writes.** `--verify-with-old` rebuilds the stored
  vectors using a tokenizer that drops the new tokens and compares term sets. A
  mismatch means this script does not reproduce the pipeline and must not write.
  Run once against `docagent_v2` it reported 2519 of 2519, which is what made
  the write safe to do at all.
* **It backs up first.** `--backup FILE` writes every point's current sparse
  vector as JSON. Restoring is the same PUT with those values.

A value difference where the term sets agree is a *stale* `avgdl`: the document
was indexed when it had a different chunk set, so its length normalisation was
frozen against a corpus it no longer has. Recomputing corrects it. Measured
2026-08-29: 4 of 33 documents in `docagent_v2` and 25 of 71 in `brain`.

    reindex_sparse.py --collection docagent_v2 --group-by doc_id --verify-with-old
    reindex_sparse.py --collection brain --url http://localhost:6433 \
        --group-by version_id --backup /tmp/brain.json --apply
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from docagent import bm25  # noqa: E402

BATCH = 200


def _request(method: str, url: str, body: dict) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def scroll(base: str, collection: str, with_vector) -> list[dict]:
    """Every point, with the payload fields this script needs."""
    out: list[dict] = []
    offset = None
    while True:
        body = {
            "limit": 500,
            "with_payload": True,
            "with_vector": with_vector,
        }
        if offset is not None:
            body["offset"] = offset
        result = _request("POST", f"{base}/collections/{collection}/points/scroll", body)["result"]
        out += result["points"]
        offset = result.get("next_page_offset")
        if offset is None:
            return out


def tokenize_without_scripture(text: str) -> list[str]:
    """What `tokenize` did before the scripture rule: the character walk alone.

    Deliberately a reimplementation rather than a flag on `tokenize` — the point
    is to compare against the *old* behaviour, and a flag would drift with it.
    """
    out: list[str] = []
    buf: list[str] = []
    for ch in text.translate(bm25._FOLD).lower():
        if ch.isalnum():
            buf.append(ch)
            continue
        token = "".join(buf)
        buf.clear()
        if len(token) >= bm25.MIN_TOKEN_LEN and token not in bm25.STOPWORDS:
            out.append(token)
    token = "".join(buf)
    if len(token) >= bm25.MIN_TOKEN_LEN and token not in bm25.STOPWORDS:
        out.append(token)
    return out


def group(points: list[dict], key: str) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = collections.defaultdict(list)
    for point in points:
        by[point["payload"][key]].append(point)
    for members in by.values():
        members.sort(key=lambda p: p["payload"]["chunk_index"])
    return by


def verify(points: list[dict], key: str) -> tuple[int, int, float]:
    """Rebuild the stored vectors with the old tokenizer.

    Returns (term sets matching, term sets differing, largest value difference).
    Term sets must match for every point; a value difference is a stale avgdl.
    """
    matched = differed = 0
    worst = 0.0
    for members in group(points, key).values():
        tokens = [tokenize_without_scripture(p["payload"]["text"]) for p in members]
        avgdl = bm25.avg_doc_len(tokens)
        for point, toks in zip(members, tokens):
            rebuilt = bm25.doc_sparse_vector(toks, avgdl)
            stored = point["vector"]["bm25"]
            mine = dict(zip(rebuilt.indices, rebuilt.values))
            theirs = dict(zip(stored["indices"], stored["values"]))
            if set(mine) != set(theirs):
                differed += 1
                continue
            matched += 1
            worst = max(worst, max((abs(mine[k] - theirs[k]) for k in mine), default=0.0))
    return matched, differed, worst


def build(points: list[dict], key: str) -> list[dict]:
    updates = []
    for members in group(points, key).values():
        tokens = [bm25.tokenize(p["payload"]["text"]) for p in members]
        avgdl = bm25.avg_doc_len(tokens)
        for point, toks in zip(members, tokens):
            vector = bm25.doc_sparse_vector(toks, avgdl)
            updates.append(
                {
                    "id": point["id"],
                    "vector": {"bm25": {"indices": vector.indices, "values": vector.values}},
                }
            )
    return updates


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:6333")
    ap.add_argument("--collection", required=True)
    ap.add_argument(
        "--group-by",
        default="doc_id",
        help="payload field identifying one document, over which avgdl is averaged "
        "(docagent_v2: doc_id; brain: version_id)",
    )
    ap.add_argument("--backup", help="write every current sparse vector here before touching anything")
    ap.add_argument("--verify-with-old", action="store_true", help="prove the pipeline is reproducible first")
    ap.add_argument("--apply", action="store_true", help="write. Without it this is a dry run.")
    args = ap.parse_args()

    base = args.url.rstrip("/")
    points = scroll(base, args.collection, with_vector=["bm25"])
    print(f"{args.collection}: {len(points)} points, "
          f"{len({p['payload'][args.group_by] for p in points})} documents by {args.group_by}")

    missing = [p["id"] for p in points if not p["payload"].get("text")]
    if missing:
        print(f"REFUSING: {len(missing)} points carry no text, so their vectors cannot be rebuilt")
        return 1

    if args.backup:
        pathlib.Path(args.backup).write_text(
            json.dumps([{"id": p["id"], "bm25": p["vector"]["bm25"]} for p in points])
        )
        print(f"backup: {args.backup}")

    if args.verify_with_old:
        matched, differed, worst = verify(points, args.group_by)
        print(f"reproduced with the old tokenizer: {matched} of {len(points)} term sets match, "
              f"{differed} differ; largest value difference {worst:.3e}")
        if differed:
            print("REFUSING: a differing term set means this script does not reproduce the pipeline")
            return 1
        if worst > 1e-6:
            print("  (values differ where terms agree: some documents carry a stale avgdl, "
                  "which recomputing corrects)")

    updates = build(points, args.group_by)
    if not args.apply:
        print(f"dry run: {len(updates)} points would be rewritten. Pass --apply to write.")
        return 0

    written = 0
    for i in range(0, len(updates), BATCH):
        chunk = updates[i : i + BATCH]
        result = _request(
            "PUT", f"{base}/collections/{args.collection}/points/vectors?wait=true", {"points": chunk}
        )
        if result.get("status") != "ok":
            print(f"FAILED at point {written}: {result}")
            return 1
        written += len(chunk)
    print(f"written: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
