#!/bin/bash
cd /home/kheiron/yorch/docaget
export PROJECT_ID=yorch-platform-prod
for vuelta in $(seq 1 40); do
  antes=$(ls cache/embed 2>/dev/null | wc -l)
  echo "=== vuelta $vuelta · $antes vectores en caché · $(date +%H:%M:%S) ===" >> logs/puertas-supervisor.log
  uv run docagent index --collection docagent_v2 --workers 1 \
      --resume "02-PuertasEternas_INT:20260829T123147" \
      "libros/02-PuertasEternas_INT.pdf" >> logs/puertas.log 2>&1
  rc=$?
  despues=$(ls cache/embed 2>/dev/null | wc -l)
  echo "    rc=$rc · $antes -> $despues vectores" >> logs/puertas-supervisor.log
  if [ $rc -eq 0 ]; then
    echo "=== CONVERGIO en la vuelta $vuelta ===" >> logs/puertas-supervisor.log
    cp costo.json logs/02-PuertasEternas.costo.json
    exit 0
  fi
  if [ "$antes" = "$despues" ] && [ $vuelta -gt 2 ]; then
    echo "=== SIN AVANCE en la vuelta $vuelta; parando ===" >> logs/puertas-supervisor.log
    exit 1
  fi
  sleep 60
done
