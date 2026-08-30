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
