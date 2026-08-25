# Runbook: indexación supervisada del corpus de docagent

## Contexto

`docagent` sabe indexar un documento; lo que no está probado es indexar **un corpus
de ~75 documentos agrupados en una decena de familias**. Las tres cosas que salieron
mal hasta ahora no fueron caídas, fueron corridas con logs sanos y números
inválidos: el evalset heredado entre libros de la misma familia, cuatro bugs de
estado en el bucle de tuning, y una regla de `kind` que un script declaró limpia
mientras la implementación etiquetaba mal nueve notas al pie.

De ahí la forma de este plan: no es "corre el comando", es **un operador humano-en-el-
loop delegado a una AI** que después de cada documento tiene que demostrar que el
número que reporta significa algo. La AI lee el PDF, escribe las pruebas de
diagnóstico a partir de lo que realmente dice el libro, y compara contra la
colección anterior antes de declarar una mejora.

Los cambios de la sesión previa (reversión del hack de perfiles, evalset por
documento, `--dry-run` sin persistir, sidecars de diagnóstico) están **sin
commitear**. Esta corrida es también su validación de punta a punta.

### Lo que la primera corrida supervisada (julio 2026, 7 documentos) enseñó

Se indexaron dos libros de Historia de la Iglesia y cinco de Hermenéutica. Las
métricas salieron todas por encima del objetivo y **aun así la corrida destruyó
datos y ocultó un defecto estructural**. Los tres hechos, porque cambian cómo se
supervisa a partir de ahora:

1. **Un perfil reutilizado sobrescribió el archivo del documento que lo aprendió.**
   El slug viajaba con el perfil, así que Hermenéutica Capítulo 1 guardó su evalset
   y sus scores encima de `1-desde-agustín-…json`. Los números medidos de los dos
   libros de Historia solo sobreviven en `logs/`. **Arreglado**: el perfil
   reutilizado re-deriva su slug (`n_load_profile`), con test.
2. **Dos archivos de perfil compartían fingerprint y `load` devolvía el primero por
   orden alfabético** — uno de ellos con todos los scores en 0.0, residuo del hack
   revertido. **Arreglado**: gana el `learned_at` más reciente, con test.
3. **El fingerprint agrupó Hermenéutica con Historia de la Iglesia.** Es agrupación
   estructural: mismo extractor, mismas cabeceras normalizadas, misma geometría,
   misma profundidad de numeración. Un capítulo de Hermenéutica quedó troceado con
   los `heading_guards` de un libro de Historia. **Sin arreglar, y el punto
   importante: las métricas no lo vieron** — recall@5 0.850, veredicto OK — porque
   el evalset sintético se genera de los mismos chunks mal segmentados. Lo único
   que lo delató fue "4 capítulos heredados vs 1 leído" en la tabla del informe.

La lección operativa: **un veredicto OK con capítulos detectados ≠ capítulos leídos
no es OK.** Ver la regla nueva en 1.5(f).

## Decisiones ya tomadas (no volver a preguntar)

| Decisión | Valor |
|---|---|
| Alcance | Por familias, en lotes. Una familia completa, validada, antes de pasar a la siguiente. |
| Colección | Nueva: `docagent_v2`. La colección `docagent` queda intacta como referencia comparable. |
| Autonomía | Puede corregir código **si** acompaña el arreglo con un test que falle antes y pase después. **No commitea**. |
| Presupuesto | Sin tope. OCR autorizado. A cambio, el informe final rinde cuentas por documento y por etapa. |

## Entregable

Un archivo `INFORME_INDEXACION.md` en `docagent/`, construido incrementalmente (una
sección por familia, escrita al terminar cada lote — no al final, para que una
interrupción no pierda el trabajo). Más los sidecars `*.diag.json` creados, los
perfiles en `profiles/`, y `costo.json`.

---

## Fase 0 — Preparación (una sola vez)

```bash
cd /home/kheiron/facter/docagent
uv sync
uv run pytest -q                      # debe dar 118 passed. Si no, PARA.
docker compose -f ../sociologia/docker-compose.yml up -d
curl -s localhost:6333/readyz         # Qdrant arriba antes de gastar un centavo
grep -c API_KEY .env                  # sin leerlo ni imprimirlo
```

`118 passed` es la línea base. Si un test falla **antes** de tocar nada, el arreglo
de eso es la tarea, no la indexación.

**Inventario del corpus.** Antes de indexar, agrupar `libros/*` en familias y
detectar basura. Hay al menos tres cosas que no deben entrar sin decisión explícita:

- **Duplicados aparentes**: `Hermeneutica Capitulo 1 (1).pdf` vs
  `Hermeneutica Capitulo 1.pdf`; `CONFERENCIA RELACIÓN DEL HOMBRE CON EL TIEMPO (1).pdf`
  vs su gemelo. `doc_id_for` hashea la **ruta**, así que dos copias del mismo
  contenido se indexan dos veces y compiten entre sí en el ranking. Comparar por
  hash de bytes y por número de páginas, y reportar.
- **No-contenido**: `Plantilla semana 5.pdf`, `Plantilla semana 6.pdf` parecen
  plantillas vacías. Verificar extrayendo con `--dry-run` y mirando el conteo de
  párrafos.
- **`LAS IGLESIAS DEL APOCALIPSIS, Cap. 2 y 3.pptx`** es el único `.pptx`. Ruta de
  extracción distinta (`extract/pptx_`), y la corrección **no** aplica a fuentes no-prosa.

Familias propuestas (verificar con el `fingerprint` real, no con el nombre):
Historia de la Iglesia · Doctrina/Sistemática · Hermenéutica · Homilética ·
Filosofía y Cristianismo (FYC) · Administración del Tiempo (ADT) · Familia y
Consejería (UNIDAD 1-4) · Teología Social · Liderazgo y Discipulado · Vida
Cristiana (SEMANA*) · Escatología · Sueltos.

**Orden de los lotes**: empezar por **Historia de la Iglesia**, porque es la única
familia con dos libros ya procesados y por tanto la única donde hay con qué
comparar. Es también donde el arreglo del evalset tiene que demostrarse.

---

## Fase 1 — Por cada familia: el documento cabeza de serie

El primer documento de una familia paga el aprendizaje de reglas; los siguientes
heredan el perfil. Por eso se supervisa distinto.

### 1.1 Leer el documento (antes de indexarlo)

Obligatorio, y es la parte que no se puede automatizar: **leer el PDF** con la
herramienta Read (soporta PDF por rango de páginas, máximo 20 por llamada). No hace
falta leerlo completo: primeras 10 páginas, el índice si lo hay, y las últimas 5
—donde viven las preguntas de repaso, que son la fuente de los fallos de
`heading_guards`—.

De esa lectura salen tres cosas anotadas en el informe:

1. **La estructura declarada**: cuántos capítulos dice tener el documento y cómo
   están numerados. Este es el número contra el que se juzga `heading_guards`
   después. Sin él, "4 capítulos" es una opinión.
2. **Si hay preguntas de repaso**, y si son interrogativas (`¿Qué es…?`) o
   imperativas (`Defina…`). Las imperativas son las que `IMPERATIVES` en
   `chunk.py` tiene que atrapar; si aparece un verbo que no está en esa lista
   (`Elabore`, `Sustente`, `Contraste`…), **ese es un hallazgo** y se arregla con
   test, como cualquier otro.
3. **Si el PDF tiene capa de texto o es escaneado**. Un escaneado va por OCR y
   cuesta ~8× más; se reporta la estimación por documento.

### 1.2 Escribir el sidecar de diagnóstico

A partir de la lectura, no de la imaginación:

```bash
# libros/<documento>.pdf.diag.json
{
  "on_topic": ["3 preguntas cuya respuesta la AI vio en el documento"],
  "exact": ["4-5 términos que aparecen literalmente en el texto"]
}
```

Regla: cada término de `exact` tiene que haber sido **visto** en el documento leído.
Un sidecar inventado convierte el diagnóstico en un generador de ceros. Si no se
leyó lo suficiente para escribirlo, se lee más — no se adivina. Verificar el
formato con `uv run pytest -q tests/test_second_book.py`, que valida todos los
sidecars del repo.

### 1.3 Dry-run: la estructura, gratis

```bash
uv run docagent index --dry-run --collection docagent_v2 "libros/<documento>.pdf"
```

No gasta en embeddings y ya no persiste perfil (cambio de esta sesión: la arista
`e_needs_tuning` va a `END` en dry-run). Qué mirar en el log, en este orden:

| Línea del log | Criterio de aceptación |
|---|---|
| `extractor: …` | El esperado. `pdf_ocr` en un PDF que se creía digital significa que no hay capa de texto. |
| `profile: reusing … / no match` | ¿Heredó de la familia correcta? Si un libro de Historia hereda de FYC, el fingerprint está colisionando de más y **eso es el hallazgo**. |
| `eval set dropped: …` | Debe aparecer si reusó perfil de **otro** archivo. Si reusa y NO aparece, la corrección de esta sesión no está actuando. |
| `heading_guards … N chapters numbered [...]` | La secuencia tiene que coincidir con la estructura leída en 1.1. |
| conteo de párrafos y de chunks | Un salto de orden de magnitud respecto a documentos parecidos de la familia apunta al bug de la separación de párrafos por *pitch*. |

**Sobre la nota de `[2, 5, 6, 7]`: sigue sin reproducirse, y "no se reproduce"
no es "está arreglado".** El check actual (`rules._check_heading_guards`) solo
rechaza duplicados y no-monotonía, y `[2,5,6,7]` no es ninguna de las dos, así que
pasaría. En la corrida de julio 2026 no llegó a ejecutarse: el libro 2 reusó el
perfil del libro 1 y la validación se saltó entera. `CLAUDE.md` ya lo registra como
**latente**. Si en esta corrida un documento aprende reglas propias y `heading_guards`
falla, esa secuencia es el dato que faltaba: anótala tal cual, con el documento y la
estructura leída en 1.1.

Si `heading_guards` falla de verdad: el grafo reintenta 3 veces y luego adopta los
defaults (`n_fallback_rules`) — **eso no es fallo de corrida**, pero sí un hallazgo
que va al informe con la secuencia detectada y la esperada.

### 1.4 Indexar de verdad

```bash
uv run docagent index --collection docagent_v2 "libros/<documento>.pdf" 2>&1 | tee "logs/<slug>.log"
```

Recomendaciones operativas, todas pagadas con dolor en corridas anteriores:

- **Un documento por invocación** para el cabeza de serie. En lote, un fallo de
  reglas se descubre después de haber gastado en los demás.
- **`--recreate` solo en el primerísimo documento** de toda la corrida, para crear
  `docagent_v2` limpia. Después nunca: el propio CLI lo desactiva tras el primer
  documento de un lote, pero entre invocaciones separadas hay que acordarse.
- **Correr en background y no quedarse mirando.** La corrección son ~19 batches
  secuenciales, decenas de minutos. Los `ReadTimeout` de la API son transitorios y
  se reintentan solos (3 reintentos en 19 batches en el libro de referencia, todos
  recuperados); los reintentos se imprimen a stderr. **450 s al 0% de CPU se ve
  exactamente igual que un cuelgue** — si el log no avanza en ~10 min, mirar si el
  proceso sigue vivo antes de matarlo.
- **Anotar el `thread:` del encabezado.** Si hay que interrumpir, se reanuda con
  `--resume <thread>`; sin él, la corrida empieza de cero y vuelve a pagar.
- La caché de corrección persiste **por batch**, así que una interrupción conserva
  lo pagado.

### 1.5 Validar el resultado — cinco comprobaciones, no una

Ninguna se salta. Las métricas solas ya demostraron ser insuficientes.

**(a) La colección tiene lo que debería.**

```bash
uv run docagent profiles --verbose
curl -s localhost:6333/collections/docagent_v2 | head -40
```

`points_count` debe crecer aproximadamente en el número de chunks que reportó el
log. Si no crece, se indexó en otra colección.

**(b) Invariante #1: `char_span` son offsets de *bytes* del archivo corregido.**
No hay auditoría en tiempo de ejecución, solo tests, así que aquí se hace a mano
con un script de lectura (`qdrant.scroll_all` ya existe):

```python
# scratchpad/audit_spans.py — leer, no escribir
from docagent.qdrant import Qdrant
with Qdrant("http://localhost:6333", "docagent_v2") as q:
    pts = q.scroll_all()
for p in pts[:20]:
    pl = p["payload"]
    if not pl.get("corrected_file"):
        continue
    raw = open(pl["corrected_file"], "rb").read()
    a, b = pl["char_span"]
    assert raw[a:b].decode("utf-8") == pl["text"], pl["chunk_index"]
print(f"{len(pts)} puntos, spans verificados")
```

Que falle aquí significa que el span se calculó sobre el texto sin corregir, o que
se cortó el archivo original. Es el bug que el invariante #1 existe para atrapar.

**(c) Diagnóstico estructural, en los dos modos.**

```bash
uv run docagent diag --collection docagent_v2 --doc "libros/<documento>.pdf"
uv run docagent diag --collection docagent_v2 --doc "libros/<documento>.pdf" --dense-only
```

La primera línea de la salida dice de dónde salió la suite. Si imprime
`NO SIDECAR`, se olvidó el paso 1.2 y **los números de abajo no valen**.
Criterios: las consultas `EN TEMA` recuperan algo con secciones diversas; las
`RUIDO` salen `(gated)` o `(below floor)`. Una consulta de ruido que puntúa como
una en tema es un fallo del gating (invariante #9). Los scores híbridos son rangos
RRF, no cosenos: **nunca** comparar un score híbrido contra uno dense-only.

**(d) Métricas, y el hueco entre los dos modos.**
El log de `index` ya imprime `recall@1 / recall@5 / MRR@10 / dense-only recall@5 /
noise floor`. Tres lecturas obligatorias:

- `recall@5 ≥ 0.85` es el objetivo, pero es **un umbral elegido sin línea base
  medida**. Quedar por debajo se reporta, no se "arregla" a la fuerza.
- `noise_floor` alto respecto a las métricas reales invalida todo lo demás.
- **El hueco híbrido − dense-only *es* la medida de fuga de vocabulario** del
  evalset sintético. Si híbrido gana mucho más aquí que en el diagnóstico
  estructural, la ganancia es artefacto del generador de preguntas. Reportarlo
  como tal.

**(e) La comparación contra la colección vieja.** Solo aplica a los dos libros ya
indexados, y es la razón de ser de `docagent_v2`:

```bash
uv run docagent diag --collection docagent   --doc "libros/done/1. …pdf"
uv run docagent diag --collection docagent_v2 --doc "libros/done/1. …pdf"
```

Ojo: los evalsets de las dos colecciones pueden ser distintos (el arreglo de esta
sesión regenera el del libro 2). Comparar métricas de evalsets distintos mide las
preguntas, no el cambio. Cuando difieran, comparar **solo** el diagnóstico
estructural, que no depende del evalset, y decirlo explícitamente en el informe.

**(f) Colisión de fingerprint entre familias — la comprobación que faltaba.**
Es la única de las seis que las métricas no pueden hacer por ti, y la que la
corrida de julio 2026 se saltó siete veces seguidas.

Para **cada** documento que reusa perfil, comparar tres cosas contra la lectura de
1.1 y contra la familia de la que hereda:

| Señal | Qué significa una discrepancia |
|---|---|
| `learned from <ruta>` en el log | Si la ruta es de **otra familia temática**, el fingerprint colisionó. No es fatal por sí solo — el fingerprint es estructural a propósito — pero obliga a las dos filas siguientes. |
| capítulos heredados vs. capítulos leídos en 1.1 | Discrepancia = el documento se troceó con los `heading_guards` de otro libro. **Esto invalida el veredicto aunque recall@5 pase.** |
| conteo de chunks por documento comparable | Un capítulo de 60 chunks con guards de un libro de 114 no está midiendo lo mismo. |

Si los capítulos no coinciden: **re-indexar ese documento con `--force-tune`** para
que aprenda sus propias reglas, y reportar los dos resultados (heredado vs.
aprendido) en el informe. Es la única excepción autorizada a la prohibición de
`--force-tune` de la sección final, y solo por esta causa.

Regla de escritura del informe: la columna "veredicto" **no puede decir OK** si la
columna "capítulos detectados vs. leídos" no coincide. Si no se re-indexó todavía,
el veredicto es `ESTRUCTURA DUDOSA`, con la explicación al lado. Un OK con 4 vs 1
es exactamente el reporte sano con números inválidos que este runbook existe para
evitar.

**(g) Los perfiles escritos son los que se esperaba.**

```bash
uv run docagent profiles --verbose
ls profiles/
```

Tres cosas, después de cada lote:

- **Un archivo por documento indexado**, con el slug derivado de *ese* documento.
  Un archivo cuyo nombre no corresponde a su `learned_from` es el bug ya arreglado
  reapareciendo — para en seco y repórtalo.
- **Ningún perfil con `scores` todos en 0.0.** Es un perfil que se guardó sin haber
  medido; borrarlo, no dejarlo, porque compite por el mismo fingerprint.
- **Dos archivos con el mismo fingerprint es normal ahora** (uno por documento de la
  familia). Gana el `learned_at` más reciente. Si eso no es lo que quieres para el
  siguiente documento, el orden de indexación importa: indexa la familia entera de
  corrido.

### 1.6 Pruebas y regresión

Después de cada familia:

```bash
uv run pytest -q          # 118 passed, incluido test_port_fidelity
```

`tests/test_port_fidelity.py` es la prueba fuerte y gratis: exige 328 chunks,
kinds 309/10/9 y spans byte-exactos sobre el texto de referencia del pipeline Go.
**Si se toca `chunk.py` y este test cambia de resultado, el cambio rompió el port**,
por bien que se vea en el libro nuevo.

---

## Fase 2 — El resto de la familia

Con el perfil ya aprendido, los siguientes documentos de la familia son casi
gratis y se pueden indexar en una sola invocación:

```bash
uv run docagent index --collection docagent_v2 "libros/<doc2>.pdf" "libros/<doc3>.pdf" …
```

Pero cada uno necesita **su propio sidecar** (paso 1.2) y su propia lectura mínima,
porque el sidecar es dato de un documento, no de la familia. Y en el log hay que
confirmar, para cada uno, que aparece `eval set dropped` cuando reusa perfil de otro
archivo. Un documento que reusa perfil **y** conserva el evalset ajeno es exactamente
el bug que dio 0.000 en el libro 2.

Verificación de que el ahorro es real: el segundo documento de una familia debe
mostrar 0 llamadas de aprendizaje de reglas en `costo.json` (`propose`/`validate` sin
incremento).

---

## Fase 3 — Corregir errores encontrados

Protocolo, no improvisación. Para cada fallo:

1. **Reproducir en `--dry-run`** si es estructural (no cuesta nada) o con un test si
   es de lógica.
2. **Escribir primero el test que falla.** Convención del repo: nombre que describe
   el bug, docstring que cuenta qué se observó realmente
   (`test_a_numbered_imperative_is_a_review_question_not_a_chapter`).
3. Arreglar en el módulo que corresponda: `chunk.py` (estructura), `rules.py`
   (aprendizaje y validación), `correct.py` (ortografía), `graph.py` (estado y
   flujo), `cli.py` (interfaz).
4. **`uv run pytest -q` completo.** El arreglo que rompe `test_port_fidelity` o
   `test_invariants` no es un arreglo.
5. **No commitear.** Dejar el árbol sucio y describir el cambio en el informe.
6. Si el arreglo mejora la estructura pero empeora las métricas dentro del margen
   de ruido, **no decidir solo**: ese es el trade-off que el usuario ya resolvió a
   mano una vez (heading_level: recall@5 0.950 → 0.925). Reportar ambos números y
   preguntar.

Fallos esperables, con su tratamiento:

| Síntoma | Tratamiento |
|---|---|
| `heading_guards` falla tras 3 intentos | No es fallo de corrida: adopta defaults. Reportar secuencia detectada vs. leída. |
| Verbo imperativo no reconocido | Añadir a `IMPERATIVES` en `chunk.py` (una sola copia; `rules.py` la importa) + test. |
| Patrones de `kind` caen a defaults | Normal y documentado: la extracción cruda fusiona notas al pie con el cuerpo. Reportar, no arreglar. |
| `ReadTimeout` repetidos | Transitorios y del servidor. Se reintentan solos. Solo escalar si una corrida no completa tras reintentos. |
| Métricas ≈ 0 | Sospechar evalset ajeno o filtro `--doc` sin hashear antes que el retrieval. |
| Capítulos heredados ≠ leídos, métricas buenas | Colisión de fingerprint entre familias. Re-indexar ese documento con `--force-tune` y reportar heredado vs. aprendido. Ver 1.5(f). |
| Perfil con `scores` todos en 0.0 | Se guardó sin medir. Borrarlo: compite por el mismo fingerprint que el bueno. |
| Un perfil cuyo nombre no corresponde a su `learned_from` | El bug de slug heredado reapareciendo. Parar y reportar. |
| Documento falla entero | El CLI continúa con el resto y devuelve 1 al final. Registrar y seguir. |

---

## Fase 4 — Informe final

Al cierre, en `INFORME_INDEXACION.md`:

- **Tabla por documento**: familia, extractor, perfil (heredado o aprendido),
  chunks, capítulos detectados vs. leídos, recall@1/@5, MRR@10, dense-only
  recall@5, noise floor, veredicto.
- **Tabla de costos por documento y por etapa**, de `costo.json`. El presupuesto es
  sin tope por decisión explícita, así que el informe es el único control: si el
  total sale un orden de magnitud sobre las corridas previas ($0.0224 por libro de
  175 páginas), decirlo en la primera línea, no en un anexo. Recordar que los
  precios de `ledger.py` vienen de agregadores terceros, no de la página de precios
  de Google: los tokens están medidos, los multiplicadores son de segunda mano.
- **Documentos excluidos** y por qué (duplicados, plantillas, escaneados).
- **Hallazgos**, cada uno con el test que lo pinea.
- **Comparación v1 vs v2** en diagnóstico estructural.
- **Lo que quedó pendiente**, explícitamente. Ya se saben dos:
  - Un perfil guarda el evalset de un solo documento, así que alternar entre libros
    de una familia regenera preguntas cada vez. Cuesta llamadas, no corrompe métricas.
  - El fingerprint es estructural y agrupa familias temáticamente distintas
    (`110b1333` cubre Historia de la Iglesia y Hermenéutica). Esto **sí** corrompe:
    hereda `heading_guards` ajenos y las métricas no lo detectan. Cada colisión
    observada va al informe con capítulos heredados vs. leídos.

## Archivos que se van a tocar

- `libros/**/<documento>.pdf.diag.json` — nuevos, uno por documento indexado.
- `INFORME_INDEXACION.md` — nuevo, incremental.
- `profiles/*.json`, `costo.json`, `state/checkpoints.sqlite` — los escribe el agente.
- `tests/test_*.py` — solo si hay hallazgos, un test por hallazgo.
- `docagent/{chunk,rules,graph,cli}.py` — solo con test que lo respalde.
- `CLAUDE.md` — corregir la nota de `[2, 5, 6, 7]` con lo reproducido.

## Lo que NO debe hacer

- Commitear, hacer push, o abrir PR.
- Escribir en la colección `docagent` (la referencia v1).
- Inventar contenidos de sidecar sin haber leído el documento.
- Comparar scores híbridos con dense-only, o métricas sobre evalsets distintos.
- Tocar `evaluate.CHUNK_CANDIDATES` / usar `--force-tune` sin pedirlo — **con una
  sola excepción, la de 1.5(f)**: un documento cuyos capítulos heredados no
  coinciden con los leídos. Fuera de ese caso: cada
  candidato de chunking cuesta un re-embed completo, y con σ≈0.358 el margen
  bootstrap necesita ~80 preguntas para ver un efecto de +0.040 MRR — por debajo de
  eso el bucle no puede recuperar un parámetro saboteado, y negarse a actuar sobre
  un efecto indistinguible del ruido es el diseño funcionando.
- Usar `docagent eval`: aparece en el docstring del CLI pero **no está cableado**
  (los subcomandos reales son `index`, `query`, `profiles`, `diag`).
