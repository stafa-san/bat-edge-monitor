1. Show it's live and collecting data:
bashcd ~/soundscape-monitor
docker compose exec db psql -U postgres -d soundscape -c "SELECT COUNT(*) FROM classifications;"
2. Show the latest classifications:
bashdocker compose exec db psql -U postgres -d soundscape -c "SELECT label, ROUND(score::numeric, 3) as score, ROUND(spl::numeric, 1) as spl_db, sync_time FROM classifications ORDER BY sync_time DESC LIMIT 20;"
3. Show unique sound classes detected:
bashdocker compose exec db psql -U postgres -d soundscape -c "SELECT label, COUNT(*) as detections, ROUND(AVG(score)::numeric, 3) as avg_score, ROUND(AVG(spl)::numeric, 1) as avg_spl FROM classifications GROUP BY label ORDER BY detections DESC;"
4. Show the pipeline is running in real-time:
bashdocker compose ps
5. Show the AudioMoth is detected:
basharecord -l
This demonstrates the full flow: AudioMoth capturing at 192kHz → AST model classifying 527 sound categories → SPL measurement → PostgreSQL storage — all running on the edge device in Docker containers.