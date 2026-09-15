from docagent.qdrant import Qdrant
import collections
with Qdrant("http://localhost:6333", "docagent_v2") as q:
    pts = q.scroll_all()
mine = [p for p in pts if "RetoDeDios" in str(p.get("source_file",""))]
ok = skip = 0
caps = collections.Counter()
kinds = collections.Counter()
for p in mine:
    pl = p
    kinds[pl.get("kind")] += 1
    if pl.get("chapter"): caps[pl["chapter"]] += 1
    cf = pl.get("corrected_file")
    if not cf: skip += 1; continue
    raw = open(cf, "rb").read()
    a, b = pl["char_span"]
    assert raw[a:b].decode("utf-8") == pl["text"], f"span roto en chunk {pl['chunk_index']}"
    ok += 1
print(f"(b) puntos del documento: {len(mine)} · spans byte-exactos verificados: {ok} · sin corrected_file: {skip}")
print(f"    kinds: {dict(kinds)}")
caps_reales = [c for c in caps if c.startswith("Capítulo")]
print(f"(f) capítulos 'Capítulo N' en breadcrumbs: {len(caps_reales)}  (leídos en el índice: 30)")
otros = sorted(c for c in caps if not c.startswith("Capítulo"))
print(f"    otros encabezados detectados ({len(otros)}): {otros[:6]}")
