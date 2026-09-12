"""Offline CLI tests for the roborev combined-review reader."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get('ROBOREV_TEST_SCRIPT',
                             ROOT / 'skills/shepherd-pr/references/roborev-review.sh'))

GH_FIXTURE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
fixture = json.load(open(os.environ['GH_FIXTURE']))
endpoint = next((a for a in args if a.startswith('repos/')), '')
endpoint = endpoint.split('?')[0]
prefix = 'repos/' + fixture['repo']
pr = str(fixture['pr'])
routes = {
    prefix + '/pulls/' + pr: 'pull',
    prefix + '/issues/' + pr + '/comments': 'issue_comments',
}
key = routes.get(endpoint)
if key is None:
    print('fixture: unknown endpoint', file=sys.stderr)
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
print(json.dumps(payload))
'''

MARKER = '<!-- roborev-pr-comment -->'
HEAD = 'a' * 40


def roborev_body(kind='Combined Review', sha=HEAD[:12], extra=''):
    return f'{MARKER}\n## roborev: {kind} (`{sha}`)\n\nfindings here\n{extra}'


def observation():
    return {
        'repo': 'prime-radiant-inc/evener', 'pr': 954,
        'pull': {'number': 954, 'head': {'sha': HEAD}},
        'issue_comments': [],
    }


def comment(body, updated='2026-09-01T00:00:00Z', login='roborev-primeradiant[bot]'):
    return {'id': 7, 'body': body, 'user': {'login': login},
            'created_at': updated, 'updated_at': updated}


class RoborevReaderTests(unittest.TestCase):
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

    def run_reader(self, *args):
        self.fixture.write_text(json.dumps(self.data))
        return subprocess.run(['bash', str(SCRIPT), '--repo', self.data['repo'],
                               '--pr', str(self.data['pr']), *args],
                              env=self.env, cwd=self.directory, text=True,
                              capture_output=True, timeout=15)

    def summary(self, result):
        return result.stdout.splitlines()[0]

    def assert_success(self, result, state):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        self.assertIn(f'state={state}', self.summary(result))

    def test_help_and_usage(self):
        help_result = subprocess.run(['bash', str(SCRIPT), '--help'], env=self.env,
                                     text=True, capture_output=True, timeout=15)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        for term in ('--repo OWNER/REPO', '--pr NUMBER', 'python3', 'gh',
                     'none', 'current', 'stale', 'review-failed', 'unparsed', 'exit'):
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

    def test_no_roborev_comment(self):
        self.data['issue_comments'] = [comment('a normal human comment')]
        result = self.run_reader()
        self.assert_success(result, 'none')
        self.assertIn(f'head={HEAD}', self.summary(result))
        self.assertIn('reviewed=-', self.summary(result))
        self.assertNotIn('## roborev', result.stdout)

    def test_current_review_prints_body(self):
        self.data['issue_comments'] = [comment(roborev_body(extra='\n**Severity**: Low\n'))]
        result = self.run_reader()
        self.assert_success(result, 'current')
        self.assertIn(f'reviewed={HEAD[:12]}', self.summary(result))
        self.assertIn('findings here', result.stdout)

    def test_review_passed(self):
        self.data['issue_comments'] = [comment(roborev_body(kind='Review Passed'))]
        result = self.run_reader()
        self.assert_success(result, 'passed')
        self.assertIn(f'reviewed={HEAD[:12]}', self.summary(result))
        self.assertIn('findings here', result.stdout)

    def test_stale_review(self):
        self.data['issue_comments'] = [comment(roborev_body(sha='b' * 12))]
        result = self.run_reader()
        self.assert_success(result, 'stale')
        self.assertIn('reviewed=bbbbbbbbbbbb', self.summary(result))

    def test_review_failed(self):
        self.data['issue_comments'] = [comment(roborev_body(kind='Review Failed'))]
        result = self.run_reader()
        self.assert_success(result, 'review-failed')
        self.assertIn(f'reviewed={HEAD[:12]}', self.summary(result))

    def test_unparsed_header_is_reported_with_body(self):
        self.data['issue_comments'] = [comment(f'{MARKER}\nroborev ran but said nothing useful\n')]
        result = self.run_reader()
        self.assert_success(result, 'unparsed')
        self.assertIn('said nothing useful', result.stdout)

    def test_latest_roborev_comment_wins(self):
        self.data['issue_comments'] = [
            comment(roborev_body(sha='b' * 12), updated='2026-09-01T00:00:00Z'),
            comment(roborev_body(sha=HEAD[:12]), updated='2026-09-02T00:00:00Z'),
        ]
        result = self.run_reader()
        self.assert_success(result, 'current')

    def test_non_marker_comment_is_ignored(self):
        header = f'## roborev: Combined Review (`{HEAD[:12]}`)\n'
        self.data['issue_comments'] = [comment(header)]
        result = self.run_reader()
        self.assert_success(result, 'none')

    def test_severity_markers_are_counted(self):
        body = roborev_body(extra='\n**Severity**: Medium\n- **Severity:** Low\n')
        self.data['issue_comments'] = [comment(body)]
        result = self.run_reader()
        self.assert_success(result, 'current')
        self.assertIn('severities=medium:1,low:1', self.summary(result))

    def test_verdict_line_is_reported(self):
        body = roborev_body(extra='\n**Verdict:** Jesse, this is sound.\n')
        self.data['issue_comments'] = [comment(body)]
        result = self.run_reader()
        self.assertIn('verdict=Jesse, this is sound.', self.summary(result))

    def test_no_severity_markers_is_dash(self):
        self.data['issue_comments'] = [comment(roborev_body())]
        result = self.run_reader()
        self.assertIn('severities=-', self.summary(result))

    def test_pagination_reaches_second_page(self):
        self.data['pages'] = {'issue_comments': [[], [comment(roborev_body())]]}
        result = self.run_reader()
        self.assert_success(result, 'current')

    def test_failed_request_is_operational_error(self):
        self.data['issue_comments'] = [comment(roborev_body())]
        self.data['fail'] = 'issue_comments'
        result = self.run_reader()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')
        self.assertIn('error', result.stderr.lower())

    def test_failed_pull_request_is_operational_error(self):
        self.data['fail'] = 'pull'
        result = self.run_reader()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')

    def test_malformed_json_is_operational_error(self):
        self.data['malformed'] = 'issue_comments'
        result = self.run_reader()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')

    def test_invalid_head_sha_is_operational_error(self):
        for sha in ('a' * 41, None, 7):
            with self.subTest(sha=sha):
                self.data['pull']['head']['sha'] = sha
                result = self.run_reader()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertEqual(result.stdout, '')

    def test_wrong_pull_number_is_operational_error(self):
        self.data['pull']['number'] = 955
        result = self.run_reader()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, '')

    def test_error_diagnostics_redact_authentication_secrets(self):
        self.env['GH_TOKEN'] = 'credential-value-from-environment'
        self.data['fail'] = 'issue_comments'
        self.data['error_detail'] = 'credential-value-from-environment github_pat_example123'
        result = self.run_reader()
        self.assertEqual(result.returncode, 1)
        self.assertIn('HTTP 403', result.stderr)
        self.assertIn('[REDACTED]', result.stderr)
        for secret in ('credential-value-from-environment', 'github_pat_example123'):
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_missing_python_is_operational_error(self):
        self.env['PATH'] = str(self.bin)
        result = subprocess.run(['/bin/bash', str(SCRIPT), '--help'], env=self.env,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn('python3 is required', result.stderr)


if __name__ == '__main__':
    unittest.main()
