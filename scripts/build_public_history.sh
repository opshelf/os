#!/usr/bin/env bash
# Build a scrubbed, main-only copy of this repository's history for publication in a
# NEW repository, then verify it. Nothing is pushed.
#
# Early history committed runtime databases (with audit rows holding login PINs), a
# settings file with the old dev JWT secret, a vite log with LAN and tailnet
# addresses, Sentry DSNs in source, node_modules, and firmware build output. They are
# all gone from the current tree but stay reachable from main, and GitHub keeps
# refs/pull/* on the existing repository even after a force push, so publication goes
# through a new repository instead of flipping this one public.
#
# usage: scripts/build_public_history.sh [source-url-or-path] [output-dir]
# requires: git-filter-repo, gitleaks, trufflehog
set -euo pipefail

SRC="${1:-git@github.com:opshelf/openshelf_os.git}"
OUT="${2:-/tmp/openshelf_os-public.git}"

REPLACEMENTS="$(mktemp)"
trap 'rm -f "$REPLACEMENTS"' EXIT
# Sentry DSNs hardcoded in early source files.
printf '%s\n' 'regex:https://[0-9a-f]{32}@o[0-9]+\.ingest\.us\.sentry\.io/[0-9]+==>SENTRY_DSN_REDACTED' > "$REPLACEMENTS"

rm -rf "$OUT"
git clone --bare --single-branch --branch main --no-tags "$SRC" "$OUT"
SOURCE_TREE="$(git -C "$OUT" rev-parse 'main^{tree}')"
cd "$OUT"

# Keep main only: stale branches, tags and refs/pull/* never reach the public repo.
git for-each-ref --format='%(refname)' | { grep -vx 'refs/heads/main' || true; } | xargs -n1 -I{} git update-ref -d {}

git filter-repo --force --invert-paths \
	--path-glob 'perpetual_hardware/*/data/*' \
	--path-glob 'perpetual_hardware/**/data/*' \
	--path-glob '*.db' \
	--path-glob '*.db-wal' \
	--path-glob '*.db-shm' \
	--path-glob '*.json.save' \
	--path-glob '*.json.bak' \
	--path-glob '*.save' \
	--path-glob '*nohup.out' \
	--path-glob '*/node_modules/*' \
	--path-glob 'node_modules/*' \
	--path-glob 'perpetual_fw/target/*' \
	--path-glob '*/.svelte-kit/*' \
	--replace-text "$REPLACEMENTS"

git reflog expire --expire=now --all
git gc --prune=now --aggressive --quiet

failed=0
check() {
	local label="$1"
	local count="$2"
	if [ "$count" = "0" ]; then
		echo "ok    $label"
	else
		echo "FAIL  $label ($count)"
		failed=1
	fi
}

check "no runtime databases, logs, backups, node_modules or build output" \
	"$(git rev-list --all --objects | grep -cE '\.db(-wal|-shm)?$|\.save$|\.json\.bak$|nohup\.out|node_modules/|perpetual_fw/target/' || true)"
check "no Sentry DSNs, literal jwt secrets or tailnet (CGNAT) addresses" \
	"$(git log --all -p --no-color | grep -E 'ingest\.[a-z.]*sentry\.io|"jwt_secret": *"[^"]+"|\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b' \
		| grep -cv 'test-private-value' || true)"
check "gitleaks" "$(gitleaks git --log-opts=--all --no-banner --report-format json --report-path /dev/stdout . 2>/dev/null | grep -c '"RuleID"' || true)"
check "trufflehog verified secrets" "$(trufflehog git "file://$OUT" --json --no-update --results=verified 2>/dev/null | wc -l | tr -d ' ')"
if [ "$(git rev-parse 'main^{tree}')" = "$SOURCE_TREE" ]; then
	echo "ok    main tree identical to the source"
else
	echo "FAIL  main tree differs from the source"
	failed=1
fi

echo "commits: $(git rev-list --count main), size: $(du -sh . | cut -f1)"
exit "$failed"
