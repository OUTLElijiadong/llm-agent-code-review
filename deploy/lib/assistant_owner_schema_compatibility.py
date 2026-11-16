#!/usr/bin/env python3
"""Strictly classify the MySQL 122 ownership foundation, without inspecting data."""
from __future__ import annotations

import json
import re
import sys

RUN = 'enterprise_assistant_run_owner'
TOOL = 'enterprise_assistant_tool_owner'
MODEL = 'enterprise_assistant_model_attempt_owner'
TABLES = {RUN, TOOL, MODEL}

# column_type, nullable, extra. No implicit coercion, generated columns or defaults.
BASE = {'id': ('bigint', False, 'auto_increment'), 'assistant_run_id': ('bigint', False, ''),
        'generation': ('int', False, ''), 'owner_digest': ('varbinary(32)', False, ''),
        'created_at': ('datetime', False, '')}
COMMON = {'run_owner_id': ('bigint', False, ''), 'enterprise_id': ('bigint', False, ''),
          'conversation_id': ('bigint', False, ''), 'initiator_user_id': ('bigint', False, ''),
          'billing_group_id': ('bigint', True, ''), 'wallet_id': ('bigint', False, ''),
          'request_digest': ('varbinary(32)', False, ''), 'request_bytes': ('blob', False, '')}
COLUMNS = {
    RUN: BASE,
    TOOL: {**BASE, **COMMON, 'tool_name': ('varchar(16)', False, ''), 'call_digest': ('varbinary(32)', False, ''),
           'call_bytes': ('blob', False, ''), 'arguments_digest': ('varbinary(32)', False, '')},
    MODEL: {**BASE, **COMMON, 'reservation_id': ('bigint', False, ''), 'payer_user_id': ('bigint', True, ''),
            'usage_kind': ('varchar(20)', False, ''), 'model_name': ('varchar(80)', False, ''),
            'round_digest': ('varbinary(32)', False, ''), 'round_bytes': ('blob', False, ''),
            'payload_digest': ('varbinary(32)', False, ''), 'billing_digest': ('varbinary(32)', False, '')},
}
UNIQUE = {
    RUN: {'PRIMARY': ('id',), 'uq_ent_run_owner_generation': ('assistant_run_id', 'generation')},
    TOOL: {'PRIMARY': ('id',), 'uq_ent_tool_owner_call': ('assistant_run_id', 'call_digest'),
           'uq_ent_tool_owner_request': ('request_digest',)},
    MODEL: {'PRIMARY': ('id',), 'uq_ent_model_owner_round': ('assistant_run_id', 'round_digest'),
            'uq_ent_model_owner_reservation': ('reservation_id',), 'uq_ent_model_owner_request': ('request_digest',)},
}
FKS = {
    RUN: {('assistant_run_id', 'enterprise_assistant_run', 'id')},
    TOOL: {('assistant_run_id', 'enterprise_assistant_run', 'id'), ('run_owner_id', RUN, 'id'),
           ('wallet_id', 'billing_wallet', 'id')},
    MODEL: {('assistant_run_id', 'enterprise_assistant_run', 'id'), ('run_owner_id', RUN, 'id'),
            ('reservation_id', 'billing_reservation', 'id'), ('wallet_id', 'billing_wallet', 'id')},
}
CHECKS = {
    RUN: {'ck_ent_run_owner_generation': 'generation > 0', 'ck_ent_run_owner_digest': 'length(owner_digest) = 32'},
    TOOL: {'ck_ent_tool_owner_generation': 'generation > 0', 'ck_ent_tool_owner_kind': "tool_name IN ('search', 'fetch')",
           'ck_ent_tool_owner_digests': 'length(owner_digest)=32 AND length(call_digest)=32 AND '
                                        'length(request_digest)=32 AND length(arguments_digest)=32'},
    MODEL: {'ck_ent_model_owner_generation': 'generation > 0',
            'ck_ent_model_owner_payer': "(usage_kind='member' AND payer_user_id IS NOT NULL AND "
                                        "payer_user_id=initiator_user_id) OR "
                                        "(usage_kind='enterprise_auto' AND payer_user_id IS NULL)",
            'ck_ent_model_owner_digests': 'length(owner_digest)=32 AND length(round_digest)=32 AND '
                                         'length(request_digest)=32 AND length(payload_digest)=32 AND '
                                         'length(billing_digest)=32'},
}
TRIGGERS = {'trg_ent_model_owner_no_update': 'UPDATE', 'trg_ent_model_owner_no_delete': 'DELETE'}
TOKEN = re.compile(r"\s*(?:(`[a-z_][a-z_0-9]*`|\b[a-z_][a-z_0-9]*\b)|('(?:[^']|'')*')|(\d+)|([(),=>]))", re.I)


def parse_check(value):
    if not isinstance(value, str):
        raise ValueError('invalid constraint')
    if '\\' in value:
        # MySQL 8.0.46 can escape literal delimiters in information_schema.
        value = re.sub(r"_utf8mb4\\'(search|fetch|member|enterprise_auto)\\'", r"'\1'", value, flags=re.I)
        if '\\' in value:
            raise ValueError('unexpected escape')
    value = re.sub(r"_utf8mb4(?=')", '', value, flags=re.I)
    tokens = []
    while value.strip():
        match = TOKEN.match(value)
        if not match:
            raise ValueError('unsupported token')
        token = next(part for part in match.groups() if part is not None)
        if token.startswith('`'):
            token = token[1:-1].lower()
        elif not token.startswith("'"):
            token = token.lower()
        tokens.append(token)
        value = value[match.end():]
    at = 0

    def take(expected=None):
        nonlocal at
        if at >= len(tokens) or (expected is not None and tokens[at] != expected):
            raise ValueError('invalid expression')
        result = tokens[at]
        at += 1
        return result

    def combine(op, values):
        flat = []
        for item in values:
            flat.extend(item[1] if item[0] == op else [item])
        return (op, tuple(sorted(flat, key=repr))) if len(flat) > 1 else flat[0]

    def atom():
        if at < len(tokens) and tokens[at] == '(':
            take('('); item = expression(); take(')'); return item
        left = take()
        if left == 'length':
            take('('); left = ('length', take()); take(')')
        op = take()
        if op == 'is':
            negated = at < len(tokens) and tokens[at] == 'not'
            if negated: take('not')
            take('null')
            return ('is-not-null' if negated else 'is-null', left)
        if op in ('=', '>'):
            return (op, left, take())
        if op == 'in':
            take('('); values = [take()]
            while at < len(tokens) and tokens[at] == ',':
                take(','); values.append(take())
            take(')')
            return ('in', left, tuple(sorted(values)))
        raise ValueError('unsupported operator')

    def conjunction():
        items = [atom()]
        while at < len(tokens) and tokens[at] == 'and':
            take('and'); items.append(atom())
        return combine('and', items)

    def expression():
        items = [conjunction()]
        while at < len(tokens) and tokens[at] == 'or':
            take('or'); items.append(conjunction())
        return combine('or', items)

    result = expression()
    if at != len(tokens):
        raise ValueError('extra tokens')
    return result


def classify(snapshot):
    try:
        tables = snapshot['tables']
        columns = snapshot['columns']
        indexes = snapshot['indexes']
        foreign_keys = snapshot['foreign_keys']
        checks = snapshot['checks']
        triggers = snapshot['triggers']
        if any(not isinstance(item, list) for item in (tables, columns, indexes, foreign_keys, checks, triggers)):
            return 'partial'
        if not any((tables, columns, indexes, foreign_keys, checks, triggers)):
            return 'absent'
        if len(tables) != 3 or {x['name'] for x in tables} != TABLES or any(
            x['type'] != 'BASE TABLE' or x['engine'].lower() != 'innodb' for x in tables
        ):
            return 'partial'
        expected_columns = {(table, name): spec for table, items in COLUMNS.items() for name, spec in items.items()}
        if len(columns) != len(expected_columns):
            return 'partial'
        seen = set()
        for col in columns:
            key = (col['table'], col['name'])
            spec = expected_columns.get(key)
            if (spec is None or key in seen or col['nullable'] not in ('YES', 'NO')
                    or (col['type'].lower(), col['nullable'] == 'YES', col['extra'].lower()) != spec
                    or col['default'] is not None):
                return 'partial'
            seen.add(key)
        found_indexes = {}
        for row in indexes:
            if row['non_unique'] != 0:
                continue
            key = (row['table'], row['name'])
            if row['sub_part'] is not None or row['type'].upper() != 'BTREE' or row['column'] is None:
                return 'partial'
            found_indexes.setdefault(key, []).append((int(row['seq']), row['column']))
        if any([seq for seq, _ in sorted(rows)] != list(range(1, len(rows) + 1))
               for rows in found_indexes.values()):
            return 'partial'
        actual_indexes = {key: tuple(c for _, c in sorted(rows)) for key, rows in found_indexes.items()}
        expected_indexes = {(table, name): cols for table, values in UNIQUE.items() for name, cols in values.items()}
        if actual_indexes != expected_indexes:
            return 'partial'
        expected_fks = {(table, *item) for table, values in FKS.items() for item in values}
        actual_fks = set()
        for fk in foreign_keys:
            if (fk['delete_rule'] != 'RESTRICT' or fk['update_rule'] not in ('RESTRICT', 'NO ACTION')
                    or fk['same_schema'] != 1 or fk['seq'] != 1):
                return 'partial'
            actual_fks.add((fk['table'], fk['column'], fk['ref_table'], fk['ref_column']))
        if len(foreign_keys) != len(expected_fks) or actual_fks != expected_fks:
            return 'partial'
        expected_checks = {(table, name): parse_check(expression)
                           for table, values in CHECKS.items() for name, expression in values.items()}
        actual_checks = {}
        for check in checks:
            key = (check['table'], check['name'])
            if key in actual_checks or check['enforced'] != 'YES':
                return 'partial'
            actual_checks[key] = parse_check(check['clause'])
        if actual_checks != expected_checks:
            return 'partial'
        found_triggers = {}
        for trigger in triggers:
            name = trigger['name']
            statement = re.sub(r'\s+', ' ', trigger['statement'].strip().lower()).rstrip(';')
            if (name in found_triggers or trigger['table'] != MODEL or trigger['timing'] != 'BEFORE'
                    or statement != "signal sqlstate '45000' set message_text = 'model attempt ownership is immutable'"):
                return 'partial'
            found_triggers[name] = trigger['event']
        return 'complete' if found_triggers == TRIGGERS else 'partial'
    except (KeyError, TypeError, ValueError, AttributeError):
        return 'partial'


if __name__ == '__main__':
    try:
        print(classify(json.load(sys.stdin)))
    except (ValueError, TypeError):
        sys.exit(1)
