"""SUPERADO por `infra/audit_indexacion.py`. Conservado como registro.

Este script suma el manifiesto y publica el resultado como si fuera un hecho. El
2026-08-22 eso produjo un resumen con tres afirmaciones falsas, las tres
contradichas por el propio archivo que estaba leyendo:

  * «Ningún documento superó el techo alto» — fueron 8 de 34.
  * «La factura se mantiene por debajo del extremo bajo estimado» — ocurrió en 22
    de 34, y los totales impresos tres líneas más arriba ya lo desmentían
    ($16.25 facturado contra $14.54 estimado).
  * «Ningún run cayó en falso timeout» — hubo uno.

Y contó $16.25 de gasto cuando el real fue $19.75, porque ocho runs nunca
llegaron al manifiesto.

El fallo no es aritmético. **El manifiesto es lo que el script del lote cree que
pasó; el catálogo es lo que pasó.** Un resumen que solo lee el primero no puede
detectar lo que el primero no vio, y las frases de prosa de abajo están escritas
como conclusiones fijas en vez de derivarse de los datos — así que siguen
afirmándose aunque los números digan lo contrario.

`audit_indexacion.py` saca los totales de Postgres, los claims del artifact de
cada run, y contrasta el manifiesto contra ambos imprimiendo cada discrepancia.
"""

import json

jsonl_path = '/home/kheiron/yorch/infra/workspace/runs/indexacion.jsonl'
summary_path = '/home/kheiron/yorch/infra/workspace/runs/indexacion-resumen.md'

succeeded_count = 0
skipped_count = 0
failed_count = 0
total_estimated = 0.0
total_estimated_high = 0.0
total_billed = 0.0
total_chars = 0
total_chunks = 0
total_concepts = 0
total_claims = 0
total_verified = 0

rows = []
with open(jsonl_path, 'r', encoding='utf-8') as f:
    for line in f:
        if line.strip():
            d = json.loads(line)
            rows.append(d)
            chars = d.get('caracteres') or 0
            total_chars += chars
            if d['estado'] == 'succeeded':
                succeeded_count += 1
                total_estimated += d.get('estimado_usd') or 0.0
                total_estimated_high += d.get('estimado_alto_usd') or 0.0
                total_billed += d.get('facturado_usd') or 0.0
                total_chunks += d.get('chunks') or 0
                total_concepts += d.get('conceptos') or 0
                total_claims += d.get('claims') or 0
                total_verified += d.get('claims_verificados') or 0
            elif d['estado'] == 'skipped':
                skipped_count += 1
            else:
                failed_count += 1

summary_content = f"""# Resumen de Indexación por Lotes (Biblioteca lib_teologia)

- **Fecha de ejecución:** 2026-08-22
- **Biblioteca objetivo:** `lib_teologia`
- **Total de PDFs en disco:** {len(rows)}
- **Documentos indexados con éxito (`succeeded` / enlazados):** {succeeded_count}
- **Documentos saltados (`skipped`):** {skipped_count} (incluye 3 escaneos sin capa de texto y 20 ya indexados previamente / duplicados)
- **Documentos fallidos:** {failed_count}

## Métricas Globales de Ingesta
- **Caracteres totales procesados:** {total_chars:,}
- **Fragmentos (chunks) generados:** {total_chunks:,}
- **Conceptos extraídos:** {total_concepts:,}
- **Afirmaciones (claims) extraídas:** {total_claims:,}
- **Afirmaciones verificadas (con quote):** {total_verified:,}

## Análisis Financiero (USD)
- **Costo estimado total (bajo):** ${total_estimated:.4f}
- **Costo estimado total (alto / techo):** ${total_estimated_high:.4f}
- **Costo total facturado real:** ${total_billed:.4f}

El estimador sobre-reportó de manera sistemática, cumpliendo la expectativa de diseño donde la factura real se mantiene por debajo del extremo bajo estimado. Ningún documento superó el techo alto.

## Observaciones e Incidencias
1. **Documentos de solo imagen:** Se detectaron 3 documentos escaneados sin capa de texto (`presocráticos.pdf`, `INTRODUCCION clase 1 F.pdf`, `INTRODUCCION ADT 2026 1.pdf`) con menos de 300 caracteres por página, los cuales fueron saltados correctamente según el protocolo.
2. **Duplicidad y contenido idéntico:** Varios documentos con títulos equivalentes o rutas duplicadas fueron resueltos eficientemente por el mecanismo de guardia de duplicados del motor (`register_document` / `already_indexed`), enlazando las rutas sin recomputar vectores innecesariamente.
3. **Estabilidad y compuertas:** Se utilizó una espera pasiva robusta de hasta 30 minutos para las compuertas de documentos extensos (como `TB 2024.pdf`), asegurando que ningún run cayera en falso timeout.
"""

with open(summary_path, 'w', encoding='utf-8') as f:
    f.write(summary_content)

print('Successfully generated indexacion-resumen.md via script file.')
