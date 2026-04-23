#!/bin/sh
set -e

# On Railway, set FIREBASE_CREDENTIALS_JSON to the raw JSON content of your
# firebase_credentials.json file. This writes it to disk before startup.
if [ -n "$FIREBASE_CREDENTIALS_JSON" ]; then
    printf '%s' "$FIREBASE_CREDENTIALS_JSON" > /app/firebase_credentials.json
    export FIREBASE_CREDENTIALS_PATH=/app/firebase_credentials.json
fi

# On Railway, set GOOGLE_CLIENT_SECRET_JSON to the raw JSON content of your
# client_secret.json file downloaded from Google Cloud Console.
if [ -n "$GOOGLE_CLIENT_SECRET_JSON" ]; then
    printf '%s' "$GOOGLE_CLIENT_SECRET_JSON" > /app/client_secret.json
    export GOOGLE_CLIENT_SECRET_PATH=/app/client_secret.json
fi

echo "Running Alembic migrations..."
alembic upgrade head

exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
