#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

mkdir -p "$TEST_DIR/scripts" "$TEST_DIR/openshelf_hardware/data" "$TEST_DIR/bin"
cp "$REPO_ROOT/scripts/start" "$TEST_DIR/scripts/start"
chmod +x "$TEST_DIR/scripts/start"

cat > "$TEST_DIR/bin/docker" <<'SH'
#!/bin/sh
set -eu

case "$*" in
	*"config --images")
		printf '%s\n' registry.example/api:latest registry.example/ui:latest
		;;
	pull\ *)
		printf '%s\n' "$*" >> "$MOCK_DOCKER_LOG"
		;;
	*" up "*)
		printf '%s\n' "$*" >> "$MOCK_DOCKER_LOG"
		;;
	*)
		echo "unexpected docker command: $*" >&2
		exit 1
		;;
esac
SH
chmod +x "$TEST_DIR/bin/docker"

printf '%s\n' '{"robot":{"enabled":false}}' > "$TEST_DIR/openshelf_hardware/data/config.json"

(
	cd "$TEST_DIR"
	PATH="$TEST_DIR/bin:$PATH" MOCK_DOCKER_LOG="$TEST_DIR/docker.log" \
		"$TEST_DIR/scripts/start" --mode development --pull --detached
)

grep -Fx 'pull registry.example/api:latest' "$TEST_DIR/docker.log" >/dev/null
grep -Fx 'pull registry.example/ui:latest' "$TEST_DIR/docker.log" >/dev/null
grep -F 'compose -f docker-compose.yml -f docker-compose.dev.yml up --no-build --remove-orphans -d' \
	"$TEST_DIR/docker.log" >/dev/null
