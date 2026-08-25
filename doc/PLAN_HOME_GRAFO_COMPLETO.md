# Plan: Inicio y grafo completo de la biblioteca

## Resumen

- Añadir una pestaña **Inicio** como pantalla predeterminada y conservar
  **Servicios** como pantalla independiente para configuración y diagnóstico.
- Mostrar en Inicio estadísticas de todo el proyecto: libros indexados,
  páginas, fragmentos, conceptos extraídos, afirmaciones y relaciones
  semánticas, junto con actividad reciente de importación e indexación.
- Convertir el grafo actual, centrado en un documento, en una vista interactiva
  de la biblioteca seleccionada que haga descubribles todos sus libros y
  conceptos extraídos.

## Cambios principales

- Crear una API de solo lectura para el resumen del proyecto, combinando el
  catálogo y la proyección del grafo. Debe devolver totales actuales y sin
  duplicados, ejecuciones recientes y un estado explícito de no disponible para
  las métricas derivadas del grafo; nunca sustituir datos no disponibles por
  cero.
- Crear una API para el grafo de una biblioteca: todos los documentos/versiones
  indexados, los nodos de concepto canónico y las aristas `MENTIONS` que superen
  el umbral de confianza. Las menciones repetidas entre un libro y un concepto
  se agregarán como una única arista ponderada.
- Añadir `HomeScreen`, la pestaña `home`, las cadenas de traducción y sus
  estados de carga, vacío, error y grafo no disponible. Inicio no necesita el
  selector de biblioteca porque sus estadísticas son globales al proyecto.
- Hacer que Inicio sea la pestaña inicial sin modificar las pantallas existentes
  ni eliminar Servicios.
- Reestructurar `GraphScreen` como una vista de conjunto interactiva, sin añadir
  una dependencia de renderizado de grafos: zoom, desplazamiento, ajustar a la
  vista, restablecer, búsqueda, filtros de libros/conceptos, umbral de confianza
  y panel de detalle para el nodo seleccionado.
- Cargar todo el conjunto de datos de la biblioteca seleccionada, aplicando
  nivel de detalle visual: mostrar etiquetas de nodos enfocados, seleccionados o
  hallados por búsqueda y ocultar etiquetas no seleccionadas al alejarse. Todos
  los libros y conceptos siguen siendo accesibles sin convertir el lienzo en
  texto solapado.
- Diferenciar visualmente libros y conceptos. Usar grosor/opacidad de las
  aristas para indicar fuerza de mención. Al seleccionar un libro, resaltar sus
  conceptos; al seleccionar un concepto, resaltar todos los libros que lo
  mencionan. Mantener visible el aviso de que las relaciones son propuestas por
  el modelo y su umbral de confianza.

## Interfaces públicas

- `GET /project-summary`: totales del proyecto, ejecuciones recientes y estado
  de disponibilidad por métrica.
- `GET /libraries/{library_id}/graph`: nodos y menciones ponderadas de todos los
  contenidos indexados de una biblioteca, filtrados por umbral de confianza.
- Añadir los métodos tipados equivalentes en el cliente Tauri y sus tipos
  TypeScript. Conservar los errores estructurados actuales para identificadores
  inválidos y para un grafo inaccesible.

## Pruebas

- Verificar en backend que los totales excluyen contenido inactivo o sin
  indexar, no duplican versiones compartidas y expresan correctamente una
  métrica de grafo no disponible.
- Verificar que el endpoint de grafo aísla la biblioteca activa, agrega
  menciones repetidas, filtra por confianza, ordena de forma estable y responde
  correctamente ante una biblioteca vacía.
- Verificar en UI que Inicio es la pestaña inicial, Servicios sigue disponible,
  las estadísticas manejan carga/error/vacío y las traducciones existen en
  español e inglés.
- Verificar que todos los nodos recibidos se representan, que búsqueda, filtros,
  zoom y restablecimiento funcionan y que la selección resalta los vecinos
  correctos con acceso por teclado incluso en grafos densos.

## Decisiones asumidas

- «Palabras» significa los **conceptos canónicos extraídos** existentes, no cada
  token literal de los textos fuente.
- Inicio muestra datos de todo el proyecto; el grafo completo se limita a la
  biblioteca seleccionada.
- «Libros» significa documentos indexados/versiones activas; para las métricas
  derivadas de contenido, una versión compartida se cuenta una sola vez.
