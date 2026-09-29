#!/usr/bin/env bash
# Runs the backend test suite (or the tests named in "$@") from anywhere in
# the repo. It exists for the PR review agent: its tool allowlist matches a
# command by prefix, so it can be allowed this script but not an arbitrary
# `cd server && …` chain.
set -euo pipefail

cd "$(dirname "$0")/../../server"
exec python -m pytest -q "$@"
