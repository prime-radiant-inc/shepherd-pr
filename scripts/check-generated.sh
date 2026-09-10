#!/usr/bin/env bash
# Run from a clean checkout: report ALL tracked/staged and untracked drift.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
bash scripts/everyharness.sh generate
status=$(git status --porcelain --untracked-files=all)
if [[ -n "$status" ]]; then
  printf 'Regeneration drift (including README and new files); regenerate, review and commit:\n%s\n' "$status" >&2
  git diff --stat
  exit 1
fi
printf 'Generated files and README match the clean checkout; no untracked drift.\n'
