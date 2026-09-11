#!/usr/bin/env python3
"""Shared read-only GitHub helpers for the shepherd-pr reference tools.

Every request is a GET through the authenticated `gh` CLI. Diagnostics redact
credentials; API response bodies are never treated as error text. JSON shapes
are validated before use, and list endpoints are paginated.
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


def request(endpoint, paginated=False, timeout=60):
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
