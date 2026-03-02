#!/bin/bash
cd "$(dirname "$0")"
python3 -m uvicorn main:app --reload --host 0.0.0.0