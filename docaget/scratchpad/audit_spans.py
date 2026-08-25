from docagent.qdrant import Qdrant

DOC_ID_TO_AUDIT = "2. PAPADO, MONAQUISMO E IMPERIO MUSULMÁN Y SU INFLUENCIA EN EL IMPERIO RONANO DE ORIENTE.pdf"

with Qdrant("http://localhost:6333", "docagent_v2") as q:
    all_pts = q.scroll_all()
    pts_to_audit = [p for p in all_pts if p.get("source_file", "").endswith(DOC_ID_TO_AUDIT)]

count = 0
for p in pts_to_audit:
    if not p.get("corrected_file"):
        continue
    raw = open(p["corrected_file"], "rb").read()
    a, b = p["char_span"]
    assert raw[a:b].decode("utf-8") == p["text"], p["chunk_index"]
    count += 1

print(f"{len(pts_to_audit)} puntos para este documento, {count} spans verificados")
