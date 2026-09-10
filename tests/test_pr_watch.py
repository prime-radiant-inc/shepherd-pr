"""Offline CLI tests: only the gh executable is replaced with a fixture server."""
import copy
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import stat
import runpy
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('PRWATCH_TEST_SCRIPT', ROOT / 'skills/shepherd-pr/references/pr-watch.sh'))

# This fixture returns REST-shaped responses and honors pagination. The optional
# --jq support lets the same regression demonstrate the preserved source defect;
# production tests and the shipped watcher do not require jq.
GH_FIXTURE = r'''#!/usr/bin/env python3
import json, os, subprocess, sys
args = sys.argv[1:]
fixture = json.load(open(os.environ['GH_FIXTURE']))
if args[:2] == ['pr', 'view']:
    payload = {'state': 'OPEN', 'mergeStateStatus': 'CLEAN', 'reviewDecision': '', 'statusCheckRollup': []}
else:
    endpoint = next((a for a in args if a.startswith('repos/')), '')
    endpoint = endpoint.split('?')[0]
    prefix = 'repos/' + fixture['repo']
    pr = str(fixture['pr'])
    routes = {
        prefix + '/pulls/' + pr: 'pull',
        prefix + '/issues/' + pr + '/comments': 'issue_comments',
        prefix + '/pulls/' + pr + '/comments': 'review_comments',
        prefix + '/pulls/' + pr + '/reviews': 'reviews',
        prefix + '/commits/' + fixture['pull']['head']['sha'] + '/check-runs': 'checks',
        prefix + '/commits/' + fixture['pull']['head']['sha'] + '/statuses': 'statuses',
    }
    key = routes.get(endpoint)
    if key is None:
        print('fixture: unknown repository, PR, head or endpoint', file=sys.stderr)
        sys.exit(4)
    if fixture.get('fail') == key:
        print('fixture: request denied (HTTP 403) ' + fixture.get('error_detail', ''), file=sys.stderr)
        sys.exit(1)
    if fixture.get('malformed') == key:
        print('{not-json')
        sys.exit(0)
    payload = fixture[key]
    if key != 'pull':
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
if '--jq' in args:
    result = subprocess.run(['jq', '-r', args[args.index('--jq') + 1]], input=json.dumps(payload), text=True)
    sys.exit(result.returncode)
print(json.dumps(payload))
'''


def observation():
    return {
        'repo': 'prime-radiant-inc/evener', 'pr': 954,
        'pull': {'number': 954, 'state': 'open', 'draft': False, 'merged': False,
                 'mergeable': True, 'mergeable_state': 'clean', 'head': {'sha': 'a' * 40}},
        'issue_comments': [{'id': 1, 'body': 'original finding', 'user': {'login': 'reviewer'},
                            'created_at': '2026-09-01T00:00:00Z', 'updated_at': '2026-09-01T00:00:00Z'}],
        'review_comments': [], 'reviews': [],
        'checks': {'total_count': 0, 'check_runs': []}, 'statuses': [],
    }


def pending_review():
    # Official GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews returns
    # components/schemas/pull-request-review: submitted_at is optional and,
    # when present, a date-time string. Keep all required review fields here.
    # https://raw.githubusercontent.com/github/rest-api-description/main/descriptions/api.github.com/api.github.com.json
    return {
        'id': 3, 'node_id': 'PRR_fixture', 'body': 'draft review', 'state': 'PENDING',
        'commit_id': 'a' * 40, 'user': None, 'author_association': 'NONE',
        'html_url': 'https://github.com/prime-radiant-inc/evener/pull/954#pullrequestreview-3',
        'pull_request_url': 'https://api.github.com/repos/prime-radiant-inc/evener/pulls/954',
        '_links': {
            'html': {'href': 'https://github.com/prime-radiant-inc/evener/pull/954#pullrequestreview-3'},
            'pull_request': {'href': 'https://api.github.com/repos/prime-radiant-inc/evener/pulls/954'},
        },
    }


def concurrent_lock_worker(helper, directory, barrier, results):
    # Real helper and real filesystem: isolate the first-create boundary from gh
    # startup timing while the CLI test continues to verify observable snapshots.
    watcher = runpy.run_path(helper)
    errors = []
    with watcher['state_directory'](directory) as descriptor:
        for number in range(100):
            barrier.wait(timeout=30)
            try:
                with watcher['locked'](descriptor, str(number)):
                    pass
            except OSError as error:
                errors.append((number, error.errno, str(error)))
            barrier.wait(timeout=30)
    results.put(errors)


class WatcherTests(unittest.TestCase):
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
        self.state = self.directory / 'state'
        self.data = observation()
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        GH_FIXTURE=str(self.fixture), STATE_FILE=str(self.state / 'source-baseline'),
                        HOME=str(self.directory / 'home'), XDG_STATE_HOME=str(self.directory / 'xdg'))

    def run_watcher(self, *args):
        self.fixture.write_text(json.dumps(self.data))
        # The original source expects the caller to provide an existing parent.
        if not self.state.exists() and not self.state.is_symlink():
            self.state.mkdir(mode=0o700, exist_ok=True)
        return subprocess.run(['bash', str(SCRIPT), '--repo', self.data['repo'], '--pr', str(self.data['pr']),
                               '--state-dir', str(self.state), *args],
                              env=self.env, cwd=self.directory, text=True, capture_output=True, timeout=15)

    def baseline(self):
        files = [p for p in self.state.iterdir() if p.suffix == '.json' or p.name == 'source-baseline']
        self.assertEqual(len(files), 1, files)
        return files[0]

    def assert_success(self, result, marker):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(marker, result.stdout)
        self.assertEqual(result.stderr, '')

    def test_failed_request_preserves_baseline(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        state = self.baseline()
        before = state.read_bytes()
        self.data['fail'] = 'issue_comments'
        result = self.run_watcher()
        with self.subTest('failure is nonzero'):
            self.assertEqual(result.returncode, 1, result.stdout)
        with self.subTest('baseline preserved'):
            self.assertEqual(state.read_bytes(), before)
        with self.subTest('no false change'):
            self.assertNotIn('PRWATCH change detected', result.stdout)
        self.assertIn('error', result.stderr.lower())

    def test_help_and_usage(self):
        help_result = subprocess.run(['bash', str(SCRIPT), '--help'], env=self.env,
                                     text=True, capture_output=True, timeout=15)
        self.assert_success(help_result, '--repo OWNER/REPO')
        for term in ('--pr NUMBER', '--state-dir DIR', 'python3', 'gh', '0', '1', '2', 'poll', 'cleanup'):
            self.assertIn(term, help_result.stdout)
        for args in ([], ['--repo', 'owner/repo'], ['--repo', '../repo', '--pr', '1'],
                     ['--repo', 'owner/repo', '--pr', '0'], ['--repo', 'owner/repo', '--pr', '-2'],
                     ['--repo', 'owner/repo', '--pr', '1.5'], ['--repo', 'a/b/c', '--pr', '1'],
                     ['--repo', 'a/b', '--pr', '1', '--unknown'],
                     ['--repo', 'a/b', '--pr', '1', '--repo', 'c/d']):
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(SCRIPT), *args], env=self.env,
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('usage', result.stderr.lower())
        self.assertFalse(self.state.exists())

    def test_first_unchanged_and_complete_snapshot(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        state = self.baseline()
        before = state.read_bytes()
        self.assert_success(self.run_watcher(), 'PRWATCH no change')
        self.assertEqual(state.read_bytes(), before)
        snapshot = json.loads(before)
        self.assertEqual(snapshot['repo'], self.data['repo'])
        self.assertEqual(snapshot['pr'], 954)
        self.assertEqual(snapshot['pull']['head'], 'a' * 40)
        self.assertEqual(snapshot['issue_comments'][0]['body'], 'original finding')
        self.assertEqual(snapshot['reviews'], [])
        self.assertEqual(snapshot['checks'], [])
        self.assertEqual(snapshot['statuses'], [])

    def test_comment_body_edits_emit_snapshot(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        self.data['issue_comments'][0]['body'] = 'edited non-bot verdict: Combined Review (`oldsha`)'
        result = self.run_watcher()
        self.assert_success(result, 'PRWATCH change detected')
        self.assertIn(self.data['issue_comments'][0]['body'], result.stdout)
        printed = json.loads(result.stdout.split('\n', 1)[1])
        self.assertEqual(printed, json.loads(self.baseline().read_bytes()))
        self.assert_success(self.run_watcher(), 'PRWATCH no change')

    def test_inline_and_submitted_review_edits(self):
        comment = copy.deepcopy(self.data['issue_comments'][0])
        comment.update(path='src/main.py', line=9, original_line=8, commit_id='a' * 40,
                       original_commit_id='b' * 40, in_reply_to_id=None)
        self.data['review_comments'] = [comment]
        self.data['reviews'] = [{'id': 3, 'body': 'please fix', 'user': {'login': 'reviewer'},
                                 'state': 'CHANGES_REQUESTED', 'submitted_at': '2026-09-01T00:00:00Z',
                                 'commit_id': 'a' * 40}]
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        for key in ('review_comments', 'reviews'):
            self.data[key][0]['body'] = 'updated review text'
            result = self.run_watcher()
            self.assert_success(result, 'PRWATCH change detected')
            self.assertIn('updated review text', result.stdout)
        self.assertEqual(json.loads(self.baseline().read_bytes())['reviews'][0]['commit_id'], 'a' * 40)

    def test_pending_review_baseline_unchanged_body_edit_and_submission(self):
        draft = pending_review()
        self.data['reviews'] = [draft]
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        snapshot = json.loads(before)
        self.assertEqual(snapshot['reviews'][0]['state'], draft['state'])
        self.assertEqual(snapshot['reviews'][0]['body'], draft['body'])
        self.assert_success(self.run_watcher(), 'PRWATCH no change')
        self.assertEqual(self.baseline().read_bytes(), before)

        draft['body'] = 'edited draft review'
        result = self.run_watcher()
        self.assert_success(result, 'PRWATCH change detected')
        printed = json.loads(result.stdout.split('\n', 1)[1])
        self.assertEqual(printed['reviews'][0]['body'], draft['body'])
        self.assertEqual(printed['reviews'][0]['state'], 'PENDING')
        self.assertEqual(printed, json.loads(self.baseline().read_bytes()))
        self.assert_success(self.run_watcher(), 'PRWATCH no change')

        draft.update(state='COMMENTED', submitted_at='2026-09-10T19:00:00Z')
        result = self.run_watcher()
        self.assert_success(result, 'PRWATCH change detected')
        submitted = json.loads(result.stdout.split('\n', 1)[1])['reviews'][0]
        self.assertEqual(submitted['state'], draft['state'])
        self.assertEqual(submitted['submitted_at'], draft['submitted_at'])
        self.assertEqual(submitted['body'], draft['body'])
        self.assert_success(self.run_watcher(), 'PRWATCH no change')

    def test_pending_review_added_to_existing_baseline(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        self.data['reviews'] = [pending_review()]
        result = self.run_watcher()
        self.assert_success(result, 'PRWATCH change detected')
        printed = json.loads(result.stdout.split('\n', 1)[1])
        self.assertEqual(printed['reviews'][0]['body'], self.data['reviews'][0]['body'])
        self.assertEqual(printed['reviews'][0]['state'], 'PENDING')
        self.assertEqual(printed, json.loads(self.baseline().read_bytes()))
        self.assert_success(self.run_watcher(), 'PRWATCH no change')

    def test_malformed_present_review_timestamp_preserves_baseline(self):
        review = pending_review()
        review.update(state='COMMENTED', submitted_at='2026-09-10T19:00:00Z')
        self.data['reviews'] = [review]
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        for state in ('PENDING', 'COMMENTED'):
            for timestamp in (0, True, [], {}):
                with self.subTest(state=state, timestamp=timestamp):
                    review.update(state=state, submitted_at=timestamp)
                    result = self.run_watcher()
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn('invalid response shape: submitted_at', result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertEqual(self.baseline().read_bytes(), before)
        review.update(state='COMMENTED', submitted_at='2026-09-10T19:00:00Z')
        self.assert_success(self.run_watcher(), 'PRWATCH no change')

    def test_head_mergeability_and_pr_state_changes(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        for key, value in (('head', {'sha': 'b' * 40}), ('mergeable', None),
                           ('mergeable_state', 'dirty'), ('state', 'closed'),
                           ('merged', True), ('draft', True)):
            with self.subTest(key=key):
                self.data['pull'][key] = value
                self.assert_success(self.run_watcher(), 'PRWATCH change detected')
                self.assert_success(self.run_watcher(), 'PRWATCH no change')

    def test_check_and_status_changes(self):
        self.data['checks'] = {'total_count': 1, 'check_runs': [
            {'id': 11, 'name': 'tests', 'status': 'in_progress', 'conclusion': None,
             'head_sha': 'a' * 40, 'details_url': 'https://example.test/check/11'}]}
        self.data['statuses'] = [{'id': 20, 'context': 'external-ci', 'state': 'pending',
                                  'description': 'running', 'target_url': None,
                                  'created_at': '2026-09-01T00:00:00Z', 'updated_at': '2026-09-01T00:00:00Z'}]
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        self.data['checks']['check_runs'][0].update(status='completed', conclusion='failure')
        self.assert_success(self.run_watcher(), 'PRWATCH change detected')
        self.data['statuses'][0]['state'] = 'success'
        result = self.run_watcher()
        self.assert_success(result, 'PRWATCH change detected')
        snapshot = json.loads(self.baseline().read_bytes())
        self.assertEqual(snapshot['checks'][0]['conclusion'], 'failure')
        self.assertEqual(snapshot['statuses'][0]['context'], 'external-ci')
        self.assertEqual(snapshot['statuses'][0]['state'], 'success')

    def test_every_list_endpoint_is_paginated(self):
        inline = dict(self.data['issue_comments'][0], path='main.py', commit_id='a' * 40)
        review = {'id': 3, 'body': 'review', 'user': None, 'state': 'APPROVED',
                  'submitted_at': None, 'commit_id': 'a' * 40}
        check = {'id': 4, 'name': 'lint', 'status': 'queued', 'conclusion': None, 'head_sha': 'a' * 40}
        status = {'id': 5, 'context': 'lint', 'state': 'pending', 'description': None, 'target_url': None,
                  'created_at': '2026-09-01T00:00:00Z', 'updated_at': '2026-09-01T00:00:00Z'}
        self.data['pages'] = {
            'issue_comments': [[], self.data['issue_comments']],
            'review_comments': [[], [inline]], 'reviews': [[], [review]],
            'checks': [{'total_count': 1, 'check_runs': []}, {'total_count': 1, 'check_runs': [check]}],
            'statuses': [[], [status]],
        }
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        snapshot = json.loads(self.baseline().read_bytes())
        for key in self.data['pages']:
            with self.subTest(endpoint=key):
                self.assertEqual(len(snapshot[key]), 1)
        self.data['pages']['issue_comments'][1][0]['body'] = 'edit on page two'
        self.assert_success(self.run_watcher(), 'PRWATCH change detected')

    def test_order_is_normalized(self):
        self.data['issue_comments'].append(dict(self.data['issue_comments'][0], id=2, body='second'))
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        self.data['issue_comments'].reverse()
        self.assert_success(self.run_watcher(), 'PRWATCH no change')
        self.assertEqual(self.baseline().read_bytes(), before)

    def test_explicit_target_and_independent_state(self):
        self.data['repo'] = 'another-owner/other-repo'
        self.data['pr'] = self.data['pull']['number'] = 17
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        first = self.baseline()
        before = first.read_bytes()
        self.data['pr'] = self.data['pull']['number'] = 18
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        self.data['repo'] = 'another-owner/third-repo'
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        self.assertEqual(len(list(self.state.glob('*.json'))), 3)
        self.assertEqual(first.read_bytes(), before)
        self.data['repo'] = 'another-owner/other-repo'
        self.data['pr'] = self.data['pull']['number'] = 17
        self.assert_success(self.run_watcher(), 'PRWATCH no change')

    def test_all_request_failures_and_malformed_json_preserve_baseline(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        for mode in ('fail', 'malformed'):
            for key in ('pull', 'issue_comments', 'review_comments', 'reviews', 'checks', 'statuses'):
                with self.subTest(mode=mode, endpoint=key):
                    self.data[mode] = key
                    result = self.run_watcher()
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn('error', result.stderr.lower())
                    self.assertNotIn('PRWATCH change detected', result.stdout)
                    self.assertEqual(self.baseline().read_bytes(), before)
            del self.data[mode]
        self.assert_success(self.run_watcher(), 'PRWATCH no change')

    def test_invalid_response_shapes_preserve_baseline(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        cases = [('pull', []), ('pull', dict(self.data['pull'], mergeable='yes')),
                 ('pull', dict(self.data['pull'], head={'sha': None})),
                 ('pull', dict(self.data['pull'], head={'sha': 'a' * 41})),
                 ('pull', dict(self.data['pull'], number=955)),
                 ('issue_comments', {}), ('issue_comments', [{'id': 1}]),
                 ('review_comments', [False]), ('reviews', [None]),
                 ('checks', {'total_count': 1, 'check_runs': {}}),
                 ('checks', {'total_count': 1, 'check_runs': [{'id': 1}]}),
                 ('statuses', [{'id': 1}]), ('statuses', None)]
        for key, payload in cases:
            with self.subTest(endpoint=key, payload=payload):
                old = self.data[key]
                self.data[key] = payload
                result = self.run_watcher()
                self.data[key] = old
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn('error', result.stderr.lower())
                self.assertEqual(self.baseline().read_bytes(), before)
                self.assertNotIn('PRWATCH change detected', result.stdout)

    def test_failure_without_baseline_does_not_arm(self):
        self.data['fail'] = 'statuses'
        result = self.run_watcher()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn('PRWATCH armed', result.stdout)
        self.assertEqual(list(self.state.glob('*.json')), [])

    def test_empty_lists_and_unknown_mergeability_are_valid(self):
        self.data['issue_comments'] = []
        self.data['pull']['mergeable'] = None
        self.data['pull']['mergeable_state'] = 'unknown'
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        self.assert_success(self.run_watcher(), 'PRWATCH no change')

    def test_private_state_permissions(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for file in self.state.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600, file)
        self.assertEqual(len(list(self.state.glob('*.json'))), 1)

    def test_insecure_directory_is_rejected(self):
        self.state.mkdir(mode=0o755)
        self.state.chmod(0o755)
        result = self.run_watcher()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('error', result.stderr.lower())
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o755)

    def test_linked_state_directory_is_rejected(self):
        target = self.directory / 'target'
        target.mkdir(mode=0o700)
        self.state.symlink_to(target, target_is_directory=True)
        result = self.run_watcher()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(list(target.iterdir()), [])

    def test_linked_baseline_is_rejected_without_touching_target(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        baseline = self.baseline()
        target = self.directory / 'target.json'
        target.write_bytes(baseline.read_bytes())
        before = target.read_bytes()
        baseline.unlink()
        baseline.symlink_to(target)
        self.data['issue_comments'][0]['body'] = 'new'
        result = self.run_watcher()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(target.read_bytes(), before)
        self.assertTrue(baseline.is_symlink())

    def test_hardlinked_baseline_is_rejected(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        baseline = self.baseline()
        target = self.directory / 'target.json'
        os.link(baseline, target)
        before = target.read_bytes()
        self.data['issue_comments'][0]['body'] = 'new'
        self.assertEqual(self.run_watcher().returncode, 1)
        self.assertEqual(baseline.read_bytes(), before)
        self.assertEqual(target.read_bytes(), before)

    def test_corrupt_baseline_is_preserved(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        baseline = self.baseline()
        baseline.write_bytes(b'{broken json')
        result = self.run_watcher()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(baseline.read_bytes(), b'{broken json')

    def test_concurrent_observations_have_one_baseline(self):
        self.fixture.write_text(json.dumps(self.data))
        command = ['bash', str(SCRIPT), '--repo', self.data['repo'], '--pr', str(self.data['pr']),
                   '--state-dir', str(self.state)]
        processes = [subprocess.Popen(command, env=self.env, cwd=self.directory,
                                     text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        outputs = []
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=30)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stderr, '')
                outputs.append(stdout)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        self.assertEqual(sum('PRWATCH armed' in out for out in outputs), 1, outputs)
        self.assertEqual(sum('PRWATCH no change' in out for out in outputs), 3, outputs)
        self.assertEqual(json.loads(self.baseline().read_bytes())['pull']['head'], 'a' * 40)

    def test_concurrent_first_lock_creation(self):
        context = multiprocessing.get_context('spawn')
        barrier = context.Barrier(4)
        results = context.Queue()
        helper = str(SCRIPT.with_name('pr_watch.py'))
        processes = [context.Process(target=concurrent_lock_worker,
                                     args=(helper, str(self.state), barrier, results)) for _ in range(4)]
        errors = []
        try:
            for process in processes:
                process.start()
            for _ in processes:
                errors.extend(results.get(timeout=30))
            for process in processes:
                process.join(timeout=30)
                self.assertEqual(process.exitcode, 0)
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=30)
            results.close()
            results.join_thread()
        self.assertEqual(errors, [], f'concurrent first creation must succeed: {errors[:5]}')
        self.assertEqual(len(list(self.state.glob('*.lock'))), 100)

    def test_missing_gh_is_operational_error(self):
        (self.bin / 'gh').unlink()
        (self.bin / 'python3').symlink_to(sys.executable)
        self.env['PATH'] = str(self.bin)
        self.fixture.write_text(json.dumps(self.data))
        result = subprocess.run(['/bin/bash', str(SCRIPT), '--repo', self.data['repo'], '--pr', '954',
                                 '--state-dir', str(self.state)], env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('gh', result.stderr)
        self.assertNotIn('PRWATCH armed', result.stdout)

    def test_default_state_location(self):
        self.fixture.write_text(json.dumps(self.data))
        result = subprocess.run(['bash', str(SCRIPT), '--repo', self.data['repo'], '--pr', '954'],
                                env=self.env, cwd=self.directory, text=True, capture_output=True, timeout=15)
        self.assert_success(result, 'PRWATCH armed')
        self.assertEqual(len(list((self.directory / 'xdg' / 'shepherd-pr').glob('*.json'))), 1)
        self.assertFalse(self.state.exists())

    def test_missing_python_is_operational_error(self):
        self.env['PATH'] = str(self.bin)
        result = subprocess.run(['/bin/bash', str(SCRIPT), '--help'], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('python3 is required', result.stderr)

    def test_error_diagnostics_redact_authentication_secrets(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        self.env['GH_TOKEN'] = 'credential-value-from-environment'
        self.data['fail'] = 'reviews'
        self.data['error_detail'] = 'credential-value-from-environment github_pat_example123'
        result = self.run_watcher()
        self.assertEqual(result.returncode, 1)
        self.assertIn('HTTP 403', result.stderr)
        self.assertIn('[REDACTED]', result.stderr)
        for secret in ('credential-value-from-environment', 'github_pat_example123'):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertEqual(self.baseline().read_bytes(), before)

    def test_malformed_second_page_preserves_baseline(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        self.data['pages'] = {'issue_comments': [self.data['issue_comments'], {'message': 'bad page'}]}
        result = self.run_watcher()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(self.baseline().read_bytes(), before)
        self.assertNotIn('PRWATCH change detected', result.stdout)

    def test_baseline_replacement_is_atomic(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        baseline = self.baseline()
        before = baseline.read_bytes()
        with baseline.open('rb') as existing_reader:
            self.data['issue_comments'][0]['body'] = 'updated atomically'
            self.assert_success(self.run_watcher(), 'PRWATCH change detected')
            self.assertEqual(existing_reader.read(), before)
        self.assertEqual(json.loads(baseline.read_bytes())['issue_comments'][0]['body'], 'updated atomically')
        self.assertFalse(list(self.state.glob('*.tmp')))

    def test_linked_lock_is_rejected(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        locks = list(self.state.glob('*.lock'))
        self.assertEqual(len(locks), 1)
        target = self.directory / 'lock-target'
        target.write_text('do not touch')
        locks[0].unlink()
        locks[0].symlink_to(target)
        self.assertEqual(self.run_watcher().returncode, 1)
        self.assertEqual(target.read_text(), 'do not touch')
        self.assertEqual(self.baseline().read_bytes(), before)

    def test_nonprivate_baseline_is_rejected(self):
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        baseline = self.baseline()
        before = baseline.read_bytes()
        baseline.chmod(0o644)
        self.assertEqual(self.run_watcher().returncode, 1)
        self.assertEqual(baseline.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(baseline.stat().st_mode), 0o644)

    def test_all_observation_lists_ignore_order(self):
        comment = self.data['issue_comments'][0]
        self.data['review_comments'] = [dict(comment, id=1, path='one.py', commit_id='a' * 40),
                                        dict(comment, id=2, path='two.py', commit_id='a' * 40)]
        review = {'body': 'review', 'user': None, 'state': 'APPROVED',
                  'submitted_at': None, 'commit_id': 'a' * 40}
        self.data['reviews'] = [dict(review, id=3), dict(review, id=4)]
        check = {'status': 'completed', 'conclusion': 'success', 'head_sha': 'a' * 40}
        self.data['checks'] = {'total_count': 2, 'check_runs': [dict(check, id=5, name='lint'),
                                                             dict(check, id=6, name='tests')]}
        status = {'state': 'pending', 'description': None, 'target_url': None,
                  'created_at': '2026-09-01T00:00:00Z', 'updated_at': '2026-09-01T00:00:00Z'}
        self.data['statuses'] = [dict(status, id=7, context='external'), dict(status, id=8, context='build')]
        self.assert_success(self.run_watcher(), 'PRWATCH armed')
        before = self.baseline().read_bytes()
        for items in (self.data['review_comments'], self.data['reviews'],
                      self.data['checks']['check_runs'], self.data['statuses']):
            items.reverse()
            self.assert_success(self.run_watcher(), 'PRWATCH no change')
            self.assertEqual(self.baseline().read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
