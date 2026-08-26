#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config_path="/app/openshelf_hardware/data/config.development.json"
register_map="cl2b-rs507-pr"
onsite_confirmation="ONSITE-RESTRAINT-AND-ESTOP-READY"

usage() {
  echo "usage: $0 <stage> [ONSITE-RESTRAINT-AND-ESTOP-READY]" >&2
  echo "stages: inspect-baseline inspect-release-250 set-release-250 move-release-250 restore-release-100 inspect-engagement-250 set-engagement-250 move-engagement-250 restore-engagement-100 inspect-both-250 set-both-250 move-both-250 restore-both-release-100" >&2
}

if [[ $# -lt 1 || $# -gt 2 ]]; then
  usage
  exit 2
fi

stage="$1"
confirmation="${2:-}"
python_args=()
requires_onsite=false

case "$stage" in
  inspect-baseline)
    python_args=(inspect --expect baseline-100)
    ;;
  inspect-release-250)
    python_args=(inspect --expect release-250)
    ;;
  set-release-250|restore-release-100|set-engagement-250|restore-engagement-100|set-both-250|restore-both-release-100)
    python_args=("$stage" --confirm SUPERVISED-RESTRAINED-Y-BRAKE-DELAY-CHANGE)
    requires_onsite=true
    ;;
  inspect-engagement-250)
    python_args=(inspect --expect engagement-250)
    ;;
  inspect-both-250)
    python_args=(inspect --expect both-250)
    ;;
  move-release-250|move-engagement-250|move-both-250)
    python_args=("$stage" --confirm SUPERVISED-RESTRAINED-Y-TIMING-MOVE)
    requires_onsite=true
    ;;
  *)
    usage
    exit 2
    ;;
esac

if [[ "$requires_onsite" == true && "$confirmation" != "$onsite_confirmation" ]]; then
  echo "refusing active stage: pass $onsite_confirmation only with onsite restraint and E-stop supervision" >&2
  exit 2
fi
if [[ "$requires_onsite" == false && -n "$confirmation" ]]; then
  echo "read-only inspection does not accept an onsite confirmation argument" >&2
  exit 2
fi

if [[ ! -f "$repo_root/openshelf_hardware/data/.mode" ]] || [[ "$(tr -d '[:space:]' < "$repo_root/openshelf_hardware/data/.mode")" != development ]]; then
  echo "refusing: device must already be in development mode" >&2
  exit 1
fi
if [[ ! -f "$repo_root/openshelf_hardware/data/config.development.json" ]]; then
  echo "refusing: missing development configuration" >&2
  exit 1
fi
if curl -sS -o /dev/null --max-time 1 http://127.0.0.1:8000/api/health; then
  echo "refusing: API is listening; stop it with ./scripts/stop first" >&2
  exit 1
fi

trace_dir="$repo_root/openshelf_hardware/data/y-brake-timing"
mkdir -p "$trace_dir"
trace_path="$trace_dir/$(date -u +%Y%m%dT%H%M%SZ)-${stage}.jsonl"
printf 'trace: %s\n' "$trace_path"

quoted_args=""
for arg in "${python_args[@]}"; do
  printf -v quoted_arg '%q' "$arg"
  quoted_args+=" $quoted_arg"
done

docker compose \
  -f "$repo_root/docker-compose.yml" \
  -f "$repo_root/docker-compose.dev.yml" \
  -f "$repo_root/docker-compose.hardware.yml" \
  run --pull never --rm --entrypoint sh \
  -v "$repo_root/scripts:/app/scripts:ro" \
  api -lc "cd /app/openshelf_hardware && uv run python /app/scripts/y_brake_timing_diagnostic.py${quoted_args} --register-map $register_map --config $config_path" \
  | tee "$trace_path"
