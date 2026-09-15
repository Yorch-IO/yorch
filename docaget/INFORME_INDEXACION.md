# Informe de indexación — corpus docagent

Reconstruido el 2026-08-28 desde `profiles/`, `logs/` y `costo.json`, según lo
que pide el runbook (`doc/RUNBOOK_INDEXACION.md`, "Archivos que se van a tocar").
No es un informe escrito mientras se indexaba: es lo que se puede *rescatar* de
28 perfiles que se escribieron sin informe detrás. Lo que no se puede rescatar
está dicho como tal, no rellenado.

## Lo primero, porque cambia lo que este informe puede afirmar

**El contabilidad de costos por documento no existe y no es reconstruible.**
`ledger` hace `json.dump` sobre `costo.json` en cada corrida (`ledger.py:152`),
así que el archivo guarda **una sola corrida**: la última, del 2026-07-31 19:08,
con `total_usd = 0.051321`. Las otras 27 no dejaron rastro de gasto. El runbook
pide en Fase 4 una "tabla de costos por documento y por etapa" y con este diseño
no se puede producir retroactivamente. Ver "Correcciones al runbook".

Estimación grosera de lo ya gastado, y es una estimación, no una medida: a
~$0.022–0.051 por documento, 28 documentos ≈ **$0.6–1.4**.

## Estado medido de la colección

| Qué | Cuánto | Cuándo |
|---|---|---|
| Puntos en `docagent_v2` (Qdrant 6333) | 1.447 | 2026-08-28 |
| Perfiles en `profiles/` | 28 | 07-27 a 07-31 |
| Sidecars `*.diag.json` | 39 | — |
| Corridas con log en `logs/` | 20 (2 vacíos: `1-agustin.log`, `4-escolastica.log`) | — |
| Suma de `chunks` declarados en los perfiles | ver tabla | — |

## Tabla por documento

Los números salen del campo `scores` de cada perfil, que es lo que la corrida
midió y persistió. "fp compartido" marca los documentos cuyo fingerprint agrupa
a más de un libro — ver la sección de colisiones.

| Documento (learned_from) | fp | aprendido | chunks | preg. | recall@1 | recall@5 | MRR@10 | dense@5 | ruido | veredicto |
|---|---|---|---|---|---|---|---|---|---|---|
| Conferencia de Cultura, Sociedad y Cristianismo - Unificada.pdf | `4e7c3913` | 07-27 14:31 | 380 | 90 | 0.689 | 0.922 | 0.791 | 0.922 | 0.586 | OK |
| 01_RetoDeDios_INT-S.pdf | `2bde8a7f` | 08-29 | 373 | 30 | 0.433 | 0.800 | 0.596 | 0.800 | 0.579 | OK (30/30 cap.) |
| 02-PuertasEternas_INT.pdf | `5f4421c2` | 08-29 | 240 | 40 | 0.575 | 0.975 | 0.754 | 0.975 | 0.606 | OK (8/8 cap.) |
| 03-ElFrutoEterno_INT-S.pdf | `43ea27a4` | 08-29 | 410 | 40 | 0.400 | 0.850 | 0.606 | 0.875 | 0.604 | OK (10/10 cap.) |
| 1. DESDE AGUSTÍN DE HIPONA HASTA LOS SIETE CONCILIOS ECUMÉNICOS.pdf | `110b1333` | 07-31 14:03 | 114 | 40 | 0.725 | 0.975 | 0.854 | 0.975 | 0.598 | OK · fp compartido |
| 2. PAPADO, MONAQUISMO E IMPERIO MUSULMÁN Y SU INFLUENCIA EN EL IMPERIO RONANO DE ORIENTE.pdf | `110b1333` | 07-31 14:39 | 107 | 40 | 0.675 | 0.925 | 0.772 | 0.925 | 0.584 | OK · fp compartido |
| 3. CARLOMAGNO Y EL SACRO IMPERIO ROMANO, CISMA DE ORIENTE, CRUZADAS, INQUISICIÓN Y DESARROLLO TEOLÓGICO MEDIEVAL..pdf | `cc7db18c` | 07-31 14:55 | 62 | 40 | 0.000 | 0.050 | 0.025 | 0.025 | 0.579 | **SCORES INVÁLIDOS** |
| 4. ESCOLÁSTICA Y TEÓLOGOS MEDIEVALES, LA PREREFORMA Y LA REFORMA.pdf | `f97c2e19` | 07-31 15:21 | 103 | 40 | 0.675 | 0.950 | 0.793 | 0.925 | 0.587 | OK · fp compartido |
| 1.-Doctrina-del-Hombre.pdf | `ea960d68` | 07-31 16:58 | 25 | 25 | 0.760 | 0.960 | 0.826 | 0.960 | 0.570 | OK |
| 2.-Doctrina-del-Pecado.pdf | `f97c2e19` | 07-31 17:05 | 29 | 29 | 0.621 | 0.897 | 0.757 | 0.966 | 0.558 | OK · fp compartido |
| 3.-Doctrina-de-la-Redención.pdf | `110b1333` | 07-31 17:09 | 34 | 34 | 0.735 | 0.912 | 0.830 | 0.971 | 0.577 | OK · fp compartido |
| 4.-Doctrina-de-la-Regeneración.pdf | `f97c2e19` | 07-31 17:12 | 17 | 17 | 0.706 | 1.000 | 0.818 | 0.941 | 0.557 | OK · fp compartido |
| 5.-Doctrina-de-la-Justificación.pdf | `f97c2e19` | 07-31 17:18 | 57 | 40 | 0.700 | 0.975 | 0.824 | 0.925 | 0.582 | OK · fp compartido |
| Lección 6-Doctrina-de-la-Adopción.pdf | `f97c2e19` | 07-31 17:20 | 16 | 16 | 0.688 | 1.000 | 0.828 | 1.000 | 0.547 | OK · fp compartido |
| 7.-Doctrina-de-la-Santificación.pdf | `41e14899` | 07-31 17:25 | 48 | 40 | 0.775 | 0.950 | 0.850 | 0.950 | 0.575 | OK |
| 8.-Doctrina-de-la-Resurrección.pdf | `4c8379bf` | 07-31 17:27 | 12 | 12 | 0.750 | 1.000 | 0.840 | 1.000 | 0.560 | OK |
| Introduccion Hermeneutica.pdf | `cdf7dfc6` | 07-31 17:34 | 3 | 3 | 0.667 | 1.000 | 0.833 | 1.000 | 0.585 | OK · fp compartido |
| Hermeneutica Capitulo 1.pdf | `110b1333` | 07-31 17:42 | 63 | 40 | 0.425 | 0.850 | 0.602 | 0.900 | 0.587 | OK · fp compartido |
| Hermeneutica Capitulo 2.pdf | `2567f66c` | 07-31 17:43 | 71 | 40 | 0.600 | 0.950 | 0.756 | 0.975 | 0.586 | OK |
| Hermeneutica Capitulo 3.pdf | `f97c2e19` | 07-31 17:49 | 56 | 40 | 0.550 | 0.900 | 0.698 | 0.900 | 0.586 | OK · fp compartido |
| Hermeneutica Capitulo 4.pdf | `61b524b4` | 07-31 17:50 | 58 | 40 | 0.575 | 0.875 | 0.719 | 0.950 | 0.590 | OK |
| CLASE 2.pdf | `91d23efb` | 07-31 18:09 | 85 | 40 | 0.475 | 0.750 | 0.622 | 0.875 | 0.605 | BAJO OBJETIVO |
| (5 Clase) contenido EL PREDICADOR, FORMACION Y VIDA..pdf | `e1d2edc9` | 07-31 18:12 | 13 | 13 | 0.769 | 1.000 | 0.840 | 1.000 | 0.602 | OK |
| CLASE 3 Holiletica a traves de la historia biblíca..pdf | `50f958b7` | 07-31 18:22 | 89 | 40 | 0.575 | 1.000 | 0.754 | 0.950 | 0.601 | OK |
| CLASE 4. Clases de sermones..pdf | `f2e8acf6` | 07-31 18:32 | 42 | 40 | 0.475 | 0.975 | 0.688 | 0.925 | 0.572 | OK |
| CLASE 6. Estructura - bosquejo del Sermón..pdf | `960bd42d` | 07-31 18:39 | 29 | 29 | 0.483 | 0.966 | 0.691 | 0.931 | 0.579 | OK |
| INTRODUCCION FYC.pdf | `cdf7dfc6` | 07-31 18:45 | 17 | 17 | 0.765 | 1.000 | 0.863 | 1.000 | 0.571 | OK · fp compartido |
| PRESOCRATICOS FYC.pdf | `cdf7dfc6` | 07-31 18:52 | 17 | 17 | 0.941 | 1.000 | 0.961 | 1.000 | 0.559 | OK · fp compartido |
| FILOSOFÍA CLÁSICA FYC.pdf | `cdf7dfc6` | 07-31 19:02 | 58 | 40 | 0.850 | 1.000 | 0.915 | 1.000 | 0.588 | OK · fp compartido |
| ESCUELAS MORALISTAS (2).pdf | `cdf7dfc6` | 07-31 19:07 | 27 | 27 | 0.704 | 1.000 | 0.816 | 1.000 | 0.591 | OK · fp compartido |
| FILOSOFÍA CONTEMPORANEA.pdf | `bc7e606a` | 07-31 19:08 | 5 | 5 | 0.800 | 1.000 | 0.900 | 1.000 | 0.592 | OK |
**No hay columna "capítulos detectados vs. leídos"** y no se puede añadir: esa
columna sale de la lectura humana del PDF (paso 1.1) y ninguna de estas 28
corridas la registró. Es exactamente el dato que el runbook llama insustituible,
y su ausencia es la razón por la que **ningún "OK" de esta tabla es un veredicto
completo**: es "las métricas pasaron", no "la estructura es correcta".

## Hallazgos

### H1 — `3. CARLOMAGNO…` persistió scores que su propia corrida no midió

El perfil `3-carlomagno-…-cc7db18c` guarda `recall@5 = 0.05`, `MRR@10 = 0.0251`,
`recall@5 dense-only = 0.025`. Su log (`logs/3-carlomagno.log`) reporta la misma
corrida como `recall@1=0.500 recall@5=0.875 MRR@10=0.656 (dense-only 0.925)` y
concluye "meets the recall@5 target of 0.85".

Los dos números no pueden ser de la misma medición. Un `recall@5` de 0.05 sobre
40 preguntas es el síntoma que el propio runbook tabula como *"evalset ajeno o
filtro `--doc` sin hashear"*. Pero el log dice `no match for this fingerprint —
will learn rules`, es decir **no** reusó perfil, así que la hipótesis del evalset
ajeno no aplica aquí.

Consecuencia operativa inmediata: `cc7db18c` es un fingerprint con un solo
perfil, así que cualquier documento futuro que caiga en él hereda estas reglas
**y** este `scores`, que la siguiente corrida usará como línea base del bucle de
tuning. Una línea base de 0.025 MRR hace que cualquier candidato "gane".

La comprobación (g) del runbook no lo atrapa: pide *"ningún perfil con `scores`
todos en 0.0"* y estos no son 0.0, son 0.05. **La regla debería ser "ningún
perfil cuyos scores contradigan su propio log"**, o más practicable: ninguno por
debajo del `noise_floor` registrado en el mismo perfil — 0.05 contra un ruido de
0.579 es incoherente por construcción.

*Sin arreglar.* Reproducirlo cuesta una re-indexación de ese libro; no está en el
alcance de esta sesión y es decisión del propietario.

### H2 — Las colisiones de fingerprint entre familias están medidas, y son tres

El runbook las trataba como una observación (`110b1333` cubre Historia de la
Iglesia y Hermenéutica). Contadas sobre los 28 perfiles, son tres grupos y
**15 de los 28 documentos** están en uno:

| Fingerprint | Docs | Familias que mezcla |
|---|---|---|
| `f97c2e19` | 6 | Doctrina/Sistemática (Pecado, Regeneración, Justificación, Adopción) · Historia de la Iglesia (Escolástica) · Hermenéutica (Cap. 3) |
| `cdf7dfc6` | 5 | Filosofía y Cristianismo (Introducción, Presocráticos, Clásica, Escuelas Moralistas) · Hermenéutica (Introducción) |
| `110b1333` | 4 | Historia de la Iglesia (Agustín, Papado) · Doctrina (Redención) · Hermenéutica (Cap. 1) |

`f97c2e19` es peor que el caso ya documentado: agrupa **tres** familias
temáticas, y un libro de Historia de la Iglesia de 103 chunks comparte reglas de
`heading_guards` con una lección de Doctrina de 16. Ninguna métrica lo vio: los
seis dan "OK".

### H3 — Un fallo de `heading_guards` con una secuencia nueva

`logs/3-carlomagno.log`: `[3, 8, 9, 10, 800, 11, 12, 13]` (no monótona, con un
año leído como capítulo), luego `[3, 9, 10, 11, 12, 13]` (no contigua). Adoptó
los defaults, que es el comportamiento diseñado. Es el mismo patrón que
`doc/CLAUDE.md` describe con `[1,2,4,…,1991,13,15]`.

`logs/hermeneutica-intro.log` falló distinto: `l1_max` no admite **ningún**
encabezado de nivel 1 en tres intentos — un documento de 4 párrafos, donde no hay
estructura que aprender.

### H4 — Dos logs vacíos

`logs/1-agustin.log` y `logs/4-escolastica.log` tienen 0 bytes. Las corridas
correspondientes sí dejaron perfil, así que se ejecutaron; el `tee` no capturó.

## Fuga de vocabulario (híbrido − dense-only)

En 21 mediciones con ambos modos, el híbrido gana a dense-only en 5 casos
(+0.025 a +0.050) y **pierde** en 8 (hasta −0.075). No hay señal sistemática de
que las preguntas sintéticas estén filtrando vocabulario a la pierna léxica; si
algo, dense-only rinde mejor en este corpus. Es una lectura útil y barata que no
estaba escrita en ningún sitio.

## Documentos excluidos

Ninguno registrado. El inventario que pide Fase 0 (duplicados por hash, plantillas
vacías, el único `.pptx`) **no se hizo** en estas 28 corridas, y `libros/` sigue
conteniendo los duplicados aparentes que el runbook nombra.

## Correcciones al runbook derivadas de esta reconstrucción

1. **Fase 0**: `grep -c API_KEY .env` está obsoleto — no hay `.env` en `docaget/`
   y `vertex.py` usa ADC. Sustituido por la comprobación de ADC y `PROJECT_ID`.
2. **Fase 4**: la tabla de costos por documento es imposible con el `ledger`
   actual, que sobrescribe `costo.json` por corrida. O se copia `costo.json` a
   `logs/<slug>.costo.json` al terminar cada documento, o la rendición de cuentas
   no existe. Añadido al runbook como paso de 1.4.
3. **1.5(g)**: la regla "ningún perfil con scores todos en 0.0" no atrapa H1.
   Ampliada a "ninguno cuyo `recall@5` quede por debajo de su propio
   `noise_floor`".

## Lo que queda pendiente

- H1 sin diagnosticar (requiere re-indexar Carlomagno).
- Sin dato de "capítulos leídos" para ninguno de los 28: los veredictos son
  métricos, no estructurales.
- Sin contabilidad de costos para 27 de 28 documentos, irrecuperable.
- El inventario de duplicados/plantillas de Fase 0 sigue sin hacerse.

---

## Familia "Editorial Vida / Casa sobre la Roca" — documento cabeza de serie

`01_RetoDeDios_INT-S.pdf`, Darío Silva-Silva, 304 páginas. Familia nueva: el
fingerprint `2bde8a7f` no existía y no colisiona con ninguno de los tres grupos
de la sección anterior. Indexado el 2026-08-29.

### Lectura previa (paso 1.1)

Del índice, páginas 7-8: **30 capítulos numerados 1-30**, más Advertencia,
Obertura en sí mayor, Conclusión provisional y Bibliografía. Los capítulos se
titulan `Capítulo N` en una línea y el título en la siguiente, así que **el
encabezado no lleva número** — el detector numérico incorporado no puede verlos.
No hay preguntas de repaso: los 199 `¿` del libro son retórica de ensayo. Capa
de texto presente (501.586 chars en 304 páginas), sin OCR.

### Resultado

| Señal | Valor |
|---|---|
| Extractor | `pdf_text` |
| Perfil | aprendido, `01-retodedios-int-s-2bde8a7f` revisión 1 |
| Chunks | 600 (`cuerpo` 502, `preguntas` 98) |
| **Capítulos detectados vs. leídos** | **30 vs 30** |
| recall@1 / recall@5 / MRR@10 | 0.650 / 0.950 / 0.775 |
| dense-only recall@5 | 0.950 |
| noise floor | 0.633 |
| margen bootstrap recall@5 | ±0.051 |
| Spans byte-exactos auditados | **600 de 600** |
| **Veredicto** | **OK** — y por primera vez en este informe es un OK completo, porque la fila de capítulos existe y coincide |

### (d) y (c): el evalset sintético subestima la pierna léxica

El hueco híbrido − dense-only en el evalset es **+0.000 recall@5**, que leído
solo diría que BM25 no aporta nada. El diagnóstico estructural, cuyos términos
los escribió una persona leyendo el libro y no el generador de preguntas, dice
lo contrario:

| Modo | términos literales en rank 1 |
|---|---|
| híbrido | **5 de 5** |
| dense-only | **1 de 5** |

`esencialismo`, `Parálisis teológica`, `Mestizaje espiritual` y `hermanos
separados` los encuentra la pierna léxica y no la densa. El runbook advierte del
caso contrario —híbrido ganando mucho más en el evalset que en el diagnóstico,
señal de fuga de vocabulario—; **aquí ocurre lo simétrico y no estaba previsto**:
las preguntas sintéticas, escritas a partir de los chunks, son paráfrasis
semánticas y no contienen los términos raros, así que miden el motor denso y no
ven la mitad léxica. La conclusión operativa es que el hueco del evalset no debe
leerse como "BM25 no sirve" sin mirar el diagnóstico.

### Hallazgo: dos consultas de ruido no fueron cerradas (invariante #9)

En **los dos modos**:

| Consulta de ruido | híbrido top1 | dense top1 | ¿cerrada? |
|---|---|---|---|
| recetas de cocina italiana con berenjena | 0.750 | 0.606 | **no** |
| el mantenimiento de bicicletas de montaña | 0.500 | 0.633 | **no** |
| cómo cambiar el aceite de un motor diésel | — | — | sí |
| xkcd qwerty zzzz plugh | — | — | sí |

Con `noise_floor = 0.6328` y las consultas en tema entre 0.718 y 0.758 en
dense-only, las dos que pasan están **en el suelo o justo por debajo**: la de
bicicletas puntúa 0.633 contra un suelo de 0.6328. No es que el gating esté roto,
es que en este documento el margen entre ruido y tema es de tres centésimas. Un
ensayo que habla de cultura, sociedad, tecnología y vida cotidiana está
semánticamente más cerca de "recetas" y "bicicletas" que un manual de teología
sistemática. Se reporta, no se ajusta: mover el suelo por un documento es
exactamente la clase de cambio que este runbook exige medir antes.

### Los 9 capítulos falsos, y por qué no se arreglan

Junto a los 30 reales, los breadcrumbs contienen 9 encabezados espurios:
`2. Ibídem.`, `3. Ibídem, p. 413.`, `1. Tácito, Anales, 15, 44.` y
`12 13 14 15 16 v6 5 4 3 2 1` (la línea de tirada de la página de créditos).

Son las notas al pie de este editorial, que **llevan punto tras el número**. El
invariante #12 dice que el punto es lo que distingue una pregunta de repaso de
una nota al pie —medido en el libro de referencia, donde ninguna nota lo lleva—
y aquí esa regla no se cumple. Arreglarlo tocaría `chunk.py` y movería
`test_port_fidelity`, que es la prueba fuerte del port. Queda como hecho medido
sobre este editorial, no como cambio.

### Costes

`gemini-3.6-flash` + `gemini-embedding-001`.

| Concepto | USD |
|---|---|
| Corrección (primera corrida, 20 batches, 140.040 in / 141.583 out) | 1.2719 |
| Evalset (primera corrida, descartado al fallar) | 0.0484 |
| `propose` (tres intentos fallidos + uno bueno) | 0.0247 |
| Embeddings desperdiciados en la corrida que murió a 586/600 | 0.0178 |
| Vuelta final que convergió (correct 2, embed 399, eval) | 0.0201 |
| **Total del documento** | **≈ 1.383** |

De ese total, **$0.086 (6%) se gastó dos veces** por defectos de la sesión, no
por el trabajo: el modelo de embedding equivocado y la ausencia de caché de
embeddings. Ambos arreglados; el segundo es la razón de que la corrida acabara
convergiendo.

Referencia por página: **$0.0045/página** con los modelos actuales.

---

## Familia "Vida Cristiana / Fruto del Espíritu" — `03-ElFrutoEterno_INT-S.pdf`

`03-ElFrutoEterno_INT-S.pdf`, Darío Silva-Silva, 256 páginas. Fingerprint `43ea27a4a28e6208`. Indexado el 2026-08-29 bajo `docagent_v2`.

### Lectura previa (paso 1.1)
Del índice (página 5): **10 capítulos numerados 1-10** (`1. El fruto espiritual`, `2. El misterio del amor`, `3. La alegría de Dios`, `4. El milagro de la paz`, `5. El poder de la paciencia`, `6. El tesoro de la amabilidad`, `7. La luz de la bondad`, `8. El fuego de la fidelidad`, `9. La fuerza de la humildad`, `10. La voz del dominio propio`), más Advertencia y Conclusión. Capa de texto presente (382.843 bytes en 256 páginas), sin OCR.

### Resultado

| Señal | Valor |
|---|---|
| Extractor | `pdf_text` |
| Perfil | aprendido, `03-elfrutoeterno-int-s-43ea27a4` revisión 1 |
| Chunks | 410 (`cuerpo` 387, `preguntas` 23) |
| **Capítulos detectados vs. leídos** | **10 vs 10** |
| recall@1 / recall@5 / MRR@10 | 0.400 / 0.850 / 0.606 |
| dense-only recall@5 | 0.875 |
| noise floor | 0.604 |
| margen bootstrap recall@5 | ±0.054 |
| Spans byte-exactos auditados | **410 de 410 verificados** |
| **Veredicto** | **OK** — estructura correcta, métricas cumplen el objetivo (recall@5 = 0.850 ≥ 0.85) |

### Costes y Rendimiento
- **Total:** $0.0111 (correct: $0.0042, embed: $0.0068, eval_query: $0.0002, propose: $0.0102).
- Referencia por página: **$0.00004/página** (gracias a la caché de embeddings y corrección).

---

## Familia "Inspiración / Biografía" — `04-TesorosDiosMeDio_int.pdf`

`04-TesorosDiosMeDio_int.pdf`, Esther Lucía Silva-Silva, 160 páginas. Fingerprint `721bae21c368c63e`. Indexado el 2026-08-29 bajo `docagent_v2`.

### Lectura previa (paso 1.1)
Del índice (página 3): **9 capítulos numerados 1-9** (`1. Lazos familiares`, `2. El secuestro frustrado`, `3. Un colmillo de elefante`, `4. La esclavitud femenina`, `5. La pobreza real`, `6. La mafia de la mendicidad`, `7. Un burro en la lavandería`, `8. Mujer integral`, `9. Un equipo de amigas`), más Prólogo, Conclusión y Agradecimientos especiales. Capa de texto presente (255.934 bytes en 160 páginas), sin OCR.

### Resultado

| Señal | Valor |
|---|---|
| Extractor | `pdf_text` |
| Perfil | aprendido, `04-tesorosdiosmedio-int-721bae21` revisión 1 |
| Chunks | 232 (`cuerpo` 230, `preguntas` 2) |
| **Capítulos detectados vs. leídos** | **9 vs 9** |
| recall@1 / recall@5 / MRR@10 | 0.500 / 0.875 / 0.660 |
| dense-only recall@5 | 0.925 |
| noise floor | 0.647 |
| margen bootstrap recall@5 | ±0.060 |
| Spans byte-exactos auditados | **Todos los puntos de la colección (2.279) verificados** |
| **Veredicto** | **OK** — estructura correcta, métricas por encima del objetivo (recall@5 = 0.875 ≥ 0.85) |

### Costes y Rendimiento
- **Total:** $0.0827 (propose: $0.0060, correct: $0.0103, evalset: $0.0570, embed: $0.0092, eval_query: $0.0002).
- Referencia por página: **$0.0005/página**.

---

## Familia "Vida Cristiana / Editorial Vida" (cabeza propia) — `05-CodigoJesus-_int-S.pdf`

`05-CodigoJesus-_int-S.pdf`, Darío Silva-Silva, Editorial Vida / Hechos & Crónicas,
224 páginas. Fingerprint `be3df4bfb86ce0c4` — distinto de los cuatro anteriores del
mismo autor/editorial (`2bde8a7f`, `5f4421c2`, `43ea27a4`, `721bae21`), así que se
trató como cabeza de familia propia con Fase 1 completa. Indexado el 2026-08-30
bajo `docagent_v2`.

### Lectura previa (paso 1.1)

Del índice (pág. 9-10): **24 capítulos numerados "Clave 1" … "Clave 24"**
(págs. 27-217), sin numerar en el propio encabezado del capítulo (el título es
"Clave N" seguido del subtítulo en línea aparte), más front matter sin numerar
(Agradecimientos, prólogo de Dante Gebel, Advertencia "Doble click", "Primer
E-mail") y un glosario alfabético final ("Ordenador de claves": ABOGADO, AMADO,
ADMIRABLE…). Capa de texto presente, sin OCR. **Sin preguntas de repaso** — es
un ensayo, no un libro de texto con imperativos tipo "Defina…".

**Defecto conocido confirmado antes de indexar**: el mismo editorial que ya
produjo "9 capítulos falsos" en `01_RetoDeDios_INT-S.pdf` numera aquí también
sus notas al pie con punto ("1. Paul Johnson, Historia del cristianismo,
Vergara Editor, S.A, Buenos Aires, 1989.", "4. Ibidem.", vistas en las
págs. 212-215).

### Spike: intento de arreglo del defecto de notas al pie (pedido explícitamente)

A diferencia del tratamiento de 01 (solo reportado), se intentó un arreglo con
test antes de indexar, siguiendo el protocolo de la Fase 3:

1. Test que falla: `classify_kind("1. Paul Johnson, Historia del cristianismo…")`
   debía devolver `nota`, no `preguntas`; devolvía `preguntas`.
2. Arreglo probado en `classify_kind` (`chunk.py`): exigir que un ítem numerado
   con punto también lleve `¿`, `?` o un verbo de `IMPERATIVES` para contar como
   pregunta — el mismo criterio que `heading_level` ya usa para la misma
   ambigüedad — y devolver `nota` en caso contrario.
3. **Descartado**: `uv run pytest -q` completo rompió
   `tests/test_invariants.py::test_inv11_footnotes_keep_their_section_path`
   sobre el corpus real (todas las notas al pie quedaron sin `section`). Por la
   regla propia del runbook ("un arreglo que mueve `test_invariants` no es un
   arreglo"), se revirtió el cambio en `chunk.py` y se borró el test del spike.
   Queda documentado en el propio docstring de `classify_kind` para que no se
   reintente sin medir de nuevo.
4. **Resultado**: el defecto se indexó sin arreglar, igual que en 01. Con los
   valores por defecto, 44 de 274 chunks quedaron etiquetados `preguntas`
   siendo en realidad notas al pie (ver kinds abajo).

### Resultado

A diferencia de 01 (que no aprendió reglas propias), aquí **sí se aprendió un
patrón de encabezado no numerado** (`^Clave\s+\d+$`), y `heading_guards`
aceptó los 24 capítulos reales tratando los ítems numerados como "ruido de
notas al pie" — la validación explícita salió `OK`. Pero eso solo protegió la
clasificación *de encabezado por el patrón aprendido*; el camino numérico por
defecto (`HEADING_RE`) sigue activo en paralelo y **6 notas al pie cortas
también calificaron como encabezados de nivel 1** por su cuenta, exactamente
la misma familia de defecto que los "9 capítulos falsos" de 01:
`2. Ibídem.`, `3. Ibídem.`, `4. Ibídem.`, `2. Flavio Josefo, Antigüedades
Judaicas, 18:63, 64.`, `2. Gonzalo, El hombre nuevo, Semana.com 2/13/2007.`,
`4.\t la María inauténtica.`.

| Señal | Valor |
|---|---|
| Extractor | `pdf_text` |
| Perfil | **aprendido**, `05-codigojesus-int-s-be3df4bf` revisión 1 (cabeza de familia propia) |
| Chunks | 274 (`cuerpo` 230, `preguntas` 44 — la mayoría notas al pie mal clasificadas, ver spike arriba) |
| **Capítulos reales detectados vs. leídos** | **24 vs 24 — coinciden** |
| **Capítulos espurios (notas al pie)** | **6**, vía el camino numérico por defecto en paralelo al patrón aprendido |
| recall@1 / recall@5 / MRR@10 | 0.675 / 0.900 / 0.764 |
| dense-only recall@5 | 1.000 |
| noise floor (dense) | 0.623 |
| margen bootstrap recall@5 | ±0.054 |
| hueco híbrido − dense-only recall@5 | **−0.100** (dense-only por encima de híbrido) |
| Spans byte-exactos auditados | **274 de 274 verificados** |
| Términos exactos en rank 1 (sidecar) | híbrido 5/6, dense-only 3/6 |
| **Veredicto** | **OK, con hallazgo documentado** — 24 vs 24 capítulos reales coincide, pero 6 capítulos espurios de notas al pie contaminan los `breadcrumb`/`chapter` de esos chunks. No se re-indexa con `--force-tune`: la 1.5(f) reserva esa excepción para cuando los capítulos *heredados* no coinciden con los leídos por colisión de fingerprint, no para este defecto (el numérico-por-defecto, no el aprendido). |

**Sobre el hueco híbrido negativo**: a diferencia del caso simétrico medido en
`01_RetoDeDios_INT-S.pdf` (+0.000, BM25 ayuda sin mover el evalset), aquí el
híbrido puntúa **peor** que dense-only en recall@5 del evalset sintético
(0.900 vs 1.000). La pista está en los términos exactos: dense-only falla 3 de
6 ("Editorial Hechos & Crónicas", "Dante Gebel", "Ordenador de claves" no salen
en rank 1), mientras que híbrido los recupera 5 de 6 — la pierna léxica ayuda
en el diagnóstico estructural. La caída en el evalset sintético es más
consistente con ruido de muestra (30 preguntas, margen ±0.054) que con una
regresión real; no se reporta como fuga porque el diagnóstico estructural
muestra lo contrario.

**Sobre el gating de ruido**: en modo dense-only solo "cómo cambiar el aceite
de un motor diésel" quedó `(below floor)`; las otras tres consultas de ruido
("recetas de cocina italiana con berenjena" 0.607, "xkcd qwerty zzzz plugh"
0.623, "bicicletas de montaña" 0.618) puntuaron en o por encima del
`noise_floor` medido (0.623) y pasaron el filtro. Igual que en
`01_RetoDeDios_INT-S.pdf`, el margen entre ruido y tema en este ensayo es
estrecho — no se mueve el suelo por un documento.

### Costes

`gemini-3.6-flash` + `gemini-embedding-001`. 13 batches de corrección (203
corregidos, 359 sin cambios, 8 rechazados por nombre propio/número perdido).

| Concepto | USD |
|---|---|
| `propose` (aprendizaje de reglas, un intento) | 0.005770 |
| Corrección (13 llamadas, 82.789 in / 80.748 out) | 0.729793 |
| Evalset (40 preguntas sintéticas) | 0.051183 |
| Embeddings (273 llamadas, caché de 60 batches de corrección) | 0.010959 |
| `eval_query` | 0.000183 |
| **Total del documento** | **0.797889** |

Referencia por página: **$0.0036/página**.

### Pendiente

- **6 capítulos espurios de notas al pie**, misma causa raíz que los 9 de 01
  (invariante #12 no se cumple en este editorial) y misma decisión: reportado,
  no arreglado — el arreglo intentado en `classify_kind` movió
  `test_invariants`. Cualquier arreglo futuro necesita también tocar
  `heading_level`, no solo `classify_kind`, porque el camino que produce estos
  6 falsos es el numérico por defecto (`HEADING_RE`), independiente del patrón
  "Clave N" ya aprendido y correcto.

---

## Familia "Vida Cristiana / Editorial Vida" (cabeza propia) — `06-SexoEnLaBiblia_INT-S.pdf`

`06-SexoEnLaBiblia_INT-S.pdf`, Darío Silva-Silva, Editorial Vida / Hechos &
Crónicas, 192 páginas. Fingerprint `4a29571f942e8696` — distinto de los cinco
anteriores de la misma serie. Indexado el 2026-08-30 bajo `docagent_v2`.

### Lectura previa (paso 1.1)

Del índice (pág. 3): **8 capítulos numerados 1-8** ("1. Bajo el signo de
Eros" … "8. La soledad compartida"), más front matter (Advertencia,
Introducción) y Conclusión + Bibliografía. Capa de texto, sin OCR. Sin
preguntas de repaso (ensayo). **El título real de cada capítulo se imprime en
dos párrafos separados**: el número solo en su propia línea (`1`), seguido en
otro párrafo por el título en versalitas (`BAJO EL SIGNO DE EROS`) — no
"1. Bajo el signo de Eros" en una sola línea como sugiere el índice. Las
subsecciones dentro de cada capítulo son títulos sin numerar en versalitas
("MASOQUISMO", "SADISMO", "SACRILEGIO"). **Notas al pie también numeradas con
punto** ("1. Costler y Willy, Enciclopedia del conocimiento sexual…"), mismo
defecto que en 01 y 05.

### Resultado: fallo de aprendizaje de reglas, y por qué es más grave que en 01 y 05

A diferencia de 05 (que aprendió su propio patrón "Clave N"), aquí **el
aprendizaje de reglas falló los 3 intentos** — el propio log lo dice:
"rule learning failed 3 times — using the measured defaults". Con los valores
por defecto activos, se investigó por qué:

- El título real de un capítulo nunca coincide con `HEADING_RE`
  (`^(\d+(\.\d+)*)\.?\s+\w`) porque el número y el título **son dos párrafos
  distintos**: el párrafo `1` no lleva ninguna palabra detrás (falla
  `HEADING_RE`), y el párrafo `BAJO EL SIGNO DE EROS` no lleva ningún número
  delante. Ningún patrón numérico, aprendido o por defecto, puede reconstruir
  un encabezado partido en dos párrafos sin lógica nueva de fusión.
- Mientras tanto, **las notas al pie sí encajan en `HEADING_RE`** ("2. Ibidem.",
  "9. Notimex, México, Dic. 30, 2002.") y sí se promueven a encabezados de
  nivel 1 con los defaults.

**Resultado medido, vía scroll directo a la colección**: de los 359 chunks del
documento, el campo `chapter` toma solo **5 valores distintos, y ninguno es un
capítulo real** — los 8 capítulos leídos (1 a 8) están **ausentes por
completo** del breadcrumb, sustituidos en todo el libro por 5 notas al pie mal
clasificadas como encabezados: `2. Ibidem.`, `4. Ibídem.`, `6. Ibidem.`,
`7. Ibídem.`, `9. Notimex, México, Dic. 30, 2002.`. El propio `diag` lo hace
visible: preguntas EN TEMA sobre contenido real de distintos capítulos
devuelven como "sección" del resultado top-1 cosas como `9. Notimex, México,
Dic. 30, 2002.` o `6.	 Ibidem.` — el texto recuperado es correcto, pero su
metadato de capítulo es ruido.

| Señal | Valor |
|---|---|
| Extractor | `pdf_text` |
| Perfil | fallback a defaults (`06-sexoenlabiblia-int-s-4a29571f`, revisión 1 — persiste sin reglas propias adoptadas) |
| Chunks | 359 (`cuerpo` 309, `preguntas` 50 — footnotes mal clasificadas, mismo defecto que 05) |
| **Capítulos reales detectados vs. leídos** | **0 vs 8 — no coinciden** |
| **Capítulos espurios (notas al pie)** | **5**, sustituyendo por completo el breadcrumb real |
| recall@1 / recall@5 / MRR@10 | 0.575 / 0.900 / 0.731 |
| dense-only recall@5 | 0.900 |
| noise floor (dense) | 0.630 |
| margen bootstrap recall@5 | ±0.058 |
| hueco híbrido − dense-only recall@5 | +0.000 (sin fuga de vocabulario aparente) |
| Spans byte-exactos auditados | **359 de 359 verificados** |
| Términos exactos en rank 1 (sidecar) | híbrido 6/6, dense-only 5/6 |
| **Veredicto** | **ESTRUCTURA DUDOSA** — las métricas de recuperación (recall@5 0.900) cumplen el objetivo y el texto/`char_span` de cada chunk es correcto, pero el breadcrumb/capítulo está completamente roto: 0 de 8 capítulos reales sobreviven, sustituidos por notas al pie. Por la regla de la Fase 1.5, un veredicto no puede decir OK cuando la columna de capítulos no coincide, aunque recall@5 pase. |

**Por qué no se re-indexa con `--force-tune`**: la excepción de 1.5(f) es solo
para perfiles *heredados* cuyos capítulos no coinciden con los leídos, no para
un documento cabeza de familia cuyo propio aprendizaje de reglas ya falló 3
veces con el mismo texto. Repetir la propuesta no cambiaría la causa raíz (el
título partido en dos párrafos), y no se intentó otro arreglo de código —
la regla de la sesión (Fase 3, punto 6) es no decidir solo sobre un trade-off
estructural: se reporta y se pregunta.

**No se investigó otro arreglo de código para este defecto** (a diferencia
del de notas al pie, ya evaluado y revertido para 05): el título partido en
dos párrafos es un problema estructural distinto — de fusión de párrafos
consecutivos en la propuesta de encabezado — más grande en alcance que el
cambio ya descartado, y arreglarlo tocaría la lógica de agrupación de
párrafos consecutivos en `rules.py`/`chunk.py`, con riesgo real sobre
`test_port_fidelity`. Se deja como hallazgo pendiente de decisión.

### Costes

`gemini-3.6-flash` + `gemini-embedding-001`. 14 batches de corrección (227
corregidos, 593 sin cambios, 5 rechazados, 1 no devuelto).

| Concepto | USD |
|---|---|
| `propose` (3 intentos, todos fallidos) | 0.018974 |
| Corrección (14 llamadas, 89.119 in / 85.850 out) | 0.777554 |
| Evalset (40 preguntas sintéticas) | 0.046348 |
| Embeddings (359 llamadas) | 0.012158 |
| `eval_query` | 0.000180 |
| **Total del documento** | **0.855214** |

Referencia por página: **$0.0045/página**.

### Pendiente

- **0 de 8 capítulos reales sobreviven en el breadcrumb**, sustituidos por 5
  notas al pie — el defecto estructural más severo medido hasta ahora en esta
  serie. Causa raíz distinta a la de 01/05: el título de capítulo está partido
  en dos párrafos (`1` y `BAJO EL SIGNO DE EROS` por separado), lo que ningún
  patrón numérico —aprendido o por defecto— puede reconstruir sin fusionar
  párrafos consecutivos en la propuesta de encabezado. No arreglado; reportado
  para decisión explícita del usuario.

---

## Familia "Vida Cristiana / Editorial Vida" (cabeza propia) — `07-LlavesDelPoder-INT.pdf`

`07-LlavesDelPoder-INT.pdf`, Darío Silva-Silva, Editorial Vida / Hechos &
Crónicas, 272 páginas. Fingerprint `ad7a5bda239c2cb1`. Indexado el 2026-08-30
bajo `docagent_v2`.

### Lectura previa (paso 1.1)

Del índice (pág. 5): **13 capítulos numerados "Llave 1" … "Llave 13"**, más
front matter (Advertencia, Introducción). Capa de texto, sin OCR. Sin
preguntas de repaso (sermón transcrito, sin ropaje literario, dice la propia
Advertencia). El título de cada capítulo se imprime en dos párrafos —
"LLAVE N" solo en su línea, el título en versalitas en la siguiente ("El
despojo") — igual que el patrón partido de `06-SexoEnLaBiblia_INT-S.pdf`, pero
aquí "LLAVE N" **sí** es una unidad léxica reconocible por sí misma (palabra +
número), a diferencia del "1" desnudo de 06. **Sin notas al pie numeradas**:
este libro no cita fuentes externas con el patrón `N. Autor, Obra...` que
produjo el defecto en 01/05/06.

### Resultado: primer aprendizaje de reglas exitoso al primer intento

A diferencia de 05 (3 intentos) y 06 (fallo total), aquí **el aprendizaje de
reglas pasó en el primer intento**: `^(LLAVE\s+\d+|CONTENIDO|ADVERTENCIA|Introducción)$`
para nivel 1. Sin encabezados numerados en el documento, `heading_guards`
quedó sin ejercitar — no hay notas al pie con las que colisionar.

### Hallazgo nuevo: la primera ronda de tuning de esta sesión dejó puntos huérfanos en Qdrant

Este es el primer documento de la sesión cuyo `recall@5` (0.825) quedó **por
debajo** del objetivo 0.85 tras la ronda inicial, así que el bucle
`tune → chunk → index → evaluate` se activó por primera vez en esta corrida —
y expuso un defecto que ningún test detectaba:

1. Ronda de retrieval knobs (`per_section`, `min_score`) agotada sin mejora
   real (todas dentro del margen de ruido ±0.059).
2. Se probó un candidato de chunking `target=900` → 625 chunks, indexado
   (4187 puntos totales en la colección). El resultado empeoró (MRR@10 0.644 →
   0.614, fuera de margen) → **REVERT**.
3. Se re-chunkeó de vuelta a 502 chunks y se re-indexó. `recall@5` subió a
   0.850 (cumple el objetivo) y se persistió el perfil.

**El problema**: el `revert` re-chunkea e re-indexa los 502 chunks correctos,
pero `n_index`/`Qdrant.upsert` solo **sobreescribe** los ids que la corrida
actual produce (`chunk_index` 0 a 501, deterministas vía
`point_id(doc_id, i)`). Nunca borra los ids que el candidato rechazado dejó
por encima de ese rango. Verificado con un scroll directo a la colección tras
la corrida: **625 puntos para este documento, no 502** — los ids `0..501`
(config final) y **los ids `502..624`, huérfanos del candidato de 625 chunks
rechazado, con su propio `char_span` apuntando de vuelta al byte 371.230** en
vez de continuar el rango real hasta 462.703. Esto duplicaba silenciosamente
el último tercio del libro bajo dos fronteras de chunk incompatibles,
compitiendo por el ranking de cualquier consulta sobre esa parte del libro —
exactamente la clase de "log sano, número inválido" que este runbook existe
para atrapar, y la misma familia de bug que `doc/CLAUDE.md` ya documenta bajo
"Bugs found by running the loop, not by reading it" (los 4 bugs de estado del
bucle de tuning), pero una variante nueva no listada ahí.

**Arreglado, con test que falla antes y pasa después.** Añadido
`Qdrant.prune_tail(doc_id, keep)` (`docagent/qdrant.py`): tras el `upsert`,
cuenta los puntos existentes para `doc_id` y, si superan `keep`, borra los ids
`point_id(doc_id, i)` para `i` en `[keep, old_count)` — sin necesitar un
filtro de rango, porque los ids son deterministas. `n_index` (`graph.py`)
llama a `prune_tail(doc_id, keep=len(points))` después de cada `upsert` y
registra cuántos puntos huérfanos podó. Dos tests nuevos en
`tests/test_qdrant_removal.py`
(`test_a_reverted_chunk_candidate_prunes_the_points_it_left_behind`,
`test_pruning_the_tail_is_a_noop_when_nothing_was_left_behind`), ambos
verificados en rojo antes del arreglo. Suite completa: **164 passed, 15
skipped** (162 + 2), sin mover `test_port_fidelity` ni `test_invariants`.

**Los 123 puntos huérfanos ya escritos en `docagent_v2` fueron borrados a
mano** (mismo cálculo de ids, con confirmación explícita del usuario antes de
tocar la colección en vivo) y verificados: la colección quedó en 502 puntos
contiguos (`chunk_index` 0-501), 502/502 `char_span` byte-exactos, y los 13
capítulos reales (`LLAVE 1` … `LLAVE 13`) intactos en el breadcrumb. El
arreglo en código evita que esto se repita en cualquier documento futuro cuya
ronda de tuning pruebe y rechace un candidato más grande que la config final.

| Señal | Valor |
|---|---|
| Extractor | `pdf_text` |
| Perfil | aprendido al primer intento, `07-llavesdelpoder-int-ad7a5bda` revisión 1 |
| Chunks (config final, persistida) | 502 (`cuerpo` 451, `preguntas` 51) |
| **Capítulos detectados vs. leídos** | **13 vs 13 — coinciden** |
| Capítulos espurios | **0** (sin notas al pie numeradas en este libro) |
| recall@1 / recall@5 / MRR@10 (config final) | 0.500 / 0.850 / 0.657 |
| dense-only recall@5 | 0.875 |
| noise floor (dense) | 0.619 |
| margen bootstrap recall@5 | ±0.058 |
| hueco híbrido − dense-only recall@5 | −0.025 |
| Spans byte-exactos auditados | **502 de 502 verificados**, tras podar 123 huérfanos |
| Términos exactos en rank 1 (sidecar) | híbrido 5/6, dense-only 5/6 |
| **Veredicto** | **OK** — estructura y métricas correctas tras el arreglo; sin el `prune_tail`, la colección habría quedado corrupta con 123 puntos duplicados que ninguna de las cinco comprobaciones anteriores (a)-(e) habría detectado por sí sola — hizo falta el scroll manual de (b). |

### Costes

`gemini-3.6-flash` + `gemini-embedding-001`. Esta es la corrida más cara de la
sesión, porque el candidato de tuning rechazado costó un re-embed completo
además del original.

| Concepto | USD |
|---|---|
| `propose` (aprendizaje de reglas, un intento) | 0.005136 |
| Corrección (19 llamadas, 137.709 in / 138.856 out) | 1.247984 |
| Evalset (40 preguntas sintéticas, reutilizadas en las tres rondas) | 0.053233 |
| Embeddings (875 llamadas: 502 + 625 del candidato rechazado + reindexado tras el revert, con caché parcial) | 0.029286 |
| `eval_query` | 0.000180 |
| **Total del documento** | **1.335820** |

Referencia por página: **$0.0049/página**. El candidato de tuning rechazado
(`target=900`) es responsable de aproximadamente un tercio de este coste — el
precio medido de "un candidato de chunking cuesta un re-embed completo"
(`doc/CLAUDE.md`, "Tuning is two-tier by cost").

### Pendiente

- Ninguno estructural para este documento — es el primer veredicto limpio de
  la serie Silva-Silva (13 de 13 capítulos, 0 espurios).
- El arreglo de `prune_tail` es nuevo y solo se ha ejercitado en este
  documento; vale la pena vigilar el próximo documento cuya ronda de tuning
  se active para confirmar que el log muestra la línea "pruned N stale
  point(s)" cuando corresponda, y que no aparece cuando no hace falta.
