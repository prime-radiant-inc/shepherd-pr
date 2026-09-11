#!/usr/bin/env python3
"""Read-only triage of open pull requests left without an update.

Lists open pull requests whose last update is older than a cutoff, oldest
first, and reports each one's roborev combined-review state so an agent can
pick the ones needing attention. Read-only toward GitHub: no writes occur.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from github_read import ReadError, field, items  # noqa: E402
from roborev_review import (SEVERITY_ORDER, comments as roborev_comments, review,  # noqa: E402
                            severity_text, verdict)

MAX_HOURS = 8760

HELP = """One read-only observation of stale open GitHub pull requests.

Dependencies: bash, gh (authenticated GitHub CLI with --paginate/--slurp),
python3 (standard library), on macOS or Linux.

Lists open pull requests whose most recent update is older than --hours
(default 6), oldest first, excluding drafts unless --include-drafts is given.
For each, it reads the roborev combined-review comment and reports whether the
reviewed commit is still the head (current), the head moved after review
(stale), roborev could not complete a review (review-failed), a roborev comment
exists without a recognized header (unparsed), or no roborev comment exists
(none). The review-hint column counts `**Severity**` markers, or, when there are
none, shows the bot's own `**Verdict:**` sentence. Both are hints; read the
review body before acting.

Text output prints a STALEPR header line then one tab-separated row per pull
request: number, updated_at, review state, reviewed commit, head commit,
mergeable state, draft, review hint, title. --json prints a single JSON object.

Exit statuses: 0 successful observation, 1 operational failure, 2 usage error.
"""


class Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        if getattr(namespace, self.dest) is not None:
            parser.error(f'{option_string} may only be supplied once')
        setattr(namespace, self.dest, values)


def arguments():
    parser = argparse.ArgumentParser(
        description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter,
        usage='%(prog)s --repo OWNER/REPO [--hours N] [--include-drafts] [--json]',
        prog='stale-prs.sh', allow_abbrev=False)
    parser.add_argument('--repo', required=True, action=Once, metavar='OWNER/REPO')
    parser.add_argument('--hours', action=Once, metavar='N')
    parser.add_argument('--include-drafts', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+', args.repo):
        parser.error('--repo must be OWNER/REPO, not a URL or path')
    if args.repo.split('/')[1] in ('.', '..'):
        parser.error('--repo must name a repository')
    if args.hours is None:
        args.hours = 6
    elif not re.fullmatch(r'[0-9]+', args.hours) or not 1 <= int(args.hours) <= MAX_HOURS:
        parser.error(f'--hours must be an integer between 1 and {MAX_HOURS}')
    else:
        args.hours = int(args.hours)
    # GitHub repository identity is case insensitive.
    args.repo = args.repo.lower()
    return args


def pull(obj):
    entry = {
        'number': field(obj, 'number', int),
        'title': field(obj, 'title', str),
        'draft': field(obj, 'draft', bool),
        'updated_at': field(obj, 'updated_at', str),
        'head': field(field(obj, 'head', dict), 'sha', str),
    }
    if not re.fullmatch(r'(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})', entry['head']):
        raise ReadError('invalid response: pull request head SHA')
    state = obj.get('mergeable_state')
    if state is not None and type(state) is not str:
        raise ReadError('invalid response shape: mergeable_state')
    entry['mergeable_state'] = state
    return entry


def stale_pull_requests(repo, hours, include_drafts):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    pulls = items(f'repos/{repo}/pulls?state=open', pull)
    stale = sorted((p for p in pulls
                    if p['updated_at'] < cutoff and (include_drafts or not p['draft'])),
                   key=lambda p: p['updated_at'])
    rows = []
    for entry in stale:
        outcome = review(roborev_comments(repo, entry['number']), entry['head'])
        rows.append({
            'number': entry['number'],
            'updated_at': entry['updated_at'],
            'review_state': outcome['state'],
            'reviewed': outcome['reviewed'],
            'head': entry['head'],
            'mergeable_state': entry['mergeable_state'],
            'draft': entry['draft'],
            'severities': {name: outcome['severities'].get(name, 0) for name in SEVERITY_ORDER},
            'verdict': outcome['verdict'],
            'title': entry['title'],
        })
    return cutoff, rows


def review_hint(counts, verdict_text):
    text = severity_text(counts)
    if text != '-':
        return text
    return f'verdict:{verdict_text[:200]}' if verdict_text else '-'


def text_output(repo, hours, cutoff, rows):
    lines = [f'STALEPR repo={repo} hours={hours} cutoff={cutoff} count={len(rows)}']
    for row in rows:
        counts = {name: row['severities'][name] for name in SEVERITY_ORDER}
        columns = [
            str(row['number']), row['updated_at'], row['review_state'],
            row['reviewed'] or '-', row['head'], row['mergeable_state'] or '-',
            'true' if row['draft'] else 'false', review_hint(counts, row['verdict']),
            row['title'].replace('\t', ' ').replace('\n', ' '),
        ]
        lines.append('\t'.join(columns))
    return '\n'.join(lines) + '\n'


def json_output(repo, hours, cutoff, rows):
    return json.dumps({'repo': repo, 'hours': hours, 'cutoff': cutoff,
                       'count': len(rows), 'pull_requests': rows},
                      indent=2, sort_keys=True) + '\n'


def main():
    args = arguments()
    try:
        if not shutil.which('gh'):
            raise ReadError('gh is required; install and authenticate the GitHub CLI')
        cutoff, rows = stale_pull_requests(args.repo, args.hours, args.include_drafts)
        output = (json_output if args.json else text_output)(args.repo, args.hours, cutoff, rows)
        print(output, end='')
        return 0
    except (ReadError, OSError, UnicodeError) as error:
        print(f'STALEPR error: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
