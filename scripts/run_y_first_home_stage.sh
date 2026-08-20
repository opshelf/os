#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
onsite_confirmation="ONSITE-RESTRAINT-AND-ESTOP-READY"
cabinet_bypass_confirmation="CABINET-DOORS-NOT-INSTALLED-AUTHORIZE-BYPASS"

usage() {
  echo "usage: $0 check | <home-immediate|home-after-5s> <position-trace> ONSITE-RESTRAINT-AND-ESTOP-READY [CABINET-DOORS-NOT-INSTALLED-AUTHORIZE-BYPASS]" >&2
}

if [[ $# -eq 1 && "$1" == check ]]; then
  python_args=(check)
  stage=check
elif [[ ( $# -eq 3 || $# -eq 4 ) && ( "$1" == home-immediate || "$1" == home-after-5s ) ]]; then
  stage="$1"
  position_trace="$2"
  if [[ "$3" != "$onsite_confirmation" ]]; then
    echo "refusing Y home: onsite confirmation mismatch" >&2
    exit 2
  fi
  python_args=(
    "$stage"
    --position-trace "$position_trace"
    --confirm SUPERVISED-Y-HOME-RECENTLY-VERIFIED-UNDER-50MM
  )
  if [[ $# -eq 4 ]]; then
    if [[ "$4" != "$cabinet_bypass_confirmation" ]]; then
      echo "refusing Y home: cabinet-door bypass confirmation mismatch" >&2
      exit 2
    fi
    python_args+=(--enable-absent-cabinet-doors-bypass)
  fi
else
  usage
  exit 2
fi

if [[ ! -f "$repo_root/openshelf_hardware/data/.mode" ]] || [[ "$(tr -d '[:space:]' < "$repo_root/openshelf_hardware/data/.mode")" != development ]]; then
  echo "refusing: device must already be in development mode" >&2
  exit 1
fi
if [[ -z "${OPENSHELF_DIAGNOSTIC_PIN:-}" && ! -t 0 ]]; then
  echo "refusing: set OPENSHELF_DIAGNOSTIC_PIN for non-interactive authentication" >&2
  exit 1
fi

trace_dir="$repo_root/openshelf_hardware/data/y-brake-timing"
mkdir -p "$trace_dir"
trace_path="$trace_dir/$(date -u +%Y%m%dT%H%M%SZ)-${stage}.jsonl"
printf 'trace: %s\n' "$trace_path"

python3 "$repo_root/scripts/y_first_home_diagnostic.py" "${python_args[@]}" | tee "$trace_path"
