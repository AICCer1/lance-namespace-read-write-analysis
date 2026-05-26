#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   REPO_NAME=lance-namespace-read-write-analysis VISIBILITY=private ./scripts/create_github_repo_and_push.sh
#
# Requires one of:
#   - GITHUB_TOKEN
#   - GH_TOKEN

REPO_NAME="${REPO_NAME:-lance-namespace-read-write-analysis}"
VISIBILITY="${VISIBILITY:-private}"
OWNER="${OWNER:-AICCer1}"
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

if [[ -z "$TOKEN" ]]; then
  echo "Missing GITHUB_TOKEN / GH_TOKEN" >&2
  exit 1
fi

if [[ "$VISIBILITY" == "public" ]]; then
  PRIVATE_JSON=false
else
  PRIVATE_JSON=true
fi

payload=$(python3 - <<PY
import json
print(json.dumps({
  "name": "${REPO_NAME}",
  "private": ${PRIVATE_JSON},
  "auto_init": False,
  "description": "Analysis of Lance namespace read path and write_fragments flow"
}))
PY
)

curl -fsS \
  -X POST \
  -H "Authorization: token ${TOKEN}" \
  -H "Accept: application/vnd.github+json" \
  -H "User-Agent: openclaw" \
  https://api.github.com/user/repos \
  -d "$payload" >/tmp/github-create-repo-response.json

if ! git remote get-url origin >/dev/null 2>&1; then
  git remote add origin "git@github.com:${OWNER}/${REPO_NAME}.git"
else
  git remote set-url origin "git@github.com:${OWNER}/${REPO_NAME}.git"
fi

git push -u origin main

echo "Created and pushed: git@github.com:${OWNER}/${REPO_NAME}.git"
