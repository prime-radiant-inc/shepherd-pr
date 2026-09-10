# Portable Shepherd PR Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Track steps with checkboxes.

**Goal:** Publish a tested, MIT-licensed shepherd-pr plugin through everyharness.

**Architecture:** Keep one discoverable skill and its read-only watcher. Use explicit repository/PR arguments, validated complete snapshots, and isolated atomic state. Generate native packaging and install docs from one YAML file.

**Tech Stack:** Bash, GitHub CLI, JSON tooling chosen for portable snapshot validation, deterministic Python unittest boundary fixtures, pinned everyharness/Node.js.

**Spec:** `docs/superpowers/specs/2026-09-10-shepherd-pr-plugin-design.md`

## Global Constraints

- Public repository: `prime-radiant-inc/shepherd-pr`; MIT; Prime Radiant, Inc.
- Version `0.1.0`; Node.js 20 or later for generator development only.
- Pin everyharness to `4f7c5e2112583b1a0d25d4d9413bd06f68f6f8b5`.
- Preserve Evener and the original skill. Work only in the new repository.
- No credentials or network for default tests; no automatic bootstrap hooks.
- Explicit repository targeting; no unauthorized remote switches or merges.

## Task 1: Portable watcher

**Files:** `skills/shepherd-pr/references/pr-watch.sh`, any adjacent JSON helper required by the chosen utility, `tests/test_pr_watch.py`.
**Consumes:** Preserved source watcher and the approved spec.
**Produces:** Executable `pr-watch.sh --repo OWNER/REPO --pr NUMBER [--state-dir DIR]`; one observation per call, exit 0 for successful observations, 1 for operational failures, 2 for usage errors. Help documents dependencies and output.

- [ ] Write a regression at the CLI boundary before changing behavior. A fixture executable supplies GitHub responses; run the actual watcher and assert output plus unchanged persisted state after a failed API request. Preserve the original source for the red comparison outside the repo.

```python
before = state.read_bytes()
result = run_watcher(fail_endpoint='comments')
assert result.returncode != 0
assert state.read_bytes() == before
assert 'PRWATCH change detected' not in result.stdout
```

- [ ] Run `python3 -m unittest discover -s tests -p 'test_pr_watch.py' -v`; establish an expected assertion failure against source behavior, not a broken fixture.
- [ ] Parameterize the shell entry point. Collect paginated comments/reviews, PR/head/mergeability, checks and statuses with explicit repo targeting. Validate response shapes, normalize ordering, and commit state atomically after all calls succeed. Use a private state directory keyed by repository and PR. Fail visibly without baseline mutation.
- [ ] Add test-first increments for help/invalid inputs, first/unchanged/changed observations, edited comments, pagination, independent PR state, targeting, CI/head/merge changes, empty lists, malformed response, and private state permissions.
- [ ] Run the full watcher suite plus `bash -n skills/shepherd-pr/references/pr-watch.sh`; review real behavior and commit named files.

## Task 2: Portable skill and documentation

**Files:** `skills/shepherd-pr/SKILL.md`, `README.md`, `LICENSE`, `docs/testing.md`.
**Consumes:** Tested watcher CLI from Task 1, source skill application-test evidence.
**Produces:** Portable instructions, documented permissions/dependencies, reproducible usage and test guidance.

- [ ] Run a no-guidance control and source-guided application scenario before editing the skill: release base, Rust tests, no RoboRev, no output-watch tool, denied push, edited comment, missing approval, malicious PR instructions. Record actual gaps without inventing baseline failures.
- [ ] Adapt the source workflow around repository gate discovery and optional RoboRev. Document authorized actions, evidence trust, stale reviews, and finite monitoring. Give the real watcher command:

```bash
bash /path/to/skills/shepherd-pr/references/pr-watch.sh --repo example/widgets --pr 42
```

- [ ] Write a useful README with everyharness install-table markers and links to generated guides, watcher help, security boundaries, finite wait patterns, development, limitations, and MIT license. Add the complete MIT text.
- [ ] Repeat the application scenario in a fresh delegate using the new skill; report actual compliance and limitations in `docs/testing.md`.
- [ ] Check frontmatter, local links, executable asset references, license metadata, and documented CLI against real help; commit named files.

## Task 3: Generated packaging and CI

**Files:** `everyharness.yaml`, generated manifests/docs/assets, `.gitignore`, `scripts/everyharness.sh`, `.github/workflows/ci.yml`, packaging tests.
**Consumes:** Skill directory and README markers from Task 2.
**Produces:** Reproducible native plugin distribution and CI.

- [ ] Add a packaging test requiring skill name, license, source config, and generated manifest; run it to show missing packaging before generation.
- [ ] Build pinned everyharness with `npm ci && npm run build` in an ignored tooling directory. Keep its dependencies separate from generated package metadata.
- [ ] Write the config:

```yaml
name: shepherd-pr
version: 0.1.0
description: Shepherd GitHub pull requests through CI and review.
license: MIT
repository: https://github.com/prime-radiant-inc/shepherd-pr
marketplace:
  name: shepherd-pr
  description: Shepherd PR plugin marketplace
  source: repository
```

- [ ] Run `node /path/to/everyharness/dist/cli.js generate`, `validate`, and `bump --check`. Use the pinned wrapper in development/CI and test regeneration drift including README and new files, not validate alone.
- [ ] Run all deterministic tests, packaging checks, and generator gates. Attempt Docker-backed `everyharness test`, recording the digest and each skip or blocker.
- [ ] Independently review spec compliance, then code quality and safety. Fix findings with regression evidence and rerun gates. Commit named paths only.

## Task 4: Public delivery

**Files:** Release evidence in `docs/testing.md`; repository metadata on GitHub.
**Consumes:** Reviewed commit, passing deterministic checks, honest install-check results.
**Produces:** Public repository with default branch main and verified remote contents.

- [ ] Verify local status and branch, then create main from the reviewed commit.
- [ ] Create and push the authorized repository:

```bash
gh repo create prime-radiant-inc/shepherd-pr --public --source=. --remote=origin --push --description 'A portable coding-agent plugin for shepherding GitHub pull requests through CI and review.'
```

- [ ] Verify `gh repo view prime-radiant-inc/shepherd-pr --json url,visibility,defaultBranchRef`, remote HEAD, GitHub LICENSE/README/config content, and CI result.
- [ ] Report repository URL, MIT license, everyharness usage, README, tests, and any incomplete gate. Preserve all pre-existing files and remove temporary artifacts we created.
