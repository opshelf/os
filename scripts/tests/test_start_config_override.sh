#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TEST_DIR="$(mktemp -d)"
trap 'rm -rf "$TEST_DIR"' EXIT

mkdir -p "$TEST_DIR/scripts" "$TEST_DIR/openshelf_hardware/data" "$TEST_DIR/bin"
cp "$REPO_ROOT/scripts/start" "$TEST_DIR/scripts/start"
chmod +x "$TEST_DIR/scripts/start"

printf '%s\n' \
	'#!/bin/sh' \
	'if [ -n "${MOCK_DOCKER_CALLED:-}" ]; then : > "$MOCK_DOCKER_CALLED"; fi' \
	'if [ "${MOCK_DOCKER_MUST_NOT_RUN:-}" = true ]; then exit 99; fi' \
	'printf "%s\\n" "${OPENSHELF_CONFIG_PATH:-}" > "$MOCK_ENV_OUTPUT"' \
	'printf "%s\\n" "$*" > "$MOCK_ARGS_OUTPUT"' \
	> "$TEST_DIR/bin/docker"
chmod +x "$TEST_DIR/bin/docker"

run_start() {
	local override="$1"
	local config="$2"
	local custom_config="$3"
	local expected_path="$4"
	local expected_hardware="$5"
	local output_file="$TEST_DIR/docker-env"
	local args_output="$TEST_DIR/docker-args"
	local output=""

	printf '%s\n' "$config" > "$TEST_DIR/openshelf_hardware/data/config.json"
	printf '%s\n' "$custom_config" > "$TEST_DIR/openshelf_hardware/data/custom.json"
	rm -f "$TEST_DIR/openshelf_hardware/data/.mode" "$TEST_DIR/openshelf_hardware/data/.last_start_mode" "$TEST_DIR/docker-called"

	output="$(
		cd "$TEST_DIR"
		PATH="$TEST_DIR/bin:$PATH" MOCK_ENV_OUTPUT="$output_file" \
			MOCK_ARGS_OUTPUT="$args_output" MOCK_DOCKER_CALLED="$TEST_DIR/docker-called" \
			OPENSHELF_CONFIG_PATH="$override" \
			"$TEST_DIR/scripts/start" --mode production --detached
	)"

	if [[ "$expected_hardware" == true ]]; then
		[[ "$output" == *"hardware config detected"* ]]
	else
		[[ "$output" != *"hardware config detected"* ]]
	fi
	[[ "$(<"$output_file")" == "$expected_path" ]]
	[[ "$(<"$args_output")" == "compose -f docker-compose.yml"* ]]
	[[ "$(<"$args_output")" == *"up --remove-orphans -d" ]]
}

assert_rejected_override() {
	local override="$1"
	local output_file="$2"

	printf '%s\n' 'previous-mode' > "$TEST_DIR/openshelf_hardware/data/.mode"
	rm -f "$TEST_DIR/docker-called"

	if PATH="$TEST_DIR/bin:$PATH" MOCK_DOCKER_MUST_NOT_RUN=true \
		MOCK_DOCKER_CALLED="$TEST_DIR/docker-called" OPENSHELF_CONFIG_PATH="$override" \
		"$TEST_DIR/scripts/start" --mode production --detached >"$output_file" 2>&1; then
		echo "expected config override to fail: $override" >&2
		exit 1
	fi

	grep -F "OPENSHELF_CONFIG_PATH must be inside " "$output_file" >/dev/null
	grep -F "external host paths are not supported" "$output_file" >/dev/null
	[[ "$(<"$TEST_DIR/openshelf_hardware/data/.mode")" == "previous-mode" ]]
	[[ ! -e "$TEST_DIR/docker-called" ]]
}

HOST_CUSTOM="$TEST_DIR/openshelf_hardware/data/custom.json"
CONTAINER_CUSTOM="/app/openshelf_hardware/data/custom.json"

# A host data-dir override is authoritative and converted to its mounted path.
run_start "$HOST_CUSTOM" '{"robot":{"enabled":false}}' '{"robot":{"enabled":true}}' "$CONTAINER_CUSTOM" true

# An unreadable/invalid override falls back to the active config.
run_start "$HOST_CUSTOM" '{"robot":{"enabled":true}}' '{invalid json' "$CONTAINER_CUSTOM" true

# A missing in-data override remains mountable and falls back to the active config.
run_start "$TEST_DIR/openshelf_hardware/data/missing.json" '{"robot":{"enabled":true}}' '{"robot":{"enabled":false}}' \
	'/app/openshelf_hardware/data/missing.json' true

# A container-valid override is retained, while host detection reads the mounted peer.
run_start "$CONTAINER_CUSTOM" '{"robot":{"enabled":false}}' '{"robot":{"enabled":true}}' "$CONTAINER_CUSTOM" true

# A valid override explicitly disabling hardware takes precedence over the active config.
run_start "$HOST_CUSTOM" '{"robot":{"enabled":true}}' '{"robot":{"enabled":false}}' "$CONTAINER_CUSTOM" false

# An external host path cannot be safely mounted because runtime state lives
# beside its config, so reject it before Docker can see an unusable path.
EXTERNAL_CUSTOM="$TEST_DIR/custom.json"
printf '%s\n' '{"robot":{"enabled":true}}' > "$EXTERNAL_CUSTOM"
assert_rejected_override "$EXTERNAL_CUSTOM" "$TEST_DIR/external-output"

# A container-path traversal must receive the same canonical containment check.
assert_rejected_override '/app/openshelf_hardware/data/../custom.json' "$TEST_DIR/container-traversal-output"

# A container-path symlink that resolves outside the data directory is also rejected.
ln -s "$EXTERNAL_CUSTOM" "$TEST_DIR/openshelf_hardware/data/escaped.json"
assert_rejected_override '/app/openshelf_hardware/data/escaped.json' "$TEST_DIR/container-symlink-output"
