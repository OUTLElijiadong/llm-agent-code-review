#!/usr/bin/env python3
"""Classify migration 121 MySQL schema with a strict parsed CHECK expression."""
from __future__ import annotations

import json
import re
import sys

EXPECTED = """(model_search_calls_evidence IS NULL AND model_fetch_calls_evidence IS NULL) OR
(model_search_calls_evidence IS NOT NULL AND model_fetch_calls_evidence IS NOT NULL
 AND model_search_calls_evidence = 0 AND model_fetch_calls_evidence = 0
 AND decision IN ('proposed', 'approved', 'rejected') AND resolution = 'settle')"""
TOKEN = re.compile(r"\s*(?:(\b[a-z_][a-z_0-9]*\b)|('(?:[^']|'')*')|(\d+)|([(),=]))", re.I)


def parse_check(expression: str):
    expression = re.sub(r"_[a-z0-9]+(?=')", "", expression.lower().replace('`', ''))
    tokens = []
    while expression.strip():
        match = TOKEN.match(expression)
        if not match:
            raise ValueError("unsupported CHECK token")
        tokens.append(next(value for value in match.groups() if value is not None))
        expression = expression[match.end():]
    index = 0

    def take(value=None):
        nonlocal index
        if index >= len(tokens) or (value is not None and tokens[index] != value):
            raise ValueError("invalid CHECK expression")
        result = tokens[index]
        index += 1
        return result

    def group(operator, terms):
        flattened = []
        for term in terms:
            if term[0] == operator:
                flattened.extend(term[1])
            else:
                flattened.append(term)
        return (operator, tuple(sorted(flattened, key=repr))) if len(flattened) > 1 else flattened[0]

    def atom():
        if index < len(tokens) and tokens[index] == '(':
            take('(')
            value = expr()
            take(')')
            return value
        left = take()
        op = take()
        if op == 'is':
            negate = index < len(tokens) and tokens[index] == 'not'
            if negate:
                take('not')
            take('null')
            return ('is-not-null' if negate else 'is-null', left)
        if op == '=':
            return ('eq', left, take())
        if op == 'in':
            take('(')
            values = [take()]
            while index < len(tokens) and tokens[index] == ',':
                take(',')
                values.append(take())
            take(')')
            return ('in', left, tuple(sorted(values)))
        raise ValueError("unsupported CHECK operator")

    def conjunction():
        terms = [atom()]
        while index < len(tokens) and tokens[index] == 'and':
            take('and')
            terms.append(atom())
        return group('and', terms)

    def expr():
        terms = [conjunction()]
        while index < len(tokens) and tokens[index] == 'or':
            take('or')
            terms.append(conjunction())
        return group('or', terms)

    result = expr()
    if index != len(tokens):
        raise ValueError("extra CHECK tokens")
    return result


def classify(snapshot):
    if snapshot.get('table_count') != 1:
        return 'partial'
    columns = snapshot.get('columns')
    checks = snapshot.get('checks')
    if not isinstance(columns, list) or not isinstance(checks, list):
        return 'partial'
    if not columns and not checks:
        return 'absent'
    expected_names = {'model_search_calls_evidence', 'model_fetch_calls_evidence'}
    if (len(columns) != 2 or {c.get('name') for c in columns} != expected_names
            or any(c.get('type', '').lower() != 'bigint' or c.get('nullable') != 'YES'
                   or c.get('default') is not None or c.get('extra') != '' for c in columns)
            or len(checks) != 1 or checks[0].get('enforced') != 'YES'):
        return 'partial'
    try:
        return 'complete' if parse_check(checks[0]['clause']) == parse_check(EXPECTED) else 'partial'
    except (ValueError, KeyError, TypeError):
        return 'partial'


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--fixture']:
            print(json.dumps({
                'table_count': 1,
                'columns': [{'name': name, 'type': 'bigint', 'nullable': 'YES', 'default': None, 'extra': ''}
                            for name in ('model_search_calls_evidence', 'model_fetch_calls_evidence')],
                'checks': [{'enforced': 'YES', 'clause': EXPECTED}],
            }))
        elif len(sys.argv) == 1:
            print(classify(json.load(sys.stdin)))
        else:
            sys.exit(2)
    except (ValueError, TypeError):
        sys.exit(1)
