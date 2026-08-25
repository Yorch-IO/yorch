"""Resumen de una indexación por lotes, construido desde Postgres.

Sustituye a `generate_summary.py`, que sumaba el manifiesto y publicaba el
resultado como si fuera un hecho. Eso produjo un resumen del 2026-08-22 con tres
afirmaciones falsas — «ningún documento superó el techo alto» cuando fueron 8 de
34, «la factura se mantiene por debajo del estimado bajo» cuando ocurrió en 22 de
34, y «ningún run cayó en falso timeout» cuando hubo uno — y un total $3.49 por
debajo del real, porque ocho runs nunca llegaron al manifiesto.

La diferencia no es de aritmética: **el manifiesto es lo que el script del lote
cree que pasó, y la base de datos es lo que pasó.** Aquí el manifiesto se trata
como una afirmación a contrastar, los totales salen del catálogo, y todo lo que
no cuadra se imprime en vez de resolverse en silencio.

Solo lectura, solo stdlib:

    python3 infra/audit_indexacion.py [ISO-8601 desde el que contar el lote]
"""

import json
import pathlib
import subprocess
import sys

RUNS_DIR = pathlib.Path("/home/kheiron/yorch/infra/workspace/runs")
MANIFEST = RUNS_DIR / "indexacion.jsonl"
SUMMARY = RUNS_DIR / "indexacion-resumen.md"
#: Los artifacts pueden estar bajo cualquiera de los dos workspaces que comparten
#: catálogo — ver el defecto «un catálogo, dos workspaces» en CLAUDE.md.
WORKSPACES = [
    pathlib.Path("/home/kheiron/yorch/infra/workspace"),
    pathlib.Path.home() / ".local/share/io.sek.companybrain/workspace",
]
DESDE = sys.argv[1] if len(sys.argv) > 1 else "2026-08-22 01:20:00+00"


def sql(query):
    res = subprocess.run(
        ["docker", "exec", "company-brain-postgres-1", "psql", "-U", "brain",
         "-d", "brain", "-tAc", query],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        raise SystemExit(f"consulta fallida: {res.stderr.strip()}")
    return [l.split("|") for l in res.stdout.strip().split("\n") if l.strip()]


def artifact(run_id, nombre):
    for w in WORKSPACES:
        ruta = w / "runs" / run_id / nombre
        if ruta.is_file():
            return ruta
    return None


def main():
    # --- lo que pasó, según el catálogo -------------------------------------
    runs = sql(
        "SELECT r.id, r.state, coalesce(d.title,''), "
        "coalesce((SELECT sum(usd) FROM cost_entry c WHERE c.run_id=r.id),0) "
        f"FROM run r LEFT JOIN document d ON d.id=r.document_id "
        f"WHERE r.started_at > '{DESDE}' ORDER BY r.started_at;"
    )
    real = {r[0]: {"state": r[1], "title": r[2], "usd": float(r[3])} for r in runs}
    gastado = sum(v["usd"] for v in real.values())
    por_estado = {}
    for v in real.values():
        por_estado[v["state"]] = por_estado.get(v["state"], 0) + 1

    # --- lo que el lote cree que pasó ---------------------------------------
    filas = []
    if MANIFEST.is_file():
        filas = [json.loads(l) for l in MANIFEST.read_text(encoding="utf-8").splitlines() if l.strip()]
    con_id = {f["run_id"]: f for f in filas if f.get("run_id")}

    # --- dónde no coinciden --------------------------------------------------
    inventados = [i for i in con_id if i not in real]
    ausentes = [(i, v) for i, v in real.items() if i not in con_id]
    estado_mal = [(con_id[i]["archivo"], con_id[i]["estado"], real[i]["state"])
                  for i in con_id if i in real and con_id[i]["estado"] != real[i]["state"]]
    coste_mal = []
    for i, f in con_id.items():
        if i not in real:
            continue
        dicho, cierto = f.get("facturado_usd"), real[i]["usd"]
        if (dicho is None and cierto > 0) or (dicho is not None and abs(dicho - cierto) > 1e-6):
            coste_mal.append((f["archivo"], dicho, cierto))

    # --- los claims, contra el artifact que los produjo ----------------------
    claims = verificados = 0
    claims_mal = []
    for i, f in con_id.items():
        ruta = artifact(i, "semantics.json")
        if ruta is None:
            continue
        cl = (json.loads(ruta.read_text(encoding="utf-8")).get("claims") or [])
        c, v = len(cl), sum(1 for x in cl if x.get("quote"))
        claims += c
        verificados += v
        if f.get("claims") is not None and (f["claims"] != c or f.get("claims_verificados") != v):
            claims_mal.append((f["archivo"], f.get("claims"), c, f.get("claims_verificados"), v))

    # --- el estimador contra la factura --------------------------------------
    sobre_alto = [f for f in filas
                  if f.get("facturado_usd") and f.get("estimado_alto_usd")
                  and f["facturado_usd"] > f["estimado_alto_usd"]]
    sobre_bajo = [f for f in filas
                  if f.get("facturado_usd") and f.get("estimado_usd")
                  and f["facturado_usd"] > f["estimado_usd"]]
    con_ambos = [f for f in filas if f.get("facturado_usd") and f.get("estimado_usd")]

    colgadas = int(sql("SELECT count(*) FROM run WHERE state='awaiting_approval';")[0][0])

    def bloque(titulo, items, formato):
        if not items:
            return f"- **{titulo}:** ninguno.\n"
        cuerpo = "".join(f"  - {formato(x)}\n" for x in items[:12])
        extra = f"  - …y {len(items) - 12} más\n" if len(items) > 12 else ""
        return f"- **{titulo}: {len(items)}**\n{cuerpo}{extra}"

    md = f"""# Indexación por lotes — resumen auditado

Generado desde el catálogo, no desde el manifiesto. Cada cifra de dinero y de
estado sale de `run` y `cost_entry`; los claims salen del `semantics.json` de
cada run. El manifiesto se usa solo para comparar, y donde discrepa se dice.

Ventana: runs iniciados después de `{DESDE}`.

## Lo que ocurrió

- **Runs en la ventana:** {len(real)}
- **Por estado:** {", ".join(f"{k} {v}" for k, v in sorted(por_estado.items()))}
- **Gasto real total:** ${gastado:.4f}
- **Compuertas sin contestar ahora:** {colgadas}
- **Claims extraídos:** {claims:,} · **con cita verificada:** {verificados:,}
  ({100 * verificados / claims:.1f}% si {claims} > 0)

## El manifiesto contra la realidad

- **Filas en el manifiesto:** {len(filas)} · **con `run_id`:** {len(con_id)}
{bloque("run_id que no existen en el catálogo", inventados, lambda x: f"`{x}`")}\
{bloque("Runs reales que el manifiesto no registra", ausentes,
        lambda x: f"${x[1]['usd']:.4f} · {x[1]['state']} · {x[1]['title'][:44]} · `{x[0]}`")}\
{bloque("Estado que no coincide", estado_mal,
        lambda x: f"{x[0][:44]}: el manifiesto dice `{x[1]}`, el catálogo `{x[2]}`")}\
{bloque("Coste que no coincide", coste_mal,
        lambda x: f"{x[0][:44]}: dice {x[1]}, real ${x[2]:.4f}")}\
{bloque("Claims que no cuadran con su artifact", claims_mal,
        lambda x: f"{x[0][:40]}: dice {x[1]}/{x[3]}, artifact {x[2]}/{x[4]}")}
## El estimador contra la factura

De {len(con_ambos)} documentos con estimación y factura:

- **Por encima del extremo bajo:** {len(sobre_bajo)} — normal, el bajo describe un
  documento típico.
- **Por encima del techo alto:** {len(sobre_alto)} — **esto es una regla rota.** El
  techo existe para no ser superado; si aparece alguno aquí, hay que volver a
  medir `SEMANTICS_CALL_OVERHEAD` y `OUTPUT_SPREAD` con la consulta de
  `doc/COMPANY_BRAIN.md`.
{bloque("Documentos por encima del techo", sobre_alto,
        lambda x: f"{x['archivo'][:44]}: ${x['facturado_usd']:.4f} contra ${x['estimado_alto_usd']:.4f}")}
"""

    SUMMARY.write_text(md, encoding="utf-8")
    print(md)
    print(f"escrito en {SUMMARY}")


if __name__ == "__main__":
    main()
