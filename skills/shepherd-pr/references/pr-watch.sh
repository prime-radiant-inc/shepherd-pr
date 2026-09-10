#!/usr/bin/env bash
# One read-only observation. The adjacent stdlib helper owns JSON and state.
set -eu
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' 'PRWATCH error: python3 is required' >&2
  exit 1
fi
script_dir="$(cd -- "${BASH_SOURCE[0]%/*}" && pwd)"
exec python3 "$script_dir/pr_watch.py" "$@"
