#!/bin/bash
cd "$(dirname "$0")"

# Activate virtual environment if it exists
if [ -f "venv/bin/activate" ]; then
  source venv/bin/activate
fi

python3 -m uvicorn main:app --reload --host 0.0.0.0