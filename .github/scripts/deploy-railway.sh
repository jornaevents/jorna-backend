#!/usr/bin/env bash
# Deploy one exact commit of jorna-backend to one Railway environment and wait
# for the result. Run by ci.yml's `deploy-staging` and `deploy-production`
# jobs, in that order, once both CI jobs have passed on that commit.
#
# Why CI deploys instead of Railway's own GitHub trigger: that trigger's
# "wait for CI" watches the commit's check suites, and Railway's own suite
# ("railway-app") sits at `queued` forever on every commit. Whether a deploy
# ever went out became luck — three of the last four merges hung until
# someone ran `railway redeploy` by hand. Deploying by commit SHA from
# GitHub's source keeps RAILWAY_GIT_COMMIT_SHA set, which Sentry's release
# tag reads (app/config.py RELEASE).
#
# Needs RAILWAY_TOKEN: a Railway *project* token for superb-encouragement,
# scoped to the environment being deployed (Project Settings → Tokens). A
# project token only works for its own environment, so staging and production
# each keep theirs as a secret on the matching GitHub environment.
# RAILWAY_ENVIRONMENT_ID picks the environment; the service ID is the same in
# every environment of a project.
set -euo pipefail

: "${RAILWAY_TOKEN:?RAILWAY_TOKEN secret is not set — see .github/scripts/deploy-railway.sh}"
: "${COMMIT_SHA:?COMMIT_SHA is required}"
: "${RAILWAY_ENVIRONMENT_ID:?RAILWAY_ENVIRONMENT_ID is not set — see .github/scripts/deploy-railway.sh}"

API=https://backboard.railway.com/graphql/v2
SERVICE_ID=6bf00f93-ea7e-4d01-b111-c984abf6411e      # Desiconnect
ENVIRONMENT_ID=$RAILWAY_ENVIRONMENT_ID
TIMEOUT_SECONDS=${TIMEOUT_SECONDS:-1200}

gql() {
  curl -sS --fail-with-body "$API" \
    -H "Project-Access-Token: $RAILWAY_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$1"
}

deploy_body=$(jq -nc --arg s "$SERVICE_ID" --arg e "$ENVIRONMENT_ID" --arg c "$COMMIT_SHA" '{
  query: "mutation($s: String!, $e: String!, $c: String) { serviceInstanceDeployV2(serviceId: $s, environmentId: $e, commitSha: $c) }",
  variables: {s: $s, e: $e, c: $c}
}')
response=$(gql "$deploy_body")
deployment_id=$(jq -r '.data.serviceInstanceDeployV2 // empty' <<<"$response")
if [ -z "$deployment_id" ]; then
  echo "::error::Railway refused the deploy: $(jq -c '.errors // .' <<<"$response")"
  exit 1
fi
echo "Deploying ${COMMIT_SHA:0:7} as Railway deployment $deployment_id"

status_body() {
  jq -nc --arg id "$deployment_id" '{query: "query($id: String!) { deployment(id: $id) { status } }", variables: {id: $id}}'
}

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))
last=""
while :; do
  status=$(gql "$(status_body)" | jq -r '.data.deployment.status // "UNKNOWN"')
  if [ "$status" != "$last" ]; then echo "$(date -u +%H:%M:%S) $status"; last=$status; fi
  case "$status" in
    SUCCESS) exit 0 ;;
    FAILED|CRASHED|REMOVED|SKIPPED)
      echo "::error::Railway deployment $deployment_id ended $status — see its logs: railway logs --deployment $deployment_id"
      exit 1 ;;
  esac
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "::error::Railway deployment $deployment_id still $status after ${TIMEOUT_SECONDS}s"
    exit 1
  fi
  sleep 10
done
