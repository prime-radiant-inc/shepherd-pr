#!/usr/bin/env bash
# One read-only stale-pull-request observation. The adjacent helper owns JSON.
set -eu
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'STALEPR error: python3 is required' >&2
  exit 1
fi
script_dir="$(cd -- "${BASH_SOURCE[0]%/*}" && pwd)"
exec python3 "$script_dir/stale_prs.py" "$@"
