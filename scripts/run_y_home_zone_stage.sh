#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
onsite_confirmation="ONSITE-RESTRAINT-AND-ESTOP-READY"

usage() {
  echo "usage: $0 inspect | <scan-zone|move-away-100> ONSITE-RESTRAINT-AND-ESTOP-READY" >&2
}

if [[ $# -eq 1 && "$1" == inspect ]]; then
  stage=inspect
  python_args=(inspect)
elif [[ $# -eq 2 && "$1" == scan-zone && "$2" == "$onsite_confirmation" ]]; then
  stage=scan-zone
  python_args=(scan-zone --confirm SUPERVISED-RESTRAINED-Y-HOME-ZONE-SCAN)
elif [[ $# -eq 2 && "$1" == move-away-100 && "$2" == "$onsite_confirmation" ]]; then
  stage=move-away-100
  python_args=(move-away-100 --confirm SUPERVISED-RESTRAINED-Y-HOME-ZONE-100MM-MOVE)
else
  usage
  exit 2
fi

if [[ ! -f "$repo_root/openshelf_hardware/data/.mode" ]]; then
  echo "refusing: missing operating-mode marker" >&2
  exit 1
fi

compose_files=(
  -f "$repo_root/docker-compose.yml"
  -f "$repo_root/docker-compose.dev.yml"
  -f "$repo_root/docker-compose.hardware.yml"
)

mode="$(tr -d '[:space:]' < "$repo_root/openshelf_hardware/data/.mode")"
case "$mode" in
  development)
    config_name=config.development.json
    ;;
  production)
    config_name=config.json
    ;;
  *)
    echo "refusing: home-zone diagnostic supports only development or production mode" >&2
    exit 1
    ;;
esac

config_host_path="$repo_root/openshelf_hardware/data/$config_name"
config_container_path="/app/openshelf_hardware/data/$config_name"
if [[ ! -f "$config_host_path" ]]; then
  echo "refusing: missing active configuration $config_host_path" >&2
  exit 1
fi
if curl -sS -o /dev/null --max-time 1 http://127.0.0.1:8000/api/health; then
  echo "refusing: API is listening; stop it with ./scripts/stop first" >&2
  exit 1
fi

api_image="$(
  docker compose "${compose_files[@]}" config --format json \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["services"]["api"]["image"])'
)"
if [[ -z "$api_image" ]] || ! docker image inspect "$api_image" >/dev/null 2>&1; then
  echo "refusing: required preloaded API image is unavailable: ${api_image:-unknown}" >&2
  exit 1
fi

trace_dir="$repo_root/openshelf_hardware/data/y-home-zone"
mkdir -p "$trace_dir"
trace_path="$trace_dir/$(date -u +%Y%m%dT%H%M%SZ)-${stage}.jsonl"
printf 'trace: %s\n' "$trace_path"

quoted_args=""
for arg in "${python_args[@]}"; do
  printf -v quoted_arg '%q' "$arg"
  quoted_args+=" $quoted_arg"
done

docker compose \
  "${compose_files[@]}" \
  run --pull never --no-deps --rm --entrypoint sh \
  -v "$repo_root/scripts:/app/scripts:ro" \
  api -lc "cd /app/openshelf_hardware && uv run python /app/scripts/y_home_zone_diagnostic.py${quoted_args} --register-map cl2b-rs507-pr --config $config_container_path" \
  | tee "$trace_path"
