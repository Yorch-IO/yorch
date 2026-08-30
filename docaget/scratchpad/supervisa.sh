#!/bin/bash
# Relanza la indexación hasta que converja. Es seguro y termina, y ninguna de las
# dos cosas es obvia:
#
#   - Seguro: `Vertex.embed` cachea cada vector en disco, así que una corrida que
#     muere en la pared de cuota conserva lo que pagó. Reanudar no vuelve a
#     gastar ni dinero ni cuota por lo ya hecho.
#   - Termina: cada vuelta pide estrictamente menos que la anterior, porque lo
#     que consiguió quedó cacheado. El progreso es monótono.
#
# La cuota `online_prediction_requests_per_base_model` se mide por minuto, de ahí
# la pausa entre vueltas: sin ella la vuelta siguiente empieza contra un cubo
# vacío y muere en el primer chunk nuevo.
cd /home/kheiron/yorch/docaget
export PROJECT_ID=yorch-platform-prod
for vuelta in $(seq 1 40); do
  antes=$(ls cache/embed 2>/dev/null | wc -l)
  echo "=== vuelta $vuelta · $antes vectores en caché · $(date +%H:%M:%S) ===" >> logs/01-supervisor.log
  uv run docagent index --collection docagent_v2 --workers 1 \
      --resume "01_RetoDeDios_INT-S:20260828T200828" \
      "libros/01_RetoDeDios_INT-S.pdf" >> logs/01-retodedios.log 2>&1
  rc=$?
  despues=$(ls cache/embed 2>/dev/null | wc -l)
  echo "    rc=$rc · $antes -> $despues vectores" >> logs/01-supervisor.log
  if [ $rc -eq 0 ]; then
    echo "=== CONVERGIO en la vuelta $vuelta ===" >> logs/01-supervisor.log
    cp costo.json logs/01-retodedios.costo.json
    exit 0
  fi
  # Sin avance en una vuelta entera significa que la pared no es de ritmo sino de
  # presupuesto: parar y decirlo, en vez de girar en vano.
  if [ "$antes" = "$despues" ] && [ $vuelta -gt 2 ]; then
    echo "=== SIN AVANCE en la vuelta $vuelta; parando ===" >> logs/01-supervisor.log
    exit 1
  fi
  sleep 90
done
echo "=== agotadas las 40 vueltas ===" >> logs/01-supervisor.log
exit 1
