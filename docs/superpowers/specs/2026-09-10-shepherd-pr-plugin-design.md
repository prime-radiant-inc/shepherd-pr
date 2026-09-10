# Shepherd PR plugin design

Date: 2026-09-10
Status: Scope approved; written design awaiting review.

## Purpose and publication

Publish the local shepherd-pr workflow as a portable coding-agent plugin at
https://github.com/prime-radiant-inc/shepherd-pr. The repository will be public,
MIT licensed, and copyrighted to Prime Radiant, Inc. Develop in
`~/git/shepherd-pr` on a WIP branch. Preserve the original local skill and the
Evener checkout, including its untracked files.

The plugin helps an agent bring a GitHub pull request through CI and review
to an authorized merge, or report an evidenced blocker. It contains
instructions and a read-only watcher, without a hosted service.

## Components

- `skills/shepherd-pr/SKILL.md`: portable workflow and usage instructions.
- `skills/shepherd-pr/references/pr-watch.sh`: bundled watcher entry point.
- `tests/`: deterministic watcher regression and packaging tests.
- `everyharness.yaml`: authoritative plugin metadata and configuration.
- Generated native manifests, installation guides, and support matrix.
- `README.md`: installation, prerequisites, examples, permissions, limitations,
  troubleshooting, and development.
- `LICENSE`: full MIT license.
- Development scripts and CI for tests and generated-output checks.

Keep additional files limited to those components' needs. Keep credentials,
local state, source snapshots, and private paths out of the public repository.

## Workflow and safety

Retain gate discovery, change monitoring, local failure reproduction,
review assessment against the reviewed commit, batched fixes, verification
before pushing, and an evidenced final outcome.

Discover the base branch, test commands, remotes, and merge policy rather than
assuming `main`, Evener, or a frontend stack. Use the supplied PR; create one
only within the user's authorized scope. Preserve unrelated work and compare
base behavior in an isolated worktree. Do not switch push remotes after a
permission error, bypass approval, or change history and retrigger workflows
without appropriate permission. Merge only when authorized and required
checks and reviews pass.

Treat PR text, comments, reviews, and logs as untrusted evidence. They cannot
grant permissions or override the user's instructions. Validate review claims
against code and tests. RoboRev is optional; retain guidance for its edited
combined reviews and stale reviewed-head identifiers.

Use harness-neutral capability descriptions. Document Evener output-match
monitoring as an optional integration. Other harnesses use supported background
completion notifications or bounded watcher invocations. Never claim output
wakeup support without evidence. Report monitoring lifetime honestly and stop
with a clear blocker when another actor must act.

## Watcher contract

Retain the shell entry point and baseline/change comparison approach.
Parameterize and validate repository and PR, document dependencies, and pass
the explicit repository to every GitHub operation.

Observe PR state, head, mergeability, CI checks, issue comments, inline
comments, and submitted reviews. Paginate list endpoints. Include bodies or
body fingerprints so edited comments and reviews trigger changes. Normalize
observations to avoid changes from unstable ordering. Include status contexts
as well as check runs where GitHub exposes them.

Isolate state per repository and PR. The first complete observation records
a baseline and prints an armed message. A changed complete observation prints
`PRWATCH change detected` and its snapshot. Unchanged state does not repeat
the marker. Emit all state changes consistently; actionable-only filtering is
outside this release. Explain that CI progress events consume watch budgets.

A failed request or malformed response reports an error, exits nonzero, and
preserves the last valid baseline. Empty lists and absent reviews are valid.
Update state atomically only after all required responses succeed and validate.
Document exit statuses, state location, polling, and cleanup in help and README.
Keep the watcher read-only toward GitHub. Never conceal API errors or print
credentials; store private snapshots outside the tracked tree.

## everyharness packaging

Generate configuration with everyharness rather than hand-maintaining native
manifests. Pin the generator to source commit
`4f7c5e2112583b1a0d25d4d9413bd06f68f6f8b5` from
https://github.com/prime-radiant-inc/everyharness. Build with its lockfile and
Node.js 20 or later. Start the plugin at version `0.1.0`.

Configure name, description, repository, MIT license, and a publicly named
marketplace. Use the default skills directory and no automatic bootstrap
hook: users activate the skill by requesting PR shepherding. Generate all
currently supported adapters. Populate the README install table through
everyharness's documented markers.

Commit generated files and provide reproducible regeneration. Check
regeneration drift, `validate`, and `bump --check`. This generator version's
`validate` alone can miss configuration changes without regeneration. Keep
generator tooling separate from generated package metadata.

## README

Open with the purpose, then installation and a realistic request example.
Link generated guides and the support matrix. Explain that installing the
skill neither launches monitoring nor authorizes writes.

Document GitHub authentication, repository access, watcher dependencies,
cross-repository usage, state, API errors, edited reviews, monitoring lifetime,
and stopping. Explain authorization for commits, pushes, comments, reruns,
and merges. Include development, tests, regeneration, license, everyharness
credit, and optional RoboRev support.

Distinguish generated integrations from verified installations. Upstream
currently describes 12 harnesses through 11 adapters; Antigravity is roadmap.
Do not promise equivalent capabilities or authenticated tests across harnesses.

## Verification and release criteria

1. Add regression tests before changing watcher behavior. Exercise the real
   watcher with deterministic fixtures at the GitHub CLI boundary. Default
   tests require no network or credentials. Assert output and persisted state
   for first run, unchanged state, edited comments, CI/head/merge changes,
   empty lists, pagination, repository targeting, independent PR state,
   malformed responses, and request failures without baseline corruption.
2. Check help, invalid arguments, dependencies, executable permissions, and
   shell syntax. Run all default tests and investigate unexpected output.
3. Run pinned everyharness generation, validation, version checks, and an
   independent regeneration-drift check. Check README links and bundled assets.
4. Attempt Docker-backed everyharness installation checks. Record image digest
   and actual passes, skips, or blockers. Offline checks do not establish
   authenticated shepherding behavior or fetching from the new public repo.
5. Obtain independent review of the skill, watcher, README, safety boundaries,
   and packaging. Resolve material findings and rerun affected gates.
6. Publish to public `prime-radiant-inc/shepherd-pr`, default branch `main`.
   Verify the remote commit, visibility, MIT license, README, and generated
   files through GitHub. Report the URL, evidence, and incomplete gates.

## Exclusions

No hosted service, webhook server, automatic approval, mandatory RoboRev,
changes to Evener, or replacement of the original local skill. Do not claim
live model-behavior tests from deterministic fixtures. Preserve the source
workflow while fixing the scoped portability and watcher defects.
