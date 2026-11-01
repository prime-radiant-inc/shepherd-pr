#!/usr/bin/env python3
"""Repository merge-gate discovery shared by the observation tools.

Answers "what does this repository require before a merge?" from the branch's
classic protection and from active branch rulesets: required status checks,
required approving reviews, and a merge queue. Read-only: every call is a GET.

The result is best-effort. A token without admin access, an enterprise setup
with unreadable rulesets, or a transient API failure yields `known: False` with
the diagnostic, never a failed observation: gate discovery must not stop a
waiter from watching CI.
"""
import re

from github_read import ReadError, diagnostic, field, request, require

CACHE = {}


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
    """Active branch rulesets with their rules, or None when unreadable."""
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


def discover(repo, base):
    """Required checks, approvals and queue for `base`, cached per invocation."""
    key = (repo, base)
    if key in CACHE:
        return CACHE[key]
    result = {'base': base, 'known': False, 'required_checks': [],
              'approvals': 0, 'merge_queue': False, 'queue_name': None,
              'error': None}
    errors = []

    try:
        protection = request(f'repos/{repo}/branches/{base}/protection', missing_ok=True)
    except ReadError as error:
        protection = None
        errors.append(str(error))
    # A 404 means the branch is unprotected: absence is a real answer.
    if protection is not None:
        result['known'] = True
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
        result['known'] = True
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
    CACHE[key] = result
    return result


def describe(gates, roborev):
    """One compact clause naming the gates an agent must not miss."""
    if not gates['known']:
        state = 'unknown' if not gates['error'] else \
            'unknown (' + diagnostic(gates['error']).split(':')[0] + ')'
    else:
        state = 'known'
    checks = ','.join(gates['required_checks']) or '-'
    return (f'gates={state} base={gates["base"]} required={checks} '
            f'approvals={gates["approvals"]} '
            f'queue={"yes" if gates["merge_queue"] else "no"} '
            f'roborev={"yes" if roborev else "no"}')
