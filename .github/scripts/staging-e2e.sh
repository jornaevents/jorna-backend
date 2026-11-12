#!/usr/bin/env bash
# Run jorna-website's staging checks against the backend that was just
# deployed to staging, and fail if they fail. Run by ci.yml's
# `staging-checks` job; `deploy-production` waits for it.
#
# The checks (jorna-website .github/workflows/staging-e2e.yml) send, negotiate
# and sign a real contract on the staging vendor site against the staging
# backend. Before this, only a website merge ran them, so a backend change
# that broke the website (contracts-in-chat, #176/#178) reached production
# unchecked.
#
# Needs GH_TOKEN: WEBSITE_ACTIONS_TOKEN, a fine-grained token on
# jornaevents/jorna-website with Actions: read and write, kept on this repo's
# `staging` environment. When it expires, this step fails with HTTP 401/403
# and production deploys stop: make a new one and replace the secret
# (pbpaste | gh secret set WEBSITE_ACTIONS_TOKEN -R jornaevents/jorna-backend --env staging).
#
# If the checks fail because this change is meant to change the website's
# behaviour: merge the website's test update (its own staging run uses this
# backend), then re-run this job.
set -euo pipefail

: "${GH_TOKEN:?WEBSITE_ACTIONS_TOKEN secret is not set — see .github/scripts/staging-e2e.sh}"
: "${TAG:?TAG is required}"

REPO=jornaevents/jorna-website
WORKFLOW=staging-e2e.yml

gh workflow run "$WORKFLOW" -R "$REPO" --ref main -f reason="$TAG"

# The dispatch doesn't return the run, so find it by the name it was given.
run_id=""
for _ in $(seq 1 30); do
  run_id=$(gh run list -R "$REPO" -w "$WORKFLOW" -e workflow_dispatch -L 20 \
    --json databaseId,displayTitle \
    --jq "map(select(.displayTitle | endswith(\"$TAG\")))[0].databaseId // empty")
  [ -n "$run_id" ] && break
  sleep 5
done
if [ -z "$run_id" ]; then
  echo "::error::Started $WORKFLOW in $REPO but couldn't find its run"
  exit 1
fi
url="https://github.com/$REPO/actions/runs/$run_id"
echo "Staging checks: $url"

# Exits non-zero unless the run succeeds (failed or cancelled both fail here).
if ! gh run watch "$run_id" -R "$REPO" --exit-status --interval 20 >/dev/null; then
  echo "::error::The website's staging checks didn't pass against this backend: $url"
  exit 1
fi
echo "Staging checks passed: $url"
