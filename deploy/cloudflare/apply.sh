#!/usr/bin/env bash
# The edge configuration the API's cache headers and the offline page depend
# on, applied through the Cloudflare API so it lives in git rather than in a
# dashboard nobody can diff.
#
#   CLOUDFLARE_API_TOKEN=... deploy/cloudflare/apply.sh            # apply
#   CLOUDFLARE_API_TOKEN=... deploy/cloudflare/apply.sh --dry-run  # show only
#
# Token scopes (Zone, pdxtrain.com only): Zone:Read, Zone Settings:Read,
# Cache Rules:Edit, Custom Pages:Edit.
#
# What it does, idempotently:
#   1. Cache rule over /api/v1/* - eligible for cache, respecting the origin's
#      own Cache-Control. Cloudflare will not cache extensionless paths on the
#      header alone; this rule is what makes services/api's s-maxage matter.
#      Merged by description into the zone's cache-settings ruleset, so any
#      other rule already there survives.
#   2. Custom 5xx error page -> https://pdxtrain.com/offline.html, so a dark
#      origin shows the product's own page. Plan-dependent: reported, never
#      fatal, if the plan does not offer custom pages.
#   3. Always Online: read and reported. It must stay OFF - a cached CLEAR
#      served during an outage is the one failure this product must never
#      have. Reported rather than changed because the token is read-only here.
set -euo pipefail

: "${CLOUDFLARE_API_TOKEN:?set CLOUDFLARE_API_TOKEN (never paste it into a chat)}"
ZONE_NAME="${ZONE_NAME:-pdxtrain.com}"
OFFLINE_URL="${OFFLINE_URL:-https://pdxtrain.com/offline.html}"
RULE_DESC="pdxtrain: cache API reads on origin TTL"
DRY_RUN=0; [[ "${1:-}" == "--dry-run" ]] && DRY_RUN=1
API="https://api.cloudflare.com/client/v4"

cf() { # method path [json-body]
  local m=$1 p=$2 body=${3:-}
  local args=(-sS -X "$m" "$API$p" -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN")
  [[ -n "$body" ]] && args+=(-H "Content-Type: application/json" --data "$body")
  curl "${args[@]}"
}
# Run a python snippet over the JSON response on stdin, parsed as `d`.
py_json() { python3 -c "import sys,json; d=json.load(sys.stdin); $1"; }
edge_headers() { # path [extra curl args...]
  local path=$1; shift
  printf "   %-20s " "$path"
  curl -s -D- -o /dev/null -A "Mozilla/5.0" "$@" "https://www.$ZONE_NAME$path" \
    | grep -iE "^(cf-cache-status|cache-control)" | tr -d '\r' | tr '\n' ' '
  echo
}

echo "== zone"
ZONE_ID=$(cf GET "/zones?name=$ZONE_NAME&status=active" | py_json 'r=d["result"]; print(r[0]["id"] if r else "")')
[[ -n "$ZONE_ID" ]] || { echo "zone $ZONE_NAME not visible to this token"; exit 1; }
PLAN=$(cf GET "/zones/$ZONE_ID" | py_json 'print(d["result"]["plan"]["name"])')
echo "   $ZONE_NAME  id=$ZONE_ID  plan=$PLAN"

echo "== 1. cache rule over /api/v1/*"
PHASE="/zones/$ZONE_ID/rulesets/phases/http_request_cache_settings/entrypoint"
EXISTING=$(cf GET "$PHASE" | py_json 'r=d.get("result") or {}; print(json.dumps(r.get("rules", [])))')
MERGED=$(RULE_DESC="$RULE_DESC" EXISTING="$EXISTING" python3 - <<'PY'
import json, os
rules = json.loads(os.environ["EXISTING"])
ours = {
  "description": os.environ["RULE_DESC"],
  # The event stream is live by definition and must never be cache-eligible.
  "expression": 'starts_with(http.request.uri.path, "/api/v1/") and not starts_with(http.request.uri.path, "/api/v1/events")',
  "action": "set_cache_settings",
  # Both TTLs follow the origin. Without browser_ttl here, the zone's
  # Browser Cache TTL (4h) overrides the origin's max-age=15 the moment a
  # path becomes cacheable - a phone would then hold a stale CLEAR for
  # hours, which is the one failure this product must never have.
  "action_parameters": {
    "cache": True,
    "edge_ttl": {"mode": "respect_origin"},
    "browser_ttl": {"mode": "respect_origin"},
  },
  "enabled": True,
}
# Keep every other rule exactly as it is; replace ours by description.
kept = [{k: r[k] for k in ("description","expression","action","action_parameters","enabled") if k in r}
        for r in rules if r.get("description") != ours["description"]]
print(json.dumps({"rules": kept + [ours]}))
PY
)
if [[ $DRY_RUN == 1 ]]; then
  echo "   would PUT $PHASE"
  echo "   $MERGED" | cut -c1-200
else
  cf PUT "$PHASE" "$MERGED" | py_json 'print("   applied" if d["success"] else "   FAILED: "+json.dumps(d["errors"]))'
fi

echo "== 2. custom 5xx page -> $OFFLINE_URL"
if [[ $DRY_RUN == 1 ]]; then
  echo "   would PUT /zones/$ZONE_ID/custom_pages/500_errors"
else
  cf PUT "/zones/$ZONE_ID/custom_pages/500_errors" "{\"url\":\"$OFFLINE_URL\",\"state\":\"customized\"}" \
    | py_json 'print("   applied" if d["success"] else "   not applied (plan-dependent): "+"; ".join(e.get("message","") for e in d["errors"]))'
fi

echo "== 3. Always Online (must be off)"
cf GET "/zones/$ZONE_ID/settings/always_online" | py_json 'v=d["result"]["value"]; print("   always_online =", v, "" if v=="off" else "  <-- TURN THIS OFF in the dashboard")'

echo "== verify (edge status, and the browser max-age the origin asked for)"
edge_headers /api/v1/status -m 20
edge_headers /api/v1/analytics -m 20
edge_headers /api/v1/events -m 4 -H "Accept: text/event-stream" 2>/dev/null
echo "   status must carry max-age=15 (not the zone's 14400); events must be DYNAMIC with no-cache"
