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

### Estado real al 2026-08-29

| Qué | Cuánto |
|---|---|
| Puntos en `docagent_v2` | **2.047** |
| Perfiles en `profiles/` | 29 |
| `INFORME_INDEXACION.md` | **existe**, reconstruido: 28 documentos + la familia nueva |

`01_RetoDeDios_INT-S.pdf` (304 páginas, Editorial Vida) se indexó el 2026-08-29
como cabeza de la familia `2bde8a7f`, y es el **primer documento del corpus con
un veredicto OK completo**: 30 capítulos detectados contra 30 leídos, 600 de 600
spans byte-exactos auditados. Costó ≈$1.383, de los cuales $0.086 se pagaron dos
veces por defectos del motor que esa corrida destapó — todos arreglados y
listados abajo.

### Estado al 2026-08-28 — la corrida ya empezó

Medido, no recordado. El runbook está escrito como si nada hubiera comenzado, y
no es así:

| Qué | Cuánto |
|---|---|
| Puntos en `docagent_v2` | **1.447** |
| Perfiles en `profiles/` | 28 |
| Sidecars `*.diag.json` | 39 |
| PDFs en `libros/` · en `libros/done/` | 68 · 16 |
| `INFORME_INDEXACION.md` | **no existe** |

Dos consecuencias inmediatas. **`--recreate` ya no es seguro en ningún
documento**: la instrucción de 1.4 («solo en el primerísimo») se escribió cuando
`docagent_v2` estaba vacía, y hoy borraría 1.447 puntos y el trabajo pagado que
representan. Si de verdad hace falta empezar de cero, es una decisión explícita,
no un flag de arranque. Y **el entregable incremental no se está escribiendo**:
la sección "una por familia, al terminar cada lote" existe precisamente para que
una interrupción no pierda el trabajo, y hay 28 perfiles sin una línea de informe
detrás. Reconstruirlo desde `logs/`, `profiles/` y `costo.json` es la primera
tarea de quien retome esto.

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
cd /home/kheiron/yorch/docaget          # ojo: el directorio es `docaget`, el paquete `docagent`
uv sync
uv run pytest -q                        # debe dar 142 passed, 13 skipped. Si no, PARA.
docker compose up -d                    # el compose vive aquí, no en ../sociologia
curl -s localhost:6333/readyz           # Qdrant del motor, arriba antes de gastar un centavo
uv run python -c "import google.auth; print(google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])[1])"
```

**No hay `.env` en `docaget/` y no debe haberlo.** La versión anterior de este
bloque decía `grep -c API_KEY .env`; es de cuando el motor usaba una clave de
express mode. `vertex.py` usa **ADC** (`google.auth.default`), igual que
`brainworker/providers/gemini.py`, y la regla de la raíz es explícita: Gemini
Enterprise rechaza las API keys y reintroducir una ruta de clave no se arregla
con configuración. Verificado el 2026-08-28.

Lo que sí hay que fijar es el proyecto de facturación: ADC en esta máquina
reporta `verveux`, que **no** es donde se factura este trabajo. Exportar
`PROJECT_ID=yorch-platform-prod` (el mismo de `infra/.env`) en cada invocación;
sin él, `vertex.py` cae al proyecto por omisión de ADC.

`142 passed, 13 skipped` es la línea base. Si un test falla **antes** de tocar
nada, el arreglo de eso es la tarea, no la indexación.

Tres correcciones de ruta, porque las tres versiones anteriores de este bloque
mandaban a sitios que ya no existen:

- El motor vive en **`/home/kheiron/yorch/docaget`**. `/home/kheiron/facter/docagent`
  sigue existiendo y es una trampa: es el checkout viejo, con el árbol de trabajo
  vaciado (todo en estado `D` en git) y sólo `.env` y `.venv` en pie, así que
  `uv sync` allí funciona y `uv run pytest` no encuentra nada que correr.
- El `docker-compose.yml` está **en `docaget/`**. Levanta el contenedor
  `sociologia-qdrant` en 6333, que es el nombre histórico y no un error.
- Los subcomandos reales son `index`, `query`, `profiles`, `diag`. `docagent eval`
  aparece en el docstring del CLI y no está cableado.

### Dos Qdrant, dos organizaciones, y el motor no sabe de ninguna de las dos

Lo que más ha cambiado alrededor de este runbook desde que se escribió, y lo que
puede corromper datos reales sin que nada falle.

**Hay dos instancias de Qdrant vivas en esta máquina**, y no son la misma cosa:

| Puerto | Instancia | Colecciones | Quién escribe |
|---|---|---|---|
| **6333** | `sociologia-qdrant`, el compose de `docaget/` | `docagent`, `docagent_v2`, `docagent_corr`, `sociologia`, `pruebas`, `sabotaje` | **este runbook**, vía CLI |
| **6433** | el stack de Company Brain (`infra/`) | `brain` | el worker de Temporal, nunca el CLI |

`DEFAULT_QDRANT` en `cli.py` es `http://localhost:6333`, así que por omisión se
apunta al sitio correcto. La bandera `--qdrant` puede apuntar al otro, y ahí está
el peligro.

**El CLI nunca escribe en `brain`, por ninguna vía y bajo ninguna organización.**
No es una preferencia, son dos daños distintos y ambos silenciosos:

1. **Un punto escrito por el CLI no lleva `tenant_id`.** El motor no tiene el
   concepto — no hay una sola referencia a tenencia en `docagent/qdrant.py` ni en
   `graph.py`. Y `answering/retrieve.py` **siempre** asigna un filtro de
   organización, después de la allowlist. Un punto sin `tenant_id` en `brain` por
   tanto no lo encuentra nadie: ni el plano libre ni el de pago. Se paga el
   embedding y el punto es inalcanzable, sin error en ninguna parte.
2. **`brain` tiene 4.734 puntos reales** — 4.721 del corpus heredado y 13 de la
   organización de verificación. Un `--recreate` apuntado ahí los borra todos, y
   sólo 31 de las 69 versiones conservan su fichero de origen en disco: las otras
   38 exigirían volver a pagar el pipeline completo, corrección incluida.

**Y nada de esto se indexa en `acme`.** `acme`
(`tnt_015ae4252056eddd0156555f`) es la organización que se sembró el 2026-08-28
para verificar el plano de pago de punta a punta; contiene un solo documento y su
razón de ser es ser pequeña y conocida. Meter el corpus ahí destruye lo único
contra lo que se puede comprobar el aislamiento entre organizaciones. El corpus
pertenece al tenant heredado, `tnt_000000000000000000000001`, y el camino para
llevarlo ahí es el pipeline del worker, no este runbook.

En corto: en esta corrida, `--qdrant` se deja en su valor por omisión y
`--collection` empieza por `docagent_`. Si alguna vez se necesita indexar contra
el producto, es por `POST /ingest`, no por el CLI.

### El embedding: qué modelo, y por qué la cuota manda el reloj

Medido el 2026-08-28 y 29 contra el endpoint real con ADC. Esto cambió el diseño
de cómo se corre una indexación, así que va antes del inventario.

**El modelo es `gemini-embedding-001` y no se toca.** `vertex.py` apuntaba a
`gemini-embedding-2` con este razonamiento escrito al lado: "listar los modelos
del endpoint el 2026-08-19 devolvió 23 ids, de los cuales este es el único de
embedding". **Listar un modelo no es tener acceso a él:**

| Proyecto | `gemini-embedding-2` | `gemini-embedding-001` |
|---|---|---|
| `yorch-platform-prod` | 404 | **200** |
| `verveux` (el de ADC por omisión) | 403 | 403 |

Una corrida murió en ese 404 tras pagar $1.27 de corrección, sin indexar nada.
Pero la disponibilidad es la mitad menor del argumento: **todos los puntos de
`docagent_v2` están embebidos con `-001`, y los dos modelos miden 3.072**, así
que Qdrant aceptaría los vectores del otro sin un solo error y el coseno entre
embeddings de dos modelos distintos no significa nada. Log sano, colección que
acepta, ranking corrupto. Cambiar de modelo de embedding es **re-embeber la
colección entera**, no editar una constante.

**La cuota es el cuello de botella, y se mide por minuto.** El error nombra la
métrica exacta:

```
Quota exceeded for aiplatform.googleapis.com/online_prediction_requests_per_base_model
with base model: gemini-embedding
```

`serviceusage` reporta su unidad como `1/min/{project}/{base_model}`. Ritmo
sostenido medido sobre las marcas de tiempo de la caché: **~6 embeddings/minuto**.
Consecuencias operativas, todas verificadas:

- Un libro de 600 chunks son **~100 minutos de reloj mínimo**. Ninguna corrida
  cabe en una sesión de 10 minutos, que es el tope de una llamada de herramienta.
  Tres corridas se dieron por muertas en esta sesión cuando lo único que pasaba
  es que el observador se iba antes: log sin traza, sin 429, detenidas en
  `embedding 1/600`. Es la versión inversa del aviso de 1.4 sobre los 450 s al
  0% de CPU.
- **Más hilos no ayudan.** Contra un cubo por minuto, `--workers 2` duplica la
  ráfaga sin duplicar el rendimiento y adelanta la pared. Usar `--workers 1`.
- Dos corridas simultáneas del mismo documento se reparten la misma cuota y
  ninguna avanza. Comprobar con `pgrep -cf "docagent index"` antes de relanzar.

**Hay caché de embeddings desde el 2026-08-29, y es lo que hace que esto
converja.** `Vertex.embed` guarda cada vector en `cache/embed/` (un fichero por
clave: modelo + dimensiones + `task_type` + texto). Antes no existía, y como
`embed_many` lanza excepción cuando un texto agota sus reintentos, una corrida
que moría en 586 de 600 tiraba los 586 — y volvía a gastar 586 unidades de una
cuota por minuto para llegar a la misma pared. Así una corrida no converge
nunca. Con la caché, cada vuelta pide estrictamente menos que la anterior.

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

### 1.3 Dry-run: la estructura, casi gratis

```bash
PROJECT_ID=yorch-platform-prod uv run docagent index --dry-run --collection docagent_v2 "libros/<documento>.pdf"
```

**No es gratis, y el título anterior de esta sección decía que sí.** El dry-run
no embebe ni genera evalset, pero el aprendizaje de reglas son llamadas al
modelo: medido el 2026-08-28 sobre `01_RetoDeDios_INT-S.pdf`, **$0.0187 cuando
la validación falla tres veces** y $0.0061 cuando pasa al primer intento. Es
barato comparado con la corrida completa y sigue siendo la decisión correcta
antes de gastar; simplemente no es cero, y un dry-run repetido sobre un
documento que no aprende reglas se paga cada vez.

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

**No se lanza en primer plano ni con `tee`.** Un libro de 600 chunks son ~100
minutos por la cuota de embedding (ver Fase 0), y cualquier envoltorio con reloj
lo mata a mitad. Lanzar en su propio grupo de procesos, y dejar que un supervisor
lo reintente:

```bash
setsid env PROJECT_ID=yorch-platform-prod \
  uv run docagent index --collection docagent_v2 --workers 1 \
  "libros/<documento>.pdf" > "logs/<slug>.log" 2>&1 < /dev/null & disown
```

`setsid` no es adorno: sin él, detener el envoltorio manda la señal a todo el
grupo y se lleva la indexación por delante. Pasó tres veces el 2026-08-28.

Para un documento que ya chocó con la cuota, el patrón que converge es un bucle
que reanuda hasta que `rc=0`, seguro **porque** existe la caché de embeddings
(`scratchpad/supervisa.sh` en `docaget/` es la versión usada):

```bash
for vuelta in $(seq 1 40); do
  antes=$(ls cache/embed | wc -l)
  uv run docagent index --collection docagent_v2 --workers 1 \
      --resume "<thread>" "libros/<documento>.pdf" >> "logs/<slug>.log" 2>&1 && break
  despues=$(ls cache/embed | wc -l)
  # sin avance en una vuelta entera = la pared no es de ritmo sino de presupuesto
  [ "$antes" = "$despues" ] && [ $vuelta -gt 2 ] && break
  sleep 90   # la cuota se mide por minuto: sin pausa, la vuelta siguiente
done          # arranca contra un cubo vacío y muere en el primer chunk nuevo
```

Termina, y no es obvio que lo haga: cada vuelta pide estrictamente menos que la
anterior porque lo conseguido quedó cacheado. El progreso es monótono.

**La medida fiable del avance es `ls cache/embed | wc -l`, no el log.** El
contador `embedding N/600` sólo se imprime en 1 y luego cada 50
(`graph.py:466`), y si se ha truncado el log al relanzar puede quedar desfasado.

Recomendaciones operativas, todas pagadas con dolor en corridas anteriores:

- **Un documento por invocación** para el cabeza de serie. En lote, un fallo de
  reglas se descubre después de haber gastado en los demás.
- **`--recreate`: ya no, en ningún documento.** La instrucción original («solo en
  el primerísimo») valía cuando `docagent_v2` estaba vacía. Hoy tiene 1.447 puntos
  y el flag los borra. El CLI lo desactiva tras el primer documento *de un lote*,
  lo que no protege entre invocaciones separadas — que es como se corre esto.
  Empezar de cero es una decisión que se pide, no un flag que se arrastra.
- **Correr en background y no quedarse mirando.** La corrección son ~19 batches
  secuenciales, decenas de minutos. Los `ReadTimeout` de la API son transitorios y
  se reintentan solos (3 reintentos en 19 batches en el libro de referencia, todos
  recuperados); los reintentos se imprimen a stderr. **450 s al 0% de CPU se ve
  exactamente igual que un cuelgue** — si el log no avanza en ~10 min, mirar si el
  proceso sigue vivo antes de matarlo.
- **Anotar el `thread:` del encabezado.** Si hay que interrumpir, se reanuda con
  `--resume <thread>`; sin él, la corrida empieza de cero y vuelve a pagar.
- La caché de corrección persiste **por batch**, y desde el 2026-08-29 los
  embeddings también (`cache/embed/`), así que una interrupción conserva todo lo
  pagado. Es lo que hace reanudable una corrida que choca con la cuota.
- **Copiar `costo.json` al terminar cada documento**:
  `cp costo.json logs/<slug>.costo.json`. `ledger` hace `json.dump` sobre el
  mismo fichero en cada corrida (`ledger.py:152`), así que `costo.json` guarda
  **una sola corrida**: la última. Sin esta copia, la "tabla de costos por
  documento" que pide Fase 4 no se puede construir — y de hecho no se pudo:
  27 de los 28 documentos ya indexados no dejaron ningún rastro de gasto.

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

**Antes de llamarlo fallo del gating, mirar el margen.** En
`01_RetoDeDios_INT-S.pdf` dos de cuatro consultas de ruido pasaron el filtro en
los dos modos: "recetas de cocina italiana con berenjena" (0.606 dense) y "el
mantenimiento de bicicletas de montaña" (0.633), con `noise_floor = 0.6328` y las
consultas en tema entre 0.718 y 0.758. No es que el filtro esté roto: **el margen
entre ruido y tema en ese documento es de tres centésimas**, porque un ensayo
sobre cultura, sociedad y vida cotidiana está semánticamente más cerca de
"recetas" que un manual de teología sistemática. Se reporta con los cuatro
números al lado; **no se mueve el suelo por un documento**, que es exactamente la
clase de cambio que este runbook exige medir antes.

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
- **Y el caso simétrico, que no estaba previsto: un hueco de +0.000 no significa
  que BM25 no aporte nada.** Medido en `01_RetoDeDios_INT-S.pdf` el 2026-08-29:
  el evalset da híbrido − dense-only = **+0.000 recall@5**, mientras el
  diagnóstico estructural encuentra **5 de 5** términos literales en rank 1 en
  híbrido contra **1 de 5** en dense-only. `esencialismo`,
  `Parálisis teológica`, `Mestizaje espiritual` y `hermanos separados` los
  recupera la pierna léxica y no la densa.

  La causa es la misma que la de la fuga, vista del otro lado: las preguntas
  sintéticas se escriben *parafraseando* el chunk, así que rara vez contienen su
  vocabulario raro — miden el motor denso y son ciegas a la mitad léxica. Por eso
  **el hueco del evalset nunca se lee solo**: la conclusión sobre BM25 sale de
  comparar los dos números, y el del diagnóstico es el que tiene términos que
  escribió una persona leyendo el libro.

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
- **Ningún perfil cuyo `recall@5` quede por debajo de su propio `noise_floor`.**
  La regla anterior decía "ninguno con `scores` todos en 0.0" y no atrapa el
  caso real: `3-carlomagno-…-cc7db18c` guarda `recall@5 = 0.05` y
  `MRR@10 = 0.0251` mientras su propio log reporta 0.875 y 0.656. No son ceros,
  así que pasaban. Un `recall@5` por debajo del suelo de ruido registrado en el
  mismo fichero es incoherente por construcción, y ese perfil se convierte en la
  línea base del bucle de tuning para todo documento que caiga en su
  fingerprint: con MRR 0.025 de base, cualquier candidato "gana". Borrarlo o
  re-medirlo, no dejarlo.
- **Dos archivos con el mismo fingerprint es normal ahora** (uno por documento de la
  familia). Gana el `learned_at` más reciente. Si eso no es lo que quieres para el
  siguiente documento, el orden de indexación importa: indexa la familia entera de
  corrido.

### 1.6 Pruebas y regresión

Después de cada familia:

```bash
uv run pytest -q          # 153 passed, 15 skipped, incluido test_port_fidelity
```

Verificar con el código de salida, no leyendo la última línea: redirigir a un
fichero y mirar `$?`.

`tests/test_port_fidelity.py` es la prueba fuerte y gratis: exige 328 chunks,
kinds 309/10/9 y spans byte-exactos sobre el texto de referencia del pipeline Go.
**Si se toca `chunk.py` y este test cambia de resultado, el cambio rompió el port**,
por bien que se vea en el libro nuevo.

**Indexar puede poner tests en rojo sin que se haya tocado código, y hay que
saberlo antes de investigar el sitio equivocado.** `tests/corpus.py` resuelve el
documento de las pruebas de propiedad como el mayor `libros/**/*.corrected.txt`
de al menos 120 KB. Indexar `01_RetoDeDios_INT-S.pdf` escribió un
`.corrected.txt` de 476 KB, que pasó a ser el mayor, y dos tests de
`test_adversarial_validation` se pusieron en rojo a mitad de sesión. Se perdieron
veinte minutos restaurando ficheros desde la copia de scratch para descartar que
fuera un cambio propio.

La causa era real y del documento, no del código: los dos afirmaban `v.passed`
para una propuesta escrita a mano, y este editorial **numera sus notas al pie con
punto** (`2. Ibídem.`), así que la vía numérica encuentra capítulos duplicados y
`heading_guards` falla — correctamente. Quedaron marcados `reference_corpus`, que
es el mecanismo que el repo ya tenía, en vez de aflojar la aserción.

La regla general, que `doc/CLAUDE.md` ya enuncia y esta sesión confirmó: al ver
un test rojo tras indexar, **mirar primero la cabecera de pytest**, que nombra el
documento resuelto. Si cambió, el rojo es un hecho sobre el libro nuevo.

**Y los tests no deben escribir en las cachés reales.** `Vertex.embed` cachea en
`cache/embed` relativo al CWD, y la suite corre desde la raíz del repo: dos
invariantes quedaron servidos desde la caché en vez de ejercitar la petición que
existen para comprobar, y dos vectores falsos —uno con `tokens=1000000`, otro
constante— acabaron en la caché que una indexación real da por buena. **Un vector
constante entra en Qdrant como cualquier otro y contamina el ranking sin error en
ninguna parte.** `tests/conftest.py` redirige ahora `EMBED_CACHE_DIR` a un
temporal en cada test, con la misma regla que el `CLAUDE.md` raíz enuncia para la
colección Qdrant. Si alguna vez se sospecha contaminación, los falsos se
reconocen por un `token_count` absurdo o por un vector de valor constante.

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
   **Y no deshacer con `git checkout -- <fichero>`.** Con el árbol sucio eso no
   revierte tu última edición: revierte al último commit, que aquí precede a
   sesiones enteras de trabajo. El 2026-08-28 borró así todos los cambios de
   tenencia de `worker/brainworker/graph/projection.py`, y sólo se recuperaron
   porque el código estaba horneado en la imagen del worker. Copia el fichero a
   un directorio de trabajo antes de experimentar y restaura desde ahí.
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
| `HTTP 429` repetidos en `embed` | Cuota `online_prediction_requests_per_base_model`, por minuto. No es transitoria como un `ReadTimeout`: bajar a `--workers 1` y reanudar en bucle. La caché de embeddings hace que cada vuelta avance. Ver Fase 0. |
| La corrida "muere" sin traza en `embedding 1/600` | Casi siempre la mató un envoltorio con reloj, no el motor. Comprobar con `pgrep -f "docagent index"` y `ls cache/embed \| wc -l` antes de concluir nada. Lanzar con `setsid`. |
| `HTTP 404` en `embed` nombrando el modelo | El proyecto no sirve ese modelo. **No cambiar el modelo para esquivarlo**: el modelo está atado al espacio vectorial de la colección. Ver Fase 0. |
| Capítulos falsos tipo `2. Ibídem.` en los breadcrumbs | El editorial numera sus notas al pie con punto, contra el invariante #12, que se midió en un libro donde ninguna lo lleva. Reportar; arreglarlo movería `test_port_fidelity`. |

---

## Fase 4 — Informe final

Al cierre, en `INFORME_INDEXACION.md`:

- **Tabla por documento**: familia, extractor, perfil (heredado o aprendido),
  chunks, capítulos detectados vs. leídos, recall@1/@5, MRR@10, dense-only
  recall@5, noise floor, veredicto.
- **Tabla de costos por documento y por etapa**, de las copias
  `logs/<slug>.costo.json` (ver 1.4: `costo.json` se sobrescribe en cada corrida).
  El presupuesto es sin tope por decisión explícita, así que el informe es el
  único control. Recordar que los precios de `ledger.py` vienen de agregadores
  terceros, no de la página de precios de Google: los tokens están medidos, los
  multiplicadores son de segunda mano.

  **La referencia de $0.0224 por libro de 175 páginas ya no sirve como umbral, y
  la diferencia es de un orden de magnitud.** Esa cifra se midió con
  `gemini-2.5-flash` ($0.15 / $1.25 por millón). El motor llama hoy a
  `gemini-3.6-flash` ($1.50 / $7.50): seis veces el precio de salida, que es
  donde está la factura de este trabajo. Medido el 2026-08-28 sobre
  `01_RetoDeDios_INT-S.pdf`, 304 páginas: **corrección $1.27** (20 llamadas,
  140.040 in / 141.583 out), evalset $0.048, propose $0.006.

  Consecuencia para el informe: **los 28 documentos indexados hasta el
  2026-07-31 se midieron con los precios viejos y sus cifras no son comparables
  con las nuevas.** Un total que "sale un orden de magnitud sobre las corridas
  previas" es hoy el resultado esperado del cambio de modelo, no una señal de
  nada. El umbral útil es por página contra otra corrida con los mismos
  modelos.
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
- `doc/CLAUDE.md` — el documento del motor, no el de la raíz: corregir ahí la nota
  de `[2, 5, 6, 7]` con lo reproducido.
- `INFORME_INDEXACION.md` en `docaget/` — hoy **no existe** pese a 28 perfiles ya
  escritos. Reconstruirlo desde `logs/`, `profiles/` y `costo.json` antes de
  indexar nada más, o el trabajo ya pagado sigue sin rendir cuentas.

## Lo que NO debe hacer

- Commitear, hacer push, o abrir PR. Y **nunca** `git checkout -- <fichero>` para
  deshacer un experimento: con el árbol sucio revierte a un commit que precede a
  todo el trabajo, no a tu última edición. Ver Fase 3, punto 5.
- Escribir en la colección `docagent` (la referencia v1).
- **Cambiar `EMBED_MODEL` en `vertex.py`.** No es una constante de configuración:
  es el espacio vectorial de `docagent_v2`. Los modelos de 3.072 dimensiones son
  intercambiables para Qdrant y no lo son para el coseno, así que el cambio no
  falla, sólo corrompe el ranking. Si hace falta otro modelo, es re-embeber la
  colección entera, y es una decisión que se pide.
- **Subir `--workers` para ir más rápido en el embedding.** La cuota es por
  minuto: más hilos adelantan la pared sin mover el techo.
- **Borrar `cache/embed/` para "empezar limpio".** Es lo único que hace que una
  corrida contra la cuota converja; borrarlo obliga a volver a gastar cuota por
  trabajo ya hecho.
- **Correr la suite esperando que no toque nada del corpus.** Ver 1.6: indexar
  cambia el documento que resuelven las pruebas de propiedad.
- **Escribir en la colección `brain`, o apuntar `--qdrant` al puerto 6433.** Es el
  almacén del producto: un punto escrito por el CLI no lleva `tenant_id` y queda
  inalcanzable para los dos planos, y un `--recreate` allí borra 4.734 puntos de
  los que 38 versiones no se pueden reconstruir sin volver a pagar el pipeline.
- **Indexar cualquier cosa en la organización `acme`.** Es la organización de
  verificación del plano de pago: un documento, conocido, y la única referencia
  contra la que se puede comprobar el aislamiento entre organizaciones. El corpus
  pertenece al tenant heredado y llega ahí por `POST /ingest`, no por el CLI.
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
  (los subcomandos reales son `index`, `query`, `profiles`, `diag`). Verificado de
  nuevo el 2026-08-28 leyendo los `add_parser` de `cli.py`.
- Correr desde `/home/kheiron/facter/docagent`. Es el checkout viejo, vaciado, y
  falla de una forma que parece un problema del entorno.

---

## Arreglos aplicados el 2026-08-28/29, cada uno con test

Sin commitear, como pide Fase 3. Todos con un test que falla antes y pasa
después, y la suite completa en verde (**153 passed, 15 skipped**, incluido
`test_port_fidelity`). Salieron todos de indexar **un** documento, que es el
argumento de este runbook en su forma más corta.

| Fichero | Defecto | Efecto medido |
|---|---|---|
| `chunk.py` | `classify_kind` no consultaba `TOC_LINE_RE`, que `heading_level` ya tenía | 99 líneas del índice etiquetadas `preguntas`, reseteando el section path |
| `chunk.py` | La densidad de `¿` bastaba por sí sola | 62 párrafos de prosa retórica idem. Ahora un solo `¿` necesita un item numerado en el párrafo — exigir dos marcas rompía el caso medido `"Freud\n27. ¿Hasta dónde…"` de `test_port_fidelity` |
| `rules.py` | `heading_guards` descartaba un `heading_l1_pattern` que él mismo había validado, por la secuencia numérica de las notas al pie | 30 capítulos de un libro de 304 páginas, de 0 |
| `rules.py` | Los dos niveles de encabezado comparten nombre de regla, así que un nivel 2 rechazado tiraba el nivel 1 — en `validate` **y** otra vez en `adopt` | idem; es la adopción parcial que el repo ya tenía, sin aplicar entre niveles |
| `extract/pdf_text.py` | Guiones blandos U+00AD partiendo palabras | 2.140 ocurrencias; `reproduc\xadción` indexaba como `reproduc` + `cion` y `propósi\xadto` perdía la cola |
| `vertex.py` | `EMBED_MODEL` apuntaba a un modelo que el proyecto no sirve, y que no es el de la colección | una corrida murió tras pagar $1.27 sin indexar |
| `vertex.py` | Un 429 de cuota se trataba como un fallo de transporte | ~10 s de paciencia contra una ventana de 60; ahora 6 intentos, 2/8/32/60/60 |
| `vertex.py` | Sin caché de embeddings | 586 vectores tirados por corrida, y 586 unidades de cuota re-gastadas para chocar con la misma pared |
| `tests/conftest.py` | La suite leía y escribía la caché de embeddings real | 2 vectores falsos en la caché, uno constante — habría entrado en Qdrant sin un solo error |

Pendiente y sin tocar, con su medición:

- **`3-carlomagno-…-cc7db18c` guarda `recall@5 = 0.05`** mientras su log reporta
  0.875. Su fingerprint tiene un solo perfil, así que ese MRR de 0.025 sería la
  línea base del tuning de cualquier documento que caiga ahí, y con esa base
  cualquier candidato "gana". Diagnosticarlo cuesta re-indexar ese libro.
- **`gemini-embedding-001` admite lote real** — la documentación dice que
  devuelve un embedding por cada texto de la lista, y la regla "un texto por
  petición" se midió sobre `gemini-embedding-2`, que agrega en silencio. Hoy se
  hacen 600 peticiones donde podrían ser bastantes menos, y las peticiones son
  justo el recurso escaso. Requiere medirlo antes de cambiarlo.
- **214 claims huérfanos** y el resto de lo que ya lista `CLAUDE.md`.
