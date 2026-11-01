#!/usr/bin/env python3
"""Shared read-only GitHub helpers for the shepherd-pr commands.

One module, so every command imports from the same place:

* GET-only requests through the authenticated `gh` CLI, with credential
  redaction and validated response shapes;
* repository merge-gate discovery from classic branch protection and active
  branch rulesets (required checks, approving reviews, merge queue);
* roborev combined-review parsing (reviewed commit, severity counts, verdict).

API response bodies are never treated as error text. List endpoints paginate.
"""
import json
import os
import re
import subprocess


class ReadError(Exception):
    """An operational failure reading GitHub: request, shape, or JSON."""


def parse_json(text):
    def invalid_constant(value):
        raise ValueError('non-JSON numeric constant')
    return json.loads(text, parse_constant=invalid_constant)


def diagnostic(text):
    # Never inherit gh's verbose HTTP debugging, and redact credentials if gh
    # includes them in a diagnostic. API response bodies are never error text.
    for name in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN'):
        token = os.environ.get(name)
        if token:
            text = text.replace(token, '[REDACTED]')
    text = re.sub(r'(?i)(authorization\s*:\s*)[^\r\n]+', r'\1[REDACTED]', text)
    return re.sub(r'(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+', '[REDACTED]', text).strip()


def request(endpoint, paginated=False, timeout=60, missing_ok=False):
    command = ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', endpoint]
    if paginated:
        command += ['--paginate', '--slurp']
    env = dict(os.environ, GH_DEBUG='', GH_PROMPT_DISABLED='1')
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ReadError(f'GitHub request timed out: {endpoint}') from None
    if result.returncode:
        detail = diagnostic(result.stderr)
        if missing_ok and re.search(r'\bHTTP 404\b', detail):
            return None
        raise ReadError(f'GitHub request failed: {endpoint} (gh exit {result.returncode})'
                        + (f': {detail}' if detail else ''))
    try:
        return parse_json(result.stdout)
    except (ValueError, TypeError):
        raise ReadError(f'malformed JSON from GitHub: {endpoint}') from None


def require(value, kind, label, nullable=False):
    if nullable and value is None:
        return value
    if type(value) is not kind:
        raise ReadError(f'invalid response shape: {label}')
    return value


def field(obj, key, kind, nullable=False):
    require(obj, dict, 'object')
    if key not in obj:
        raise ReadError(f'invalid response shape: missing {key}')
    return require(obj[key], kind, key, nullable)


def selected(obj, schema):
    return {key: field(obj, key, kind, nullable) for key, kind, nullable in schema}


def author(obj):
    user = field(obj, 'user', dict, nullable=True)
    return None if user is None else field(user, 'login', str)


def items(endpoint, normalize, check_runs=False):
    pages = require(request(endpoint + ('&' if '?' in endpoint else '?') + 'per_page=100',
                            paginated=True), list, 'pages')
    if not pages:
        raise ReadError('invalid response shape: no pages')
    result = []
    for page in pages:
        if check_runs:
            total = field(page, 'total_count', int)
            if total < 0:
                raise ReadError('invalid response shape: negative check total')
            page = field(page, 'check_runs', list)
        result.extend(normalize(item) for item in require(page, list, 'list page'))
    return sorted(result, key=lambda item: json.dumps(item, sort_keys=True))


# --- roborev combined review -------------------------------------------------

MARKER = '<!-- roborev-pr-comment -->'
HEADER = re.compile(
    r'^##\s+roborev:\s+(.+?)\s+\(`([0-9a-fA-F]{7,64})`\)\s*$', re.M)
SEVERITY = re.compile(r'\*\*\s*severity\s*[*:\s]*(critical|high|medium|low)', re.I)
SEVERITY_HEADING = re.compile(
    r'^#{1,6}\s+\W*(critical|high|medium|low)\b'
    r'(?:\W*(?:severity|priority|findings?|issues?))?\W*(?:\d+\W*)?$', re.I | re.M)
ANY_HEADING = re.compile(r'^#{1,6}\s', re.M)
FINDING = re.compile(r'^(?:[-*]|\d+\.)\s', re.M)
VERDICT = re.compile(r'^\s*\*\*\s*Verdict:\*\*\s*(.+?)\s*$', re.M | re.I)
SEVERITY_ORDER = ('critical', 'high', 'medium', 'low')


def same_commit(one, other):
    """A full SHA matches its own prefix; case is normalized by the caller."""
    one, other = one.lower(), other.lower()
    return one.startswith(other) or other.startswith(one)


def roborev_comments(repo, pr):
    return items(f'repos/{repo}/issues/{pr}/comments', lambda obj: {
        'id': field(obj, 'id', int),
        'body': field(obj, 'body', str, nullable=True),
        'created_at': field(obj, 'created_at', str),
        'updated_at': field(obj, 'updated_at', str),
        'user': author(obj),
    })


def severities(body):
    inline = {name: 0 for name in SEVERITY_ORDER}
    headed = {name: 0 for name in SEVERITY_ORDER}
    for match in SEVERITY.finditer(body):
        inline[match.group(1).lower()] += 1
    for match in SEVERITY_HEADING.finditer(body):
        rest = body[match.end():]
        next_heading = ANY_HEADING.search(rest)
        section = rest[:next_heading.start()] if next_heading else rest
        # A heading with no list under it still counts, so a change in how
        # findings are listed cannot hide a severity.
        headed[match.group(1).lower()] += max(1, len(FINDING.findall(section)))
    # One finding can carry both forms, so take the larger count, not the sum.
    return {name: max(inline[name], headed[name]) for name in SEVERITY_ORDER}


def verdict(body):
    """The bot's own `**Verdict:**` sentence, when the comment carries one."""
    match = VERDICT.search(body)
    return match.group(1) if match else None


def severity_text(counts):
    return ','.join(f'{name}:{counts.get(name, 0)}' for name in SEVERITY_ORDER
                    if counts.get(name)) or '-'


def review(comment_list, head):
    """Classify the latest roborev comment on a pull request against its head."""
    found = [c for c in comment_list if c['body'] and MARKER in c['body']]
    if not found:
        return {'state': 'none', 'reviewed': None, 'severities': {},
                'updated': None, 'author': None, 'body': None, 'verdict': None}
    latest = max(found, key=lambda c: c['updated_at'])
    body = latest['body']
    match = HEADER.search(body)
    if match is None:
        state, reviewed = 'unparsed', None
    else:
        kind, reviewed = match.group(1), match.group(2)
        if kind == 'Review Failed':
            state = 'review-failed'
        elif kind == 'Review Passed':
            state = 'passed' if same_commit(head, reviewed) else 'stale'
        else:
            state = 'current' if same_commit(head, reviewed) else 'stale'
    return {'state': state, 'reviewed': reviewed, 'severities': severities(body),
            'updated': latest['updated_at'], 'author': latest['user'], 'body': body,
            'verdict': verdict(body)}


# --- repository merge gates --------------------------------------------------

GATE_CACHE = {}


def _ref_matches(conditions, base, default_branch):
    """Whether a ruleset's ref_name condition covers the pull request base."""
    if not isinstance(conditions, dict):
        return True
    ref = conditions.get('ref_name')
    if not isinstance(ref, dict):
        return True
    include = ref.get('include') or []
    if not isinstance(include, list) or not include:
        return True
    for pattern in include:
        if not isinstance(pattern, str):
            continue
        if pattern == '~ALL':
            return True
        if pattern == '~DEFAULT_BRANCH' and default_branch == base:
            return True
        if pattern == f'refs/heads/{base}':
            return True
    return False


def _rulesets(repo):
    """Active branch rulesets with their rules."""
    pages = request(f'repos/{repo}/rulesets?per_page=100', paginated=True, missing_ok=True)
    if pages is None:
        return []
    require(pages, list, 'rulesets pages')
    result = []
    for page in pages:
        for summary in require(page, list, 'rulesets page'):
            require(summary, dict, 'ruleset')
            if field(summary, 'enforcement', str) != 'active':
                continue
            if field(summary, 'target', str) != 'branch':
                continue
            detail = request(f'repos/{repo}/rulesets/{field(summary, "id", int)}',
                             missing_ok=True)
            if detail is not None:
                result.append(detail)
    return result


def _default_branch(repo):
    detail = request(f'repos/{repo}', missing_ok=True)
    if detail is None:
        return None
    return field(detail, 'default_branch', str, nullable=True)


def discover_gates(repo, base):
    """Required checks, approvals and queue for `base`, cached per invocation."""
    key = (repo, base)
    if key in GATE_CACHE:
        return GATE_CACHE[key]
    result = {'base': base, 'known': False, 'required_checks': [],
              'approvals': 0, 'merge_queue': False, 'queue_name': None,
              'error': None}
    errors = []

    try:
        protection = request(f'repos/{repo}/branches/{base}/protection', missing_ok=True)
    except ReadError as error:
        protection = None
        errors.append(str(error))
    if protection is not None:
        checks = (protection.get('required_status_checks') or {}).get('contexts') or []
        result['required_checks'] = [name for name in checks if isinstance(name, str)]
        reviews = (protection.get('required_pull_request_reviews') or {})
        count = reviews.get('required_approving_review_count')
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            result['approvals'] = count

    try:
        rulesets = _rulesets(repo)
    except ReadError as error:
        rulesets = []
        errors.append(str(error))
    default = None
    for ruleset in rulesets:
        if default is None:
            default = _default_branch(repo)
        if not _ref_matches(ruleset.get('conditions'), base, default):
            continue
        for rule in field(ruleset, 'rules', list):
            require(rule, dict, 'ruleset rule')
            kind = field(rule, 'type', str)
            parameters = rule.get('parameters') or {}
            if kind == 'required_status_checks':
                for check in parameters.get('required_status_checks') or []:
                    if isinstance(check, dict) and isinstance(check.get('context'), str):
                        result['required_checks'].append(check['context'])
            elif kind == 'pull_request':
                count = parameters.get('required_approving_review_count')
                if isinstance(count, int) and not isinstance(count, bool):
                    result['approvals'] = max(result['approvals'], count)
            elif kind == 'merge_queue':
                result['merge_queue'] = True
                result['queue_name'] = field(ruleset, 'name', str, nullable=True)

    result['required_checks'] = sorted(set(result['required_checks']))
    # A fully readable repo reports its real gates, even when there are none.
    result['known'] = not errors
    if errors:
        result['error'] = errors[0]
    GATE_CACHE[key] = result
    return result


def describe_gates(gates, roborev):
    """One compact clause naming the gates an agent must not miss."""
    if gates['known']:
        state = 'known'
    elif gates['error']:
        state = 'unknown (' + diagnostic(gates['error']).split(':')[0] + ')'
    else:
        state = 'unknown'
    checks = ','.join(gates['required_checks']) or '-'
    return (f'gates={state} base={gates["base"]} required={checks} '
            f'approvals={gates["approvals"]} '
            f'queue={"yes" if gates["merge_queue"] else "no"} '
            f'roborev={"yes" if roborev else "no"}')
