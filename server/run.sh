#!/bin/bash
cd "$(dirname "$0")"

# Activate virtual environment if it exists
if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
fi

# Load environment variables from server/.env if present
if [ -f ".env" ]; then
  export $(grep -v '^#' .env | xargs)
fi

python -m uvicorn main:app --reload --host 0.0.0.0