#!/bin/sh
cd "$(dirname "$0")"

PORT="${PORT:-8000}"

echo "Starting OutboundAI on port ${PORT}..."
echo "LiveKit: ${LIVEKIT_URL:-not set}"
echo "Supabase: ${SUPABASE_URL:-not set}"
echo "Webhook: ${CALL_COMPLETED_WEBHOOK_URL:-not set}"

# Start API first — Azure needs this running for health checks
uvicorn server:app --host 0.0.0.0 --port "${PORT}" &
UVICORN_PID=$!

sleep 5

# Start agent worker after API is up (avoids memory spike at boot)
python agent.py start &
AGENT_PID=$!

echo "API pid=${UVICORN_PID}  Agent pid=${AGENT_PID}"

# Keep container alive while API is running
wait $UVICORN_PID
