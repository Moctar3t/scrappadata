#!/bin/bash
# Round-two catalog chain: fix fakes → refine genres → publish → wait for GPU
# resplits → publish again. Serialized so nothing races the index file.
cd /Users/dime/Developer/scrappadata || exit 1

echo "[chain] $(date '+%H:%M') fix round (gate re-pick + requeue suspects)"
python3 -u audit_fakes.py --fix > fix_run.log 2>&1

echo "[chain] $(date '+%H:%M') genre refinement"
python3 -u refine_genres.py > genres_run.log 2>&1

echo "[chain] $(date '+%H:%M') export + publish (pass 1)"
python3 export_catalog.py >> genres_run.log 2>&1
bash /Users/dime/Developer/ios-zingy/scripts/upload-catalog.sh >> genres_run.log 2>&1

echo "[chain] $(date '+%H:%M') waiting for the GPU to drain the resplit queue"
for i in $(seq 1 360); do
  n=$(ssh -o BatchMode=yes -o ConnectTimeout=12 root@204.168.236.140 \
      "sudo -u postgres psql -d zingy -tA -c \"SELECT count(*) FROM tracks WHERE status IN ('queued','processing');\"" 2>/dev/null)
  echo "[chain] in-flight: ${n:-?} ($((i*30))s)"
  [ "$n" = "0" ] && break
  sleep 30
done

echo "[chain] $(date '+%H:%M') final export + publish (resplit stems)"
python3 export_catalog.py > final_export.log 2>&1
bash /Users/dime/Developer/ios-zingy/scripts/upload-catalog.sh >> final_export.log 2>&1
echo "ALL_DONE $(date '+%H:%M')" | tee -a final_export.log
