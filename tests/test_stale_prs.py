"""Offline CLI tests for the stale pull-request triage tool."""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('STALEPR_TEST_SCRIPT',
                             ROOT / 'skills/shepherd-pr/references/stale-prs.sh'))

GH_FIXTURE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
fixture = json.load(open(os.environ['GH_FIXTURE']))
endpoint = next((a for a in args if a.startswith('repos/')), '')
endpoint = endpoint.split('?')[0]
prefix = 'repos/' + fixture['repo']
if endpoint == prefix + '/pulls':
    key, payload = 'pulls', fixture['pulls']
elif endpoint.startswith(prefix + '/issues/') and endpoint.endswith('/comments'):
    number = endpoint[len(prefix) + len('/issues/'):-len('/comments')]
    key, payload = 'comments:' + number, fixture['comments'].get(number, [])
else:
    print('fixture: unknown endpoint ' + endpoint, file=sys.stderr)
    sys.exit(4)
if fixture.get('fail') == key:
    print('fixture: request denied (HTTP 403) ' + fixture.get('error_detail', ''), file=sys.stderr)
    sys.exit(1)
if fixture.get('malformed') == key:
    print('{not-json')
    sys.exit(0)
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
NOW = datetime.now(timezone.utc)


def ts(hours):
    return (NOW + timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')


def head_sha(number):
    return f'{number:040d}'


def pull(number, updated, draft=False, title='a change', mergeable_state='dirty'):
    return {'id': number, 'number': number, 'title': title, 'draft': draft,
            'updated_at': updated, 'head': {'sha': head_sha(number)},
            'mergeable_state': mergeable_state}


def review_comment(body, updated='2026-01-01T00:00:00Z', login='roborev-primeradiant[bot]'):
    return {'id': 7, 'body': body, 'user': {'login': login},
            'created_at': updated, 'updated_at': updated}


def review_body(kind='Combined Review', sha='0' * 12, extra=''):
    return f'{MARKER}\n## roborev: {kind} (`{sha}`)\n\n{extra}'


class StalePrTests(unittest.TestCase):
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
        self.data = {'repo': 'prime-radiant-inc/evener', 'pulls': [], 'comments': {}}
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'],
                        GH_FIXTURE=str(self.fixture), HOME=str(self.directory / 'home'))
        self.add_pull(pull(593, ts(-10)))

    def add_pull(self, item):
        self.data['pulls'].append(item)

    def run_tool(self, *args):
        self.fixture.write_text(json.dumps(self.data))
        return subprocess.run(['bash', str(SCRIPT), '--repo', self.data['repo'], *args],
                              env=self.env, cwd=self.directory, text=True,
                              capture_output=True, timeout=15)

    def test_help_and_usage(self):
        help_result = subprocess.run(['bash', str(SCRIPT), '--help'], env=self.env,
                                     text=True, capture_output=True, timeout=15)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for term in ('--repo OWNER/REPO', '--hours', '--include-drafts', '--json',
                     'python3', 'gh', 'current', 'stale', 'review-failed', 'none', 'exit'):
            self.assertIn(term, help_result.stdout)
        for args in (['--repo', 'owner/repo', '--hours', '0'],
                     ['--repo', 'owner/repo', '--hours', '-1'],
                     ['--repo', 'owner/repo', '--hours', 'x'],
                     ['--repo', 'owner/repo', '--hours', '99999'],
                     ['--repo', '../repo', '--hours', '6'],
                     ['--repo', 'a/b', '--hours', '6', '--unknown']):
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(SCRIPT), *args], env=self.env,
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn('usage', result.stderr.lower())
        for args in ([], ['--hours', '6']):
            with self.subTest(args=args):
                result = subprocess.run(['bash', str(SCRIPT), *args], env=self.env,
                                        text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_filters_fresh_and_reports_header(self):
        self.add_pull(pull(600, ts(-1)))
        result = self.run_tool('--hours', '6')
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(lines[0].startswith('STALEPR '), lines[0])
        self.assertIn('count=1', lines[0])
        self.assertIn('cutoff=', lines[0])
        self.assertEqual(len(lines), 2, result.stdout)
        self.assertTrue(lines[1].startswith('593\t'), lines[1])

    def test_drafts_excluded_by_default_and_included_on_request(self):
        self.add_pull(pull(601, ts(-9), draft=True))
        default = self.run_tool('--hours', '6')
        self.assertNotIn('601', default.stdout)
        self.assertIn('count=1', default.stdout.splitlines()[0])
        included = self.run_tool('--hours', '6', '--include-drafts')
        self.assertIn('601', included.stdout)
        self.assertIn('count=2', included.stdout.splitlines()[0])

    def test_oldest_first(self):
        self.add_pull(pull(500, ts(-20)))
        self.add_pull(pull(700, ts(-7)))
        result = self.run_tool('--hours', '6')
        numbers = [line.split('\t')[0] for line in result.stdout.splitlines()[1:]]
        self.assertEqual(numbers, ['500', '593', '700'])

    def test_review_states(self):
        self.data['comments'] = {
            '593': [review_comment(review_body(sha=head_sha(593)[:12]))],
            '500': [],
        }
        self.add_pull(pull(500, ts(-20)))
        self.add_pull(pull(700, ts(-7)))
        self.add_pull(pull(800, ts(-8)))
        self.data['comments']['700'] = [review_comment(review_body(kind='Review Failed',
                                                                   sha=head_sha(700)[:12]))]
        self.data['comments']['800'] = [review_comment(review_body(kind='Review Passed',
                                                                   sha=head_sha(800)[:12]))]
        result = self.run_tool('--hours', '6')
        states = {line.split('\t')[0]: line.split('\t')[2]
                  for line in result.stdout.splitlines()[1:]}
        self.assertEqual(states, {'500': 'none', '593': 'current', '700': 'review-failed',
                                  '800': 'passed'})

    def test_stale_review_state(self):
        self.data['comments']['593'] = [review_comment(review_body(sha='b' * 12))]
        result = self.run_tool('--hours', '6')
        row = result.stdout.splitlines()[1].split('\t')
        self.assertEqual(row[2], 'stale')
        self.assertEqual(row[3], 'b' * 12)

    def test_severity_column(self):
        self.data['comments']['593'] = [review_comment(
            review_body(sha=head_sha(593)[:12], extra='**Severity**: Medium\n- **Severity:** Low\n'))]
        result = self.run_tool('--hours', '6')
        self.assertIn('medium:1,low:1', result.stdout.splitlines()[1])

    def test_verdict_fallback_when_no_severity_markers(self):
        self.data['comments']['593'] = [review_comment(
            review_body(sha=head_sha(593)[:12],
                        extra='**Verdict:** One medium-severity regression found.\n'))]
        result = self.run_tool('--hours', '6')
        self.assertEqual(result.stdout.splitlines()[1].split('\t')[7],
                         'verdict:One medium-severity regression found.')

    def test_json_output(self):
        self.add_pull(pull(600, ts(-1)))
        result = self.run_tool('--hours', '6', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['repo'], 'prime-radiant-inc/evener')
        self.assertEqual(payload['hours'], 6)
        self.assertEqual(payload['count'], 1)
        entry = payload['pull_requests'][0]
        self.assertEqual(entry['number'], 593)
        self.assertEqual(entry['review_state'], 'none')
        self.assertIn('head', entry)
        self.assertIn('severities', entry)

    def test_no_stale_prs(self):
        self.data['pulls'] = [pull(600, ts(-1))]
        result = self.run_tool('--hours', '6')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertIn('count=0', result.stdout)

    def test_pagination_of_pulls_and_comments(self):
        self.data['pulls'] = []
        self.add_pull(pull(800, ts(-30)))
        self.data['comments']['800'] = [review_comment(review_body(sha=head_sha(800)[:12]))]
        self.data['pages'] = {
            'pulls': [[pull(593, ts(-10))], self.data['pulls']],
            'comments:800': [[], self.data['comments']['800']],
            'comments:593': [[]],
        }
        result = self.run_tool('--hours', '6')
        self.assertIn('count=2', result.stdout.splitlines()[0])
        numbers = {line.split('\t')[0] for line in result.stdout.splitlines()[1:]}
        self.assertEqual(numbers, {'593', '800'})

    def test_failed_pulls_request_is_operational_error(self):
        self.data['fail'] = 'pulls'
        result = self.run_tool('--hours', '6')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')
        self.assertIn('error', result.stderr.lower())

    def test_failed_comments_request_is_operational_error(self):
        self.data['fail'] = 'comments:593'
        result = self.run_tool('--hours', '6')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')

    def test_malformed_json_is_operational_error(self):
        self.data['malformed'] = 'pulls'
        result = self.run_tool('--hours', '6')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')

    def test_invalid_pull_shape_is_operational_error(self):
        self.data['pulls'] = [dict(pull(593, ts(-10)), head={})]
        result = self.run_tool('--hours', '6')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')

    def test_error_diagnostics_redact_secrets(self):
        self.env['GH_TOKEN'] = 'credential-value-from-environment'
        self.data['fail'] = 'pulls'
        self.data['error_detail'] = 'credential-value-from-environment github_pat_example123'
        result = self.run_tool('--hours', '6')
        self.assertEqual(result.returncode, 1)
        for secret in ('credential-value-from-environment', 'github_pat_example123'):
            self.assertNotIn(secret, result.stdout + result.stderr)
        self.assertIn('[REDACTED]', result.stderr)

    def test_missing_python_is_operational_error(self):
        self.env['PATH'] = str(self.bin)
        result = subprocess.run(['/bin/bash', str(SCRIPT), '--help'], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('python3 is required', result.stderr)


if __name__ == '__main__':
    unittest.main()
