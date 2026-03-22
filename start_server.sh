#!/bin/bash

PORT=${GRPC_PORT:-50061}

# Kill any process running on the port
PID=$(lsof -ti :$PORT)
if [ -n "$PID" ]; then
    echo "Killing existing process on port $PORT (PID: $PID)"
    kill -9 $PID
    sleep 1
fi

# Start the server
echo "Starting sage-billing-engine on port $PORT..."
poetry run python -m app.main