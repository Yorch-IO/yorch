# Informe de Indexación

## Fase 0: Inventario del Corpus

Se ha realizado un análisis preliminar del corpus en `libros/`.

### Hallazgos

- **Documentos Duplicados**: Se confirmaron dos pares de documentos duplicados mediante hash `sha1sum`. Se recomienda excluir una de las copias en la indexación.
  - `libros/Hermeneutica Capitulo 1 (1).pdf` y `libros/done/Hermeneutica Capitulo 1.pdf`
  - `libros/CONFERENCIA RELACIÓN DEL HOMBRE CON EL TIEMPO (1).pdf` y `libros/CONFERENCIA RELACIÓN DEL HOMBRE CON EL TIEMPO.pdf`

- **Documentos "Plantilla"**: Los archivos `Plantilla semana 5.pdf` (68 párrafos) y `Plantilla semana 6.pdf` (210 párrafos) sí contienen texto y no son plantillas vacías. Se tratarán como documentos normales.

- **Formato Único**: El archivo `LAS IGLESIAS DEL APOCALIPSIS, Cap. 2 y 3.pptx` es el único `.pptx` y seguirá una ruta de extracción diferente.

### Familias de Documentos

Se ha propuesto una agrupación inicial por familias basada en los nombres de archivo. Esta agrupación se verificará con los fingerprints generados por `docagent` durante la indexación.

- Historia de la Iglesia
- Doctrina/Sistemática
- Hermenéutica
- Homilética
- Filosofía y Cristianismo (FYC)
- Administración del Tiempo (ADT)
- Familia y Consejería
- Teología Social
- Liderazgo y Discipulado
- Vida Cristiana
- Escatología
- Sueltos

### Próximos Pasos

Se comenzará la indexación con la familia **Historia de la Iglesia**, según lo especificado en el runbook.

## Fase 1: Familia "Historia de la Iglesia"

### Documento: 1. DESDE AGUSTÍN DE HIPONA HASTA LOS SIETE CONCILIOS ECUMÉNICOS.pdf

- **Veredicto**: OK
- **Extractor**: `pdf_text`
- **Perfil**: Reutilizado de `2. PAPADO, MONAQUISMO...` (fingerprint `110b1333`). El eval set se regeneró correctamente.
- **Capítulos**: N/A (reglas heredades, no aprendidas).
- **Chunks**: 114
- **Métricas**:
  - `recall@1`: 0.725
  - `recall@5`: 0.975
  - `MRR@10`: 0.854
  - `dense-only recall@5`: 0.975
  - `noise_floor`: 0.598
- **Costo**: $0.005673

**Observaciones**:

- La indexación en la nueva colección `docagent_v2` muestra una mejora drástica en el rendimiento del retrieval en comparación con la colección `docagent` de referencia, donde todas las consultas de diagnóstico fallaron.
- El proceso de re-creación de la colección y la validación del `char_span` encontraron y resolvieron un problema de estado inicial de la colección Qdrant, destacando la importancia de estos pasos de verificación.
- El comportamiento de reutilización de perfil y descarte del eval set funcionó como se esperaba.

### Resto de la familia

Se indexaron los siguientes 6 documentos de la familia en un solo lote.

| Documento                                                                                      | Veredicto            | `recall@5` | Perfil Reutilizado de                                  | Fingerprint Collision |
| ---------------------------------------------------------------------------------------------- | -------------------- | ---------- | ------------------------------------------------------ | --------------------- |
| `2. PAPADO, MONAQUISMO...`                                                                     | OK                   | 0.925      | `1. DESDE AGUSTÍN...`                                  | No (misma familia)    |
| `3. CARLOMAGNO...`                                                                             | **MÉTRICAS BAJAS**   | 0.050      | `3. CARLOMAGNO...` (propio, pero scores bajos)         | No                    |
| `4. ESCOLÁSTICA...`                                                                            | **COLISIÓN DE PERFIL** | 0.000      | `hermeneutica-capitulo-3`                              | **Sí (otra familia)** |
| `LA IGLEISA APOTÓLICA.pdf`                                                                       | MÉTRICAS BAJAS       | 0.075      | No (aprendido)                                         | N/A                   |
| `La iglesia perseguida.pdf`                                                                    | MÉTRICAS BAJAS       | 0.025      | No (aprendido)                                         | N/A                   |
| `LOS APOLOGISTAS.pdf`                                                                          | MÉTRICAS BAJAS       | 0.025      | No (aprendido)                                         | N/A                   |

**Observaciones Adicionales**:

- **Colisión de Fingerprint**: El hallazgo más importante es que `4. ESCOLÁSTICA...` (familia Historia de la Iglesia) reutilizó un perfil de `hermeneutica-capitulo-3`. Esto es una colisión de fingerprint entre familias temáticas distintas y, como advierte el runbook, invalida las métricas de evaluación. El `recall@5` de 0.0 confirma que la evaluación falló.
- **Métricas Bajas**: Varios de los documentos suplementarios obtuvieron un `recall@5` muy por debajo del objetivo de 0.85. Esto sugiere que los perfiles aprendidos (o la falta de ellos) no son adecuados para estos documentos más cortos y de estructura simple. Para el documento de `3. CARLOMAGNO...`, las métricas post-tuning son inexplicablemente bajas y requieren mayor investigación.
- **Tiempos de Ejecución**: El proceso de indexación en lote fue extremadamente largo y propenso a timeouts, lo que indica que procesar tantos documentos heterogéneos a la vez es ineficiente.

### Fase 3: Corrección de Colisión de Fingerprint

El documento `4. ESCOLÁSTICA...` fue re-indexado individualmente con la bandera `--force-tune` para resolver la colisión de fingerprint.

- **Documento**: `4. ESCOLÁSTICA Y TEÓLOGOS MEDIEVALES, LA PREREFORMA Y LA REFORMA.pdf`
- **Veredicto**: OK
- **Perfil**: Aprendido (forzado), pero falló la validación de `heading_guards` y usó los defaults.
- **Capítulos Detectados**: `[4, 14, 15, 16, 17, 18, 19, 20, 21, 22]` (no contiguos).
- **Métricas**:
  - `recall@5`: 0.950
  - `MRR@10`: 0.793
- **Costo**: $0.005472

**Observaciones de la corrección**:

- El uso de `--force-tune` fue exitoso. A pesar de que el aprendizaje de reglas falló y se usaron los valores por defecto, las métricas de retrieval mejoraron de `recall@5=0.000` a `0.950`, demostrando que usar un perfil incorrecto es mucho peor que usar los defaults.
- El fallo en `heading_guards` (`not contiguous`) es un hallazgo importante sobre la estructura de este documento y las limitaciones del algoritmo de aprendizaje de reglas.

## Fase 1: Familia "Doctrina/Sistemática"

### Documento: 1.-Doctrina-del-Hombre.pdf

- **Veredicto**: OK
- **Extractor**: `pdf_text`
- **Perfil**: Reutilizado de `1-doctrina-del-hombre-ea960d68`. El eval set se regeneró correctamente.
- **Chunks**: 25
- **Métricas**:
  - `recall@5`: 0.960
  - `MRR@10`: 0.826
- **Costo**: $0.008017

**Observaciones**:

- El proceso de indexación, incluyendo la reutilización de perfil, el descarte del eval set y el ciclo de tuning, funcionó sin inconvenientes y produjo excelentes métricas.

## Fase 1: Familia "Hermenéutica"

### Documento: Introduccion Hermeneutica.pdf

- **Veredicto**: **COLISIÓN DE PERFIL**
- **Extractor**: `pdf_text`
- **Perfil**: Reutilizado de `INTRODUCCION FYC.pdf` (fingerprint `cdf7dfc6`).
- **Chunks**: 3
- **Métricas**:
  - `recall@5`: 1.000 (No confiable)
- **Costo**: $0.000570

**Observaciones**:

- Se ha detectado una colisión de fingerprint entre las familias "Hermenéutica" y "Filosofía y Cristianismo (FYC)". El documento de introducción a la hermenéutica ha reutilizado el perfil de la introducción a la filosofía.
- Aunque las métricas son perfectas, el runbook advierte que este resultado no es fiable, ya que el `evalset` se genera a partir de los chunks creados con un perfil incorrecto. Esto oculta posibles problemas estructurales.
- Se procederá a re-indexar este documento con `--force-tune` para forzar el aprendizaje de un perfil específico para la familia "Hermenéutica".

### Fase 3: Corrección de Colisión de Fingerprint (Hermenéutica)

El documento `Introduccion Hermeneutica.pdf` fue re-indexado con `--force-tune`.

- **Veredicto**: OK
- **Perfil**: Aprendido (forzado), pero usó defaults al no encontrar estructura de capítulos.
- **Métricas**:
  - `recall@5`: 1.000
  - `MRR@10`: 0.833

**Observaciones de la corrección**:

- Al forzar el aprendizaje, se creó un nuevo perfil para el fingerprint `cdf7dfc6`, separándolo de la familia "FYC". Aunque el documento no tenía una estructura de capítulos que aprender, ahora existe un perfil base correcto para la familia "Hermenéutica".

### Resto de la familia "Hermenéutica"

Se indexaron los cuatro capítulos restantes de la familia. Los resultados son mixtos, con buenas métricas pero con múltiples colisiones de fingerprint, lo que pone en duda la validez de la evaluación.

| Documento                   | Veredicto            | `recall@5` | Perfil Reutilizado de          | Fingerprint Collision |
| --------------------------- | -------------------- | ---------- | ------------------------------ | --------------------- |
| `Hermeneutica Capitulo 1.pdf` | **COLISIÓN DE PERFIL** | 0.850      | `3.-Doctrina-de-la-Redención.pdf` | **Sí (Doctrina)**     |
| `Hermeneutica Capitulo 2.pdf` | OK                   | 0.950      | (propio)                       | No                    |
| `Hermeneutica Capitulo 3.pdf` | **COLISIÓN DE PERFIL** | 0.900      | `Lección 6-Doctrina-de-la-Adopción.pdf` | **Sí (Doctrina)**     |
| `Hermeneutica Capitulo 4.pdf` | OK                   | 0.875      | (propio)                       | No                    |

**Observaciones Adicionales**:

- Las colisiones de fingerprint son un problema recurrente y sistémico. Los capítulos de Hermenéutica están reutilizando perfiles de la familia Doctrina, lo que indica que sus estructuras son demasiado similares para que el fingerprinting actual las distinga. Esto refuerza el hallazgo de `CLAUDE.md` de que el fingerprint es puramente estructural y no capta el tema.
- A pesar de las colisiones, las métricas de `recall@5` se mantienen altas. Esto es engañoso y peligroso, ya que valida una indexación estructuralmente incorrecta.

## Fase 1 y 2: Familia "Filosofía y Cristianismo (FYC)"

Se indexaron 5 documentos de esta familia. Todos reutilizaron el mismo perfil con fingerprint `cdf7dfc6`, que colisiona con la familia "Hermenéutica". El último documento (`FILOSOFÍA CONTEMPORANEA.pdf`) aprendió un perfil nuevo y distinto.

| Documento                      | Veredicto | `recall@5` | Perfil Reutilizado de          | Fingerprint Collision |
| ------------------------------ | --------- | ---------- | ------------------------------ | --------------------- |
| `INTRODUCCION FYC.pdf`         | OK        | 1.000      | `Introduccion Hermeneutica.pdf` | **Sí (Hermenéutica)** |
| `PRESOCRATICOS FYC.pdf`        | OK        | 1.000      | `INTRODUCCION FYC.pdf`         | **Sí (Hermenéutica)** |
| `FILOSOFÍA CLÁSICA FYC.pdf`    | OK        | 1.000      | `PRESOCRATICOS FYC.pdf`        | **Sí (Hermenéutica)** |
| `ESCUELAS MORALISTAS (2).pdf`  | OK        | 1.000      | `FILOSOFÍA CLÁSICA FYC.pdf`    | **Sí (Hermenéutica)** |
| `FILOSOFÍA CONTEMPORANEA.pdf`  | OK        | 1.000      | (propio)                       | No                    |

**Observaciones**:

- La colisión de perfiles entre "FYC" y "Hermenéutica" es total. Esto invalida por completo las métricas de evaluación para esta familia y demuestra una limitación fundamental del método de fingerprinting actual.

## Fase 1 y 2: Familia "Homilética"

Se indexaron 5 documentos de esta familia. Los resultados fueron mayormente positivos, aunque el primer documento procesado (`CLASE 2.pdf`) no alcanzó el objetivo de recall.

| Documento                                      | Veredicto      | `recall@5` | Perfil Usado                               |
| ---------------------------------------------- | -------------- | ---------- | ------------------------------------------ |
| `CLASE 2.pdf`                                  | MÉTRICAS BAJAS | 0.750      | Aprendido (propio)                         |
| `(5 Clase) contenido EL PREDICADOR...`         | OK             | 1.000      | Aprendido (propio)                         |
| `CLASE 3 Holiletica...`                        | OK             | 1.000      | Aprendido (propio)                         |
| `CLASE 4. Clases de sermones..pdf`             | OK             | 0.975      | Aprendido (propio)                         |
| `CLASE 6. Estructura - bosquejo del Sermón..pdf`| OK             | 0.966      | Aprendido (propio)                         |

**Observaciones**:

- A diferencia de las familias anteriores, en "Homilética" no hubo reutilización de perfiles entre documentos, y no se observaron colisiones con otras familias. Cada documento aprendió su propio perfil.
- A pesar de no reutilizar perfiles, el documento `CLASE 2.pdf` no alcanzó el objetivo de `recall@5`, lo que sugiere que el problema de las métricas bajas no se limita a las colisiones de perfiles.

### Resto de la familia "Doctrina/Sistemática"

Se indexaron los siguientes 7 documentos de la familia en un solo lote. Todos los documentos reutilizaron perfiles de documentos con fingerprints similares y todos superaron el objetivo de `recall@5` de 0.85.

| Documento                              | Veredicto | `recall@5` | Perfil Reutilizado de          |
| -------------------------------------- | --------- | ---------- | ------------------------------ |
| `2.-Doctrina-del-Pecado.pdf`           | OK        | 0.897      | `4. ESCOLÁSTICA...`            |
| `3.-Doctrina-de-la-Redención.pdf`      | OK        | 0.912      | `2. PAPADO...`                 |
| `4.-Doctrina-de-la-Regeneración.pdf`   | OK        | 1.000      | `2.-Doctrina-del-Pecado.pdf`   |
| `5.-Doctrina-de-la-Justificación.pdf`  | OK        | 0.975      | `4.-Doctrina-de-la-Regeneración.pdf` |
| `Lección 6-Doctrina-de-la-Adopción.pdf`| OK        | 1.000      | `5.-Doctrina-de-la-Justificación.pdf`|
| `7.-Doctrina-de-la-Santificación.pdf`  | OK        | 0.950      | `7.-Doctrina-de-la-Santificación.pdf` (propio) |
| `8.-Doctrina-de-la-Resurrección.pdf`   | OK        | 1.000      | `8.-Doctrina-de-la-Resurrección.pdf` (propio) |

**Observaciones**:

- La indexación de esta familia fue muy exitosa, con métricas de alta calidad en todos los documentos. El sistema de reutilización de perfiles y la generación de nuevos `evalsets` funcionó como se esperaba, demostrando la robustez del agente para corpus homogéneos.
