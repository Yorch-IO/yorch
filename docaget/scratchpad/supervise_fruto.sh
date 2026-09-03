#!/bin/bash
cd /home/kheiron/yorch/docaget
export PROJECT_ID=yorch-platform-prod
for vuelta in $(seq 1 40); do
  antes=$(ls cache/embed 2>/dev/null | wc -l)
  echo "=== vuelta $vuelta · $antes vectores en caché · $(date +%H:%M:%S) ===" >> logs/03-supervisor.log
  uv run docagent index --collection docagent_v2 --workers 1 \
      --resume "03-ElFrutoEterno_INT-S:20260829T222711" \
      "libros/03-ElFrutoEterno_INT-S.pdf" >> logs/03-elfrutoeterno.log 2>&1
  rc=$?
  despues=$(ls cache/embed 2>/dev/null | wc -l)
  echo "    rc=$rc · $antes -> $despues vectores" >> logs/03-supervisor.log
  if [ $rc -eq 0 ]; then
    echo "=== CONVERGIO en la vuelta $vuelta ===" >> logs/03-supervisor.log
    cp costo.json logs/03-elfrutoeterno.costo.json
    exit 0
  fi
  if [ "$antes" = "$despues" ] && [ $vuelta -gt 2 ]; then
    echo "=== SIN AVANCE en la vuelta $vuelta; parando ===" >> logs/03-supervisor.log
    exit 1
  fi
  sleep 90
done
echo "=== agotadas las 40 vueltas ===" >> logs/03-supervisor.log
exit 1
