#!/usr/bin/env bash
# Read-only drive register audit in a one-off hardware API container.
# Stop the API first (./scripts/stop); restart after (./scripts/start -d -h).
# Extra arguments pass through, e.g. --all, --json, --motor x_axis.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
  development) config_name=config.development.json ;;
  production) config_name=config.json ;;
  *)
    echo "refusing: register audit supports only development or production mode" >&2
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

out_dir="$repo_root/openshelf_hardware/data/register-audit"
mkdir -p "$out_dir"
out_path="$out_dir/$(date -u +%Y%m%dT%H%M%SZ).txt"
printf 'output: %s\n' "$out_path"

quoted_args=""
for arg in "$@"; do
  printf -v quoted_arg '%q' "$arg"
  quoted_args+=" $quoted_arg"
done

docker compose \
  "${compose_files[@]}" \
  run --pull never --no-deps --rm --entrypoint sh \
  -v "$repo_root/scripts:/app/scripts:ro" \
  api -lc "cd /app/openshelf_hardware && uv run python /app/scripts/motor_register_audit.py --config $config_container_path${quoted_args}" \
  | tee "$out_path"
