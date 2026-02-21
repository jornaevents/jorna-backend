#!/bin/bash
cd "$(dirname "$0")"
python3 -m uvicorn main:app --reload