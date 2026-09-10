#!/usr/bin/env bash
# Build only the pinned upstream source; never install from the plugin package.json.
set -euo pipefail
readonly pin=4f7c5e2112583b1a0d25d4d9413bd06f68f6f8b5
readonly upstream=https://github.com/prime-radiant-inc/everyharness.git
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
readonly tools="$root/.tools/everyharness"
fail() { printf 'everyharness wrapper: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'HELP'
Usage: scripts/everyharness.sh {generate|validate|bump|test} [arguments...]
Examples:
  scripts/everyharness.sh generate
  scripts/everyharness.sh validate
  scripts/everyharness.sh bump --check
  scripts/everyharness.sh test --image IMAGE@sha256:DIGEST

Requires Git, Node.js >=20, npm, and network access to GitHub/npm.
Clones pinned upstream into .tools/everyharness, checks its identity and clean
source, then runs npm ci and npm run build on every call. Generated plugin
package.json is NOT the tooling package. Do not run wrappers concurrently.
A dirty/wrong cached checkout is refused: preserve your changes, then move
.tools/everyharness aside and rerun to clone afresh. No bootstrap hooks run.
Use a subcommand's --help for upstream options (requires the tooling build).
HELP
}
case "${1:-}" in
  -h|--help) usage; exit 0 ;;
  generate|validate|bump|test) ;;
  *) usage >&2; exit 2 ;;
esac
for command in git node npm; do
  command -v "$command" >/dev/null || fail "Install $command and retry; see --help."
done
node -e 'process.exit(Number(process.versions.node.split(".")[0]) >= 20 ? 0 : 1)' ||
  fail 'Node.js >=20 is required; install a supported Node release and retry.'
if [[ ! -e "$tools" ]]; then
  mkdir -p "$root/.tools"
  git clone --no-checkout "$upstream" "$tools" || fail 'Clone failed; check GitHub connectivity and move any partial .tools/everyharness aside before retrying.'
  git -C "$tools" checkout --detach "$pin" || fail 'Cannot check out the pinned source; move .tools/everyharness aside and retry.'
fi
[[ -d "$tools/.git" ]] || fail 'Cache is not an upstream Git checkout; move .tools/everyharness aside and retry.'
[[ "$(git -C "$tools" remote get-url origin)" == "$upstream" ]] ||
  fail 'Cached origin differs from pinned upstream; move .tools/everyharness aside and retry.'
[[ "$(git -C "$tools" rev-parse HEAD)" == "$pin" ]] ||
  fail 'Cached HEAD differs from pinned commit; move .tools/everyharness aside and retry.'
# Only disposable upstream build/dependency outputs may be ignored in the cache.
status=$(git -C "$tools" status --porcelain --untracked-files=all --ignored)
while IFS= read -r line; do
  case "$line" in
    ''|'!! dist/'*|'!! node_modules/'*) ;;
    *) fail "Cached source is dirty ($line); preserve changes and move .tools/everyharness aside before retrying." ;;
  esac
done <<< "$status"
(
  cd "$tools"
  npm ci || fail 'npm ci failed; check the npm error/network access and retry (do not alter the pinned lockfile).'
  # tsc does not prune stale files: discard cached executable output first.
  rm -rf dist
  npm run build || fail 'Upstream build failed; inspect the compiler output above.'
)
cd "$root"
exec node "$tools/dist/cli.js" "$@"
