"""Offline CLI tests for the settle detector: only the gh executable is replaced
with a fixture server, so every test exercises the real wrapper, helper, and
roborev parser without network access."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('PRSETTLE_TEST_SCRIPT',
                             ROOT / 'skills/shepherd-pr/scripts/pr-settle.py'))

GH_FIXTURE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
fixture = json.load(open(os.environ['GH_FIXTURE']))
if args[:2] == ['pr', 'view']:
    if fixture.get('fail') == 'pr view':
        print('fixture: request denied (HTTP 403) ' + fixture.get('error_detail', ''), file=sys.stderr)
        sys.exit(1)
    if fixture.get('malformed') == 'pr view':
        print('{not-json')
        sys.exit(0)
    print(json.dumps({
        'headRefOid': fixture['head'], 'baseRefName': fixture.get('base', 'main'),
        'statusCheckRollup': fixture['rollup'],
        'reviewDecision': fixture.get('review_decision', 'APPROVED'),
        'mergeable': fixture.get('mergeable', 'MERGEABLE'),
        'mergeStateStatus': fixture.get('merge_state', 'CLEAN'),
        'state': fixture.get('state', 'OPEN'),
        'isDraft': fixture.get('draft', False)}))
    sys.exit(0)
endpoint = next((a for a in args if a.startswith('repos/')), '')
endpoint = endpoint.split('?')[0]
prefix = 'repos/' + fixture['repo']
pr = str(fixture['pr'])
routes = {
    prefix + '/issues/' + pr + '/comments': 'issue_comments',
    prefix + '/branches/' + fixture.get('base', 'main') + '/protection': 'protection',
    prefix + '/rulesets': 'rulesets',
    prefix + '/rulesets/1': 'ruleset',
    prefix: 'repository',
}
key = routes.get(endpoint)
if key is None:
    print('fixture: unknown endpoint ' + endpoint, file=sys.stderr)
    sys.exit(4)
if key in (fixture.get('fail_many') or []) or fixture.get('fail') == key:
    print('fixture: request denied (HTTP 403) ' + fixture.get('error_detail', ''), file=sys.stderr)
    sys.exit(1)
if fixture.get('malformed') == key:
    print('{not-json')
    sys.exit(0)
defaults = {'protection': None, 'rulesets': [], 'ruleset': {},
            'repository': {'default_branch': 'main'}}
if key == 'protection' and key not in fixture:
    print('fixture: Branch not protected (HTTP 404)', file=sys.stderr)
    sys.exit(1)
payload = fixture[key] if key in fixture else defaults.get(key)
pages = fixture.get('pages', {}).get(key, [payload])
if '--paginate' not in args:
    pages = pages[:1]
if '--slurp' in args:
    payload = pages
elif '--paginate' in args:
    for page in pages:
        print(json.dumps(page))
    sys.exit(0)
else:
    payload = pages[0]
print(json.dumps(payload))
'''

MARKER = '<!-- roborev-pr-comment -->'
HEAD = 'a' * 40
OTHER = 'b' * 40


def run_check(name='tests', status='COMPLETED', conclusion='SUCCESS',
              ident=None, started=None):
    entry = {'name': name, 'context': None, 'status': status,
             'conclusion': conclusion, 'state': None, '__typename': 'CheckRun'}
    if ident is not None:
        entry['id'] = ident
    if started is not None:
        entry['startedAt'] = started
    return entry


def commit_status(context='roborev', state='SUCCESS', ident=None, started=None):
    entry = {'name': None, 'context': context, 'status': None,
             'conclusion': None, 'state': state, '__typename': 'StatusContext'}
    if ident is not None:
        entry['id'] = ident
    if started is not None:
        entry['startedAt'] = started
    return entry


def roborev_comment(kind='Combined Review', sha=HEAD[:12], extra=''):
    body = f'{MARKER}\n## roborev: {kind} (`{sha}`)\n\nfindings here\n{extra}'
    return {'id': 7, 'body': body, 'user': {'login': 'roborev-primeradiant[bot]'},
            'created_at': '2026-09-17T00:00:00Z', 'updated_at': '2026-09-17T00:00:00Z'}


def observation():
    return {'repo': 'prime-radiant-inc/evener', 'pr': 1607, 'head': HEAD,
            'rollup': [run_check('tests'), commit_status('roborev', 'SUCCESS')],
            'issue_comments': [roborev_comment()]}


class SettleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.bin = self.directory / 'bin'
        self.bin.mkdir()
        gh = self.bin / 'gh'
        gh.write_text(GH_FIXTURE)
        gh.chmod(0o755)
        self.fixture = self.directory / 'fixture.json'
        self.data = observation()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        GH_FIXTURE=str(self.fixture), HOME=str(self.directory / 'home'))

    def run_tool(self, *args, head=HEAD):
        self.fixture.write_text(json.dumps(self.data))
        command = [sys.executable, str(SCRIPT), '--repo', self.data['repo'], '--pr', str(self.data['pr'])]
        if head is not None:
            command += ['--head', head]
        return subprocess.run([*command, *args],
                              env=self.env, cwd=self.directory, text=True,
                              capture_output=True, timeout=20)

    def assert_settled(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertEqual(result.stdout.count('PRSETTLE settled'), 1, result.stdout)
        self.assertNotIn('PRSETTLE state', result.stdout)

    def test_help_and_usage(self):
        help_result = subprocess.run([sys.executable, str(SCRIPT), '--help'], env=self.env,
                                     text=True, capture_output=True, timeout=15)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for term in ('--repo OWNER/REPO', '--pr NUMBER', '--head SHA', '--count N',
                     '--interval SECONDS', '--until auto|settled|approved|merged',
                     '--gates', '--no-review', '--allow-unapproved', 'python3', 'gh',
                     '0', '1', '2', '3', 'settled', 'timeout', 'roborev', 'clean',
                     'merge queue', 'required status checks', 'approving review'):
            self.assertIn(term, help_result.stdout)
        for args in ([], ['--repo', 'owner/repo'],
                     ['--repo', '../repo', '--pr', '1', '--head', HEAD],
                     ['--repo', 'owner/repo', '--pr', '0', '--head', HEAD],
                     ['--repo', 'owner/repo', '--pr', '-2', '--head', HEAD],
                     ['--repo', 'owner/repo', '--pr', '1.5', '--head', HEAD],
                     ['--repo', 'a/b/c', '--pr', '1', '--head', HEAD],
                     ['--repo', 'a/b', '--pr', '1', '--head', 'abc'],
                     ['--repo', 'a/b', '--pr', '1', '--head', HEAD, '--unknown'],
                     ['--repo', 'a/b', '--pr', '1', '--head', HEAD, '--repo', 'c/d']):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(SCRIPT), *args], env=self.env,
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('usage', result.stderr.lower())

    def test_settled_line_carries_head_checks_and_review(self):
        result = self.run_tool()
        self.assert_settled(result)
        line = next((entry for entry in result.stdout.splitlines()
                     if entry.startswith('PRSETTLE settled ')), '')
        self.assertTrue(line.startswith('PRSETTLE settled '), line)
        for token in (f'head={HEAD}', f'wanted={HEAD}', 'checks=green',
                      'roborev=current', f'reviewed={HEAD[:12]}', 'roborev_check=SUCCESS',
                      'review=APPROVED', 'merge=CLEAN', 'pr=OPEN', 'draft=no',
                      'next=merge'):
            self.assertIn(token, line)

    def test_commit_status_without_conclusion_is_not_outstanding(self):
        # The trap: a commit status row has state and a null conclusion; a naive
        # conclusion != SUCCESS test would call it outstanding.
        self.data['rollup'] = [commit_status('roborev', 'SUCCESS')]
        self.assert_settled(self.run_tool())
        self.data['rollup'] = [commit_status('external', 'NEUTRAL')]
        self.assert_settled(self.run_tool())

    def test_commit_status_pending_is_outstanding(self):
        self.data['rollup'] = [run_check('tests'), commit_status('build', 'PENDING')]
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('checks=outstanding:build', result.stdout)
        self.assertIn('PRSETTLE timeout', result.stdout)
        self.assertNotIn('PRSETTLE settled', result.stdout)

    def test_superseded_cancelled_entry_yields_to_a_newer_check_run(self):
        # statusCheckRollup keeps history: a superseded run leaves CANCELLED
        # entries that share a name with the newer run's SUCCESS. gh pr checks
        # reports the current run per name; the tool must do the same instead
        # of treating every historical entry as a current one.
        superseded = run_check('tests', 'COMPLETED', 'CANCELLED',
                               ident=100, started='2026-09-17T18:19:04Z')
        current = run_check('tests', 'COMPLETED', 'SUCCESS',
                            ident=200, started='2026-09-17T18:19:15Z')
        for rollup in ([superseded, current], [current, superseded]):
            with self.subTest(first=rollup[0]['conclusion']):
                self.data['rollup'] = [*rollup, commit_status('roborev', 'SUCCESS')]
                result = self.run_tool()
                self.assert_settled(result)
                self.assertIn('checks=green', result.stdout)

    def test_newest_run_wins_by_start_time_when_no_id_is_emitted(self):
        # gh pr view does not emit a check-run id, so startedAt is the ordering
        # fallback. The superseded entry is listed second and must still lose.
        superseded = run_check('native', 'COMPLETED', 'CANCELLED',
                               started='2026-09-17T18:19:04Z')
        current = run_check('native', 'COMPLETED', 'SUCCESS',
                            started='2026-09-17T18:19:15Z')
        self.data['rollup'] = [current, superseded, commit_status('roborev', 'SUCCESS')]
        self.assert_settled(self.run_tool())

    def test_newest_failure_stops_the_wait(self):
        # A fix that ignores failures wholesale would wrongly settle here: the
        # newest run for the name is the failing one. A completed failure is a
        # terminal outcome, not a reason to keep polling until the budget ends.
        older = run_check('static', 'COMPLETED', 'SUCCESS',
                          ident=100, started='2026-09-17T18:19:04Z')
        newest = run_check('static', 'COMPLETED', 'FAILURE',
                           ident=200, started='2026-09-17T18:27:29Z')
        self.data['rollup'] = [older, newest, commit_status('roborev', 'SUCCESS')]
        result = self.run_tool()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('checks=failed:static', result.stdout)
        self.assertEqual(result.stdout.count('PRSETTLE failed'), 1, result.stdout)
        self.assertNotIn('PRSETTLE timeout', result.stdout)
        self.assertNotIn('PRSETTLE settled', result.stdout)

    def test_newest_in_progress_remains_outstanding(self):
        older = run_check('tests', 'COMPLETED', 'SUCCESS',
                          ident=100, started='2026-09-17T18:19:04Z')
        newest = run_check('tests', 'IN_PROGRESS', None,
                           ident=200, started='2026-09-17T18:19:15Z')
        self.data['rollup'] = [older, newest, commit_status('roborev', 'SUCCESS')]
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('checks=outstanding:tests', result.stdout)
        self.assertNotIn('PRSETTLE settled', result.stdout)

    def test_superseded_commit_status_context_is_judged_by_its_newest_state(self):
        # The trap: a commit status row carries state with a null conclusion,
        # and a context can carry history too. The newest state per context
        # decides, read through `.conclusion // .state // .status`.
        self.data['rollup'] = [
            commit_status('external', 'FAILURE', started='2026-09-17T18:19:04Z'),
            commit_status('external', 'SUCCESS', started='2026-09-17T18:19:15Z'),
            run_check('tests'), commit_status('roborev', 'SUCCESS')]
        self.assert_settled(self.run_tool())
        self.data['rollup'] = [
            commit_status('external', 'SUCCESS', started='2026-09-17T18:19:04Z'),
            commit_status('external', 'PENDING', started='2026-09-17T18:19:15Z'),
            run_check('tests'), commit_status('roborev', 'SUCCESS')]
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('checks=outstanding:external', result.stdout)

    def test_a_current_cancelled_check_is_a_terminal_failure(self):
        # A job cancelled in the superseded run that never re-ran leaves one
        # CANCELLED entry and no newer run for its name. The newest entry for
        # that name is still the CANCELLED one, so the wait ends immediately
        # with a definite report instead of polling to the budget.
        self.data['rollup'] = [
            run_check('tests', 'COMPLETED', 'SUCCESS', ident=200,
                      started='2026-09-17T18:19:15Z'),
            run_check('retired', 'COMPLETED', 'CANCELLED', ident=100,
                      started='2026-09-17T18:19:05Z'),
            commit_status('roborev', 'SUCCESS')]
        result = self.run_tool('--count', '2', '--interval', '1')
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertIn('checks=failed:retired', result.stdout)
        self.assertEqual(result.stdout.count('PRSETTLE state'), 0, result.stdout)
        self.assertEqual(result.stdout.count('PRSETTLE failed'), 1, result.stdout)
        self.assertNotIn('PRSETTLE settled', result.stdout)

    def test_unfinished_check_runs_are_outstanding(self):
        for status, conclusion, name in (('IN_PROGRESS', None, 'tests'),
                                         ('QUEUED', None, 'lint')):
            with self.subTest(conclusion=conclusion, status=status):
                self.data['rollup'] = [run_check(name, status, conclusion)]
                result = self.run_tool()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(f'checks=outstanding:{name}', result.stdout)

    def test_completed_failures_end_the_wait(self):
        for conclusion, name in (('FAILURE', 'tests'), ('CANCELLED', 'native'),
                                 ('TIMED_OUT', 'fuzz'), ('ACTION_REQUIRED', 'deploy'),
                                 ('STARTUP_FAILURE', 'build')):
            with self.subTest(conclusion=conclusion):
                self.data['rollup'] = [run_check(name, 'COMPLETED', conclusion)]
                result = self.run_tool('--count', '3', '--interval', '1')
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn(f'checks=failed:{name}', result.stdout)
                self.assertNotIn('PRSETTLE timeout', result.stdout)

    def test_accepted_conclusions_are_green(self):
        self.data['rollup'] = [run_check('docs', 'COMPLETED', 'SKIPPED'),
                               run_check('meta', 'COMPLETED', 'NEUTRAL'),
                               run_check('tests', 'COMPLETED', 'SUCCESS')]
        self.assert_settled(self.run_tool())
        self.assertIn('checks=green', self.run_tool().stdout)

    def test_head_moved_under_the_wait_ends_the_wait(self):
        self.data['head'] = OTHER
        self.data['issue_comments'] = [roborev_comment(sha=OTHER[:12])]
        result = self.run_tool('--count', '3', '--interval', '1')
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn(f'head={OTHER} wanted={HEAD}', result.stdout)
        self.assertEqual(result.stdout.count('PRSETTLE moved'), 1, result.stdout)
        self.assertNotIn('PRSETTLE timeout', result.stdout)

    def test_short_head_prefix_is_accepted(self):
        result = self.run_tool(head=HEAD[:7])
        self.assert_settled(result)
        self.assertIn(f'wanted={HEAD[:7]}', result.stdout)

    def test_head_defaults_to_the_observed_head(self):
        result = self.run_tool(head=None)
        self.assert_settled(result)
        self.assertIn(f'wanted={HEAD}', result.stdout)

    def test_stale_review_is_not_settled(self):
        self.data['issue_comments'] = [roborev_comment(sha=OTHER[:12])]
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('roborev=stale', result.stdout)
        self.assertIn(f'reviewed={OTHER[:12]}', result.stdout)

    def test_missing_review_is_not_settled(self):
        self.data['issue_comments'] = []
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('roborev=none', result.stdout)
        self.assertIn('reviewed=-', result.stdout)

    def test_repository_without_roborev_settles_without_a_review(self):
        # No combined comment and no roborev check context: the repository does
        # not run roborev, so waiting for a review would always time out.
        self.data['issue_comments'] = []
        self.data['rollup'] = [run_check('tests')]
        result = self.run_tool()
        self.assert_settled(result)
        self.assertIn('roborev=absent', result.stdout)

    def test_no_review_flag_ignores_a_missing_review(self):
        self.data['issue_comments'] = []
        result = self.run_tool('--no-review')
        self.assert_settled(result)
        self.assertIn('roborev=skipped', result.stdout)

    def test_approval_requirement_blocks_settlement_without_admin_override(self):
        self.data['review_decision'] = 'REVIEW_REQUIRED'
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('review=REVIEW_REQUIRED', result.stdout)
        self.assertNotIn('PRSETTLE settled', result.stdout)
        self.assert_settled(self.run_tool('--allow-unapproved'))

    def test_changes_requested_ends_the_wait(self):
        self.data['review_decision'] = 'CHANGES_REQUESTED'
        result = self.run_tool('--count', '3', '--interval', '1')
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn('review=CHANGES_REQUESTED', result.stdout)
        self.assertEqual(result.stdout.count('PRSETTLE blocked'), 1, result.stdout)

    def test_until_approved_waits_for_approval(self):
        self.data['review_decision'] = 'REVIEW_REQUIRED'
        result = self.run_tool('--until', 'approved')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('review=REVIEW_REQUIRED', result.stdout)
        self.assertEqual(result.stdout.count('PRSETTLE state'), 1, result.stdout)
        self.data['review_decision'] = 'APPROVED'
        self.assert_settled(self.run_tool('--until', 'approved'))

    def test_until_merged_waits_through_the_queue(self):
        self.data['merge_state'] = 'BLOCKED'
        result = self.run_tool('--until', 'merged', '--count', '2', '--interval', '1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('merge=BLOCKED', result.stdout)
        self.assertNotIn('PRSETTLE merged', result.stdout)
        self.data['state'] = 'MERGED'
        result = self.run_tool('--until', 'merged')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('PRSETTLE merged'), 1, result.stdout)

    def test_closed_unmerged_ends_the_wait(self):
        self.data['state'] = 'CLOSED'
        result = self.run_tool()
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('PRSETTLE closed'), 1, result.stdout)

    def test_gates_prints_required_checks_approvals_and_queue(self):
        self.data['protection'] = {
            'required_status_checks': {'contexts': ['tests', 'lint']},
            'required_pull_request_reviews': {'required_approving_review_count': 1}}
        self.data['rulesets'] = [{'id': 1, 'name': 'main merge queue',
                                  'enforcement': 'active', 'target': 'branch'}]
        self.data['ruleset'] = {
            'name': 'main merge queue', 'enforcement': 'active', 'target': 'branch',
            'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
            'rules': [{'type': 'merge_queue', 'parameters': {}}]}
        result = self.run_tool('--gates', head=None)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = result.stdout.strip()
        self.assertTrue(line.startswith('PRSETTLE gates '), line)
        for token in ('required=lint,tests', 'approvals=1', 'queue=yes', 'roborev=yes',
                      'base=main', 'next=enqueue'):
            self.assertIn(token, line)
        self.assertNotIn('PRSETTLE state', result.stdout)

    def test_merge_queue_gate_yields_enqueue(self):
        self.data['rulesets'] = [{'id': 1, 'name': 'main merge queue',
                                  'enforcement': 'active', 'target': 'branch'}]
        self.data['ruleset'] = {
            'name': 'main merge queue', 'enforcement': 'active', 'target': 'branch',
            'conditions': {'ref_name': {'include': ['refs/heads/main'], 'exclude': []}},
            'rules': [{'type': 'merge_queue', 'parameters': {}}]}
        result = self.run_tool()
        self.assert_settled(result)
        self.assertIn('queue=yes', result.stdout)
        self.assertIn('next=enqueue', result.stdout)

    def test_auto_waits_for_a_required_approval_but_settled_does_not(self):
        self.data['protection'] = {
            'required_status_checks': {'contexts': ['tests']},
            'required_pull_request_reviews': {'required_approving_review_count': 1}}
        self.data['review_decision'] = 'REVIEW_REQUIRED'
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('approvals=1', result.stdout)
        self.assertIn('next=approval', result.stdout)
        self.assertNotIn('PRSETTLE settled', result.stdout)
        result = self.run_tool('--until', 'settled')
        self.assert_settled(result)
        self.assertIn('next=approval', result.stdout)
        self.assert_settled(self.run_tool('--allow-unapproved'))

    def test_unreadable_gates_still_settle(self):
        self.data['fail_many'] = ['protection', 'rulesets']
        result = self.run_tool()
        self.assert_settled(result)
        self.assertIn('gates=unknown', result.stdout)

    def test_review_failed_for_the_head_settles_and_is_reported(self):
        self.data['issue_comments'] = [roborev_comment(kind='Review Failed')]
        result = self.run_tool()
        self.assert_settled(result)
        self.assertIn('roborev=review-failed', result.stdout)

    def test_settled_does_not_mean_clean(self):
        # Findings in the combined comment do not stop settlement; the severities
        # and verdict hint must ride on the settled line for the caller to judge.
        self.data['issue_comments'] = [roborev_comment(
            extra='\n**Severity**: High\n**Verdict:** One regression found.\n')]
        result = self.run_tool()
        self.assert_settled(result)
        self.assertIn('severities=high:1', result.stdout)
        self.assertIn('verdict=One regression found.', result.stdout)
        self.assertIn('roborev_check=SUCCESS', result.stdout)

    def test_null_rollup_is_green(self):
        self.data['rollup'] = None
        self.assert_settled(self.run_tool())

    def test_unchanged_batch_prints_one_state_line_then_timeout(self):
        self.data['rollup'] = [run_check('tests', 'IN_PROGRESS', None)]
        result = self.run_tool('--count', '3', '--interval', '1')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(result.stdout.count('PRSETTLE state'), 1, result.stdout)
        self.assertEqual(result.stdout.count('PRSETTLE timeout'), 1, result.stdout)
        self.assertIn('checks=outstanding:tests', result.stdout)

    def test_batch_usage_validation(self):
        for args in (['--count', '0'], ['--count', '-1'], ['--count', 'x'],
                     ['--count', '1001'], ['--interval', '0'], ['--interval', '-1'],
                     ['--interval', 'abc'], ['--interval', '86401'],
                     ['--count', '2', '--count', '3'], ['--interval', '5', '--interval', '6']):
            with self.subTest(args=args):
                result = self.run_tool(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('usage', result.stderr.lower())

    def test_pr_view_failure_is_operational_error(self):
        self.data['fail'] = 'pr view'
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')
        self.assertIn('error', result.stderr.lower())

    def test_comments_failure_is_operational_error(self):
        self.data['fail'] = 'issue_comments'
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')
        self.assertIn('error', result.stderr.lower())

    def test_malformed_json_is_operational_error(self):
        for key in ('pr view', 'issue_comments'):
            with self.subTest(key=key):
                self.data['malformed'] = key
                result = self.run_tool()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout, '')

    def test_invalid_response_shapes_are_operational_errors(self):
        for head in ('a' * 41, None, 7):
            with self.subTest(head=head):
                self.data['head'] = head
                result = self.run_tool()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout, '')
        self.data['head'] = HEAD
        for rollup in ([False], 'not-a-list'):
            with self.subTest(rollup=rollup):
                self.data['rollup'] = rollup
                result = self.run_tool()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(result.stdout, '')
        # An entry with no recognizable state is treated conservatively as
        # outstanding rather than assumed green.
        self.data['rollup'] = [{'name': 'x'}]
        result = self.run_tool()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn('checks=outstanding:x', result.stdout)

    def test_pagination_of_comments(self):
        self.data['pages'] = {'issue_comments': [[], [roborev_comment()]]}
        self.assert_settled(self.run_tool())

    def test_error_diagnostics_redact_authentication_secrets(self):
        self.env['GH_TOKEN'] = 'credential-value-from-environment'
        self.data['fail'] = 'issue_comments'
        self.data['error_detail'] = 'credential-value-from-environment github_pat_example123'
        result = self.run_tool()
        self.assertEqual(result.returncode, 1)
        self.assertIn('HTTP 403', result.stderr)
        self.assertIn('[REDACTED]', result.stderr)
        for secret in ('credential-value-from-environment', 'github_pat_example123'):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_missing_gh_is_operational_error(self):
        (self.bin / 'gh').unlink()
        (self.bin / 'python3').symlink_to(sys.executable)
        self.env['PATH'] = str(self.bin)
        self.fixture.write_text(json.dumps(self.data))
        result = subprocess.run([sys.executable, str(SCRIPT), '--repo', self.data['repo'],
                                 '--pr', '1607', '--head', HEAD],
                                env=self.env, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('gh', result.stderr)
        self.assertEqual(result.stdout, '')

if __name__ == '__main__':
    unittest.main()
