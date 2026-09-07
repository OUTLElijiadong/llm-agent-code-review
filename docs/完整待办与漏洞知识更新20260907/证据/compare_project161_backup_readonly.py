import datetime
import gzip
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time

BACKUP = '/opt/code-review/backups/code_review_20260905T201610Z_343616d9d40c.sql.gz'
EXPECTED_SHA = '7bb99942cc0e571008743fc66b0d1cdd88fcf13640579b32fb493c0679d7dc6f'
EXPECTED_BYTES = 402726075
RELEASE = 'f1ab6684e86c0df1c564c0e893cdfdd8389da78e'
COLUMNS = [('id', 'bigint'), ('user_id', 'bigint'), ('project_name', 'varchar(100)'), ('description', 'varchar(500)'), ('language', 'varchar(50)'), ('status', 'varchar(20)'), ('create_time', 'datetime'), ('update_time', 'datetime')]
START = time.monotonic()


class Blocked(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise Blocked(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.sha = hashlib.sha256()
        self.count = 0

    def read(self, size=-1):
        require(time.monotonic() - START < 300, 'single_pass_time_limit')
        chunk = self.stream.read(size)
        self.sha.update(chunk)
        self.count += len(chunk)
        return chunk


class ValuesParser:
    def __init__(self, text):
        self.text = text
        self.position = 0

    def whitespace(self):
        while self.position < len(self.text) and self.text[self.position] in ' \t\r\n':
            self.position += 1

    def expect(self, token):
        self.whitespace()
        require(self.text.startswith(token, self.position), 'insert_syntax_not_supported')
        self.position += len(token)

    def scalar(self):
        self.whitespace()
        require(self.position < len(self.text), 'incomplete_insert_value')
        if self.text[self.position] == "'":
            self.position += 1
            value = []
            escapes = {'0': '\0', 'b': '\b', 'n': '\n', 'r': '\r', 't': '\t', 'Z': '\x1a', '\\': '\\', "'": "'", '"': '"'}
            while self.position < len(self.text):
                character = self.text[self.position]
                self.position += 1
                if character == "'":
                    if self.position < len(self.text) and self.text[self.position] == "'":
                        value.append("'")
                        self.position += 1
                    else:
                        return ''.join(value)
                elif character == '\\':
                    require(self.position < len(self.text), 'incomplete_string_escape')
                    escaped = self.text[self.position]
                    self.position += 1
                    require(escaped in escapes, 'unsupported_string_escape')
                    value.append(escapes[escaped])
                else:
                    require(character not in '\r\n\0', 'raw_control_in_dump_string')
                    value.append(character)
            raise Blocked('unterminated_dump_string')
        if self.text.startswith('NULL', self.position):
            self.position += 4
            return None
        match = re.match(r'-?(?:0|[1-9][0-9]*)', self.text[self.position:])
        require(match is not None, 'unsupported_non_string_literal')
        self.position += len(match.group())
        return int(match.group())

    def rows(self):
        self.expect('INSERT INTO `project`')
        self.whitespace()
        if self.text[self.position] == '(':
            ending = self.text.find(')', self.position)
            require(ending >= 0, 'incomplete_insert_column_list')
            names = self.text[self.position + 1:ending].split(',')
            require([name.strip() for name in names] == ['`' + name + '`' for name, _ in COLUMNS], 'insert_column_list_mismatch')
            self.position = ending + 1
        self.expect('VALUES')
        while True:
            self.expect('(')
            row = []
            for index in range(len(COLUMNS)):
                if index:
                    self.expect(',')
                row.append(self.scalar())
            self.expect(')')
            yield row
            self.whitespace()
            require(self.position < len(self.text), 'missing_insert_terminator')
            if self.text[self.position] == ';':
                self.position += 1
                self.whitespace()
                require(self.position == len(self.text), 'unexpected_insert_suffix')
                return
            self.expect(',')


def normalize(row):
    require(len(row) == len(COLUMNS), 'row_column_count_mismatch')
    normalized = []
    for (name, datatype), value in zip(COLUMNS, row):
        if value is None:
            require(name in ('description', 'language'), 'unexpected_null')
        elif datatype == 'bigint':
            require(type(value) is int and -(2 ** 63) <= value < 2 ** 63, 'bigint_type_or_range')
            value = str(value)
        elif datatype == 'datetime':
            require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}', value), 'datetime_format_or_precision')
            require(datetime.datetime.strptime(value, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%d %H:%M:%S') == value, 'invalid_datetime')
        else:
            require(isinstance(value, str), 'varchar_value_type')
            require(len(value) <= int(re.search(r'\d+', datatype).group()), 'varchar_length')
            value.encode('utf-8', errors='strict')
        normalized.append([name, datatype, value])
    return normalized


def stream_project(output):
    before = os.lstat(BACKUP)
    require(stat.S_ISREG(before.st_mode) and before.st_size == EXPECTED_BYTES, 'backup_file_metadata_mismatch')
    section = []
    section_bytes = 0
    in_project = False
    finished = False
    charset_seen = False
    sql_mode_seen = False
    with open(BACKUP, 'rb', buffering=0) as raw:
        reader = HashReader(raw)
        compressed = gzip.GzipFile(fileobj=reader, mode='rb')
        while True:
            part = compressed.readline(65536)
            require(part, 'project_block_missing_or_incomplete')
            if not part.endswith(b'\n'):
                pieces = [part] if in_project else None
                size = len(part)
                while not part.endswith(b'\n'):
                    part = compressed.readline(65536)
                    require(part, 'unterminated_dump_line')
                    size += len(part)
                    if in_project:
                        require(size < 16 * 1024 * 1024, 'project_statement_size_limit')
                        pieces.append(part)
                if not in_project:
                    continue
                part = b''.join(pieces)
            if not in_project:
                header_statement = part.startswith((b'/*!', b'SET '))
                if header_statement and re.search(rb'SET NAMES utf8mb4\b', part):
                    charset_seen = True
                mode = re.search(rb"\bSQL_MODE='([^']*)'", part) if header_statement else None
                if mode:
                    require(mode.group(1) == b'NO_AUTO_VALUE_ON_ZERO', 'unrecognized_dump_sql_mode')
                    sql_mode_seen = True
                if part.strip() == b'-- Table structure for table `project`':
                    in_project = True
                continue
            section_bytes += len(part)
            require(section_bytes < 16 * 1024 * 1024, 'project_section_size_limit')
            section.append(part.decode('utf-8', errors='strict'))
            if part.strip() == b'UNLOCK TABLES;':
                finished = True
                break
        compressed.close()
        while reader.read(1024 * 1024):
            pass
        after = os.fstat(raw.fileno())
    final_stat = os.lstat(BACKUP)
    stable = all((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns) for item in (after, final_stat))
    output['backup'] = {'path': BACKUP, 'bytes': reader.count, 'sha256': reader.sha.hexdigest(), 'expected_sha_matches': reader.sha.hexdigest() == EXPECTED_SHA, 'stable_during_read': stable, 'file_open_count': 1, 'project_section_bytes': section_bytes, 'project_block_complete': finished}
    require(stable and reader.count == EXPECTED_BYTES and reader.sha.hexdigest() == EXPECTED_SHA, 'backup_integrity_mismatch')
    require(charset_seen and sql_mode_seen and finished, 'dump_encoding_or_sql_mode_not_proven')
    text = ''.join(section)
    ddl = re.findall(r'CREATE TABLE `project` \(\n(.*?)\n\)([^;]*);', text, re.S)
    require(len(ddl) == 1, 'project_ddl_cardinality')
    body, options = ddl[0]
    require(re.search(r'\bDEFAULT CHARSET=utf8mb4\b', options), 'project_default_charset')
    columns = []
    for line in body.splitlines():
        if line.lstrip().startswith('`'):
            match = re.fullmatch(r'\s*`([a-z_]+)` (bigint|varchar\([0-9]+\)|datetime)(\s+.*?)(?:,)?', line)
            require(match is not None, 'unsupported_project_column_type')
            name, datatype, attributes = match.groups()
            require(not re.search(r'\b(?:unsigned|GENERATED|INVISIBLE|BINARY)\b', attributes, re.I), 'unsupported_column_attributes')
            charset = re.search(r'CHARACTER SET ([A-Za-z0-9_]+)', attributes)
            require(charset is None or charset.group(1) == 'utf8mb4', 'column_charset_mismatch')
            columns.append({'name': name, 'type': datatype, 'nullable': 'NOT NULL' not in attributes.upper()})
        else:
            require(re.match(r'\s*(?:PRIMARY KEY|KEY|UNIQUE KEY|CONSTRAINT)\b', line), 'unrecognized_ddl_element')
    require([(column['name'], column['type']) for column in columns] == COLUMNS, 'backup_complete_column_set_mismatch')
    require(re.findall(r'PRIMARY KEY\s*\(([^)]*)\)', body) == ['`id`'], 'backup_primary_key_mismatch')
    outside_ddl = re.sub(r'CREATE TABLE `project` \(\n.*?\n\)[^;]*;', '', text, count=1, flags=re.S)
    for line in outside_ddl.splitlines():
        stripped = line.strip()
        allowed = not stripped or stripped.startswith('--') or stripped in ('DROP TABLE IF EXISTS `project`;', 'LOCK TABLES `project` WRITE;', 'UNLOCK TABLES;') or stripped.startswith('INSERT INTO `project`') or re.fullmatch(r'/\*![0-9]{5} (?:SET [^;]*|ALTER TABLE `project` (?:DISABLE|ENABLE) KEYS) \*/;', stripped)
        require(allowed, 'unsupported_statement_in_project_block')
    inserts = [line for line in text.splitlines() if line.startswith('INSERT INTO ')]
    require(inserts and all(line.startswith('INSERT INTO `project`') for line in inserts), 'project_insert_boundary')
    primary_keys = set()
    target = None
    target_count = 0
    for statement in inserts:
        for row in ValuesParser(statement).rows():
            normalized = normalize(row)
            require(type(row[0]) is int and row[0] not in primary_keys, 'duplicate_or_invalid_backup_primary_key')
            primary_keys.add(row[0])
            if row[0] == 161:
                target_count += 1
                target = normalized
    require(target_count == 1 and len(primary_keys) == 100, 'backup_project_or_target_row_count')
    output['parser'] = {'format': 'strict_mysqldump_project_insert', 'complete_columns': columns, 'primary_key': ['id'], 'primary_keys_unique': True, 'project_rows': len(primary_keys), 'target_rows': target_count, 'insert_statements': len(inserts), 'utf8mb4_confirmed': charset_seen, 'no_auto_value_on_zero_confirmed': sql_mode_seen, 'sql_from_backup_executed': False, 'non_project_tables_parsed': 0}
    return target, columns


def current_project(output, before_row, backup_columns):
    field_values = ','.join("'" + name + "'," + ("DATE_FORMAT(`" + name + "`, '%Y-%m-%d %H:%i:%s')" if datatype == 'datetime' else '`' + name + '`') for name, datatype in COLUMNS)
    sql = "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ; SET SESSION TRANSACTION READ ONLY; START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;\n"
    sql += "SELECT JSON_OBJECT('kind','schema','name',COLUMN_NAME,'type',COLUMN_TYPE,'nullable',IS_NULLABLE='YES','charset',CHARACTER_SET_NAME,'ordinal',ORDINAL_POSITION,'key',COLUMN_KEY,'precision',DATETIME_PRECISION) FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='project' ORDER BY ORDINAL_POSITION;\n"
    sql += "SELECT JSON_OBJECT('kind','counts','project_rows',COUNT(*),'target_rows',SUM(id=161),'read_only',@@transaction_read_only,'observed_at_utc',UTC_TIMESTAMP(6)) FROM project;\n"
    sql += "SELECT JSON_OBJECT('kind','target'," + field_values + ") FROM project WHERE id=161;\nROLLBACK;\n"
    command = ['docker', 'exec', '-i', 'cr_mysql', 'sh', '-c', 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql --protocol=TCP -h127.0.0.1 -uroot --database=code_review --default-character-set=utf8mb4 --connect-timeout=8 --batch --raw --skip-column-names']
    result = subprocess.run(command, input=sql, capture_output=True, text=True, timeout=40)
    require(result.returncode == 0, 'current_readonly_query_failed')
    records = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    schema = [row for row in records if row.get('kind') == 'schema']
    counts = [row for row in records if row.get('kind') == 'counts']
    target = [row for row in records if row.get('kind') == 'target']
    require(len(counts) == len(target) == 1, 'current_result_cardinality')
    require([(item['name'], item['type']) for item in schema] == COLUMNS, 'current_complete_column_set_mismatch')
    require([item['name'] for item in schema if item['key'] == 'PRI'] == ['id'], 'current_primary_key_mismatch')
    require(all(bool(current['nullable']) == backup['nullable'] for current, backup in zip(schema, backup_columns)), 'nullable_schema_mismatch')
    require(all(item['charset'] == 'utf8mb4' if item['type'].startswith('varchar') else item['charset'] is None for item in schema), 'current_character_set_mismatch')
    require(all(item['precision'] == 0 for item in schema if item['type'] == 'datetime'), 'current_datetime_precision')
    require(counts[0]['target_rows'] == 1, 'current_project_or_target_count')
    after_row = normalize([target[0][name] for name, _ in COLUMNS])
    output['current'] = counts[0]
    output['schema_matches'] = True
    output['datetime_normalization'] = 'DATE_FORMAT preserves DATETIME(0) seconds after schema precision=0 verification; JSON serialization previously added .000000.'
    output['field_comparison'] = [{'field': before[0], 'equal': before == after, 'backup_normalized_sha256': digest(before), 'current_normalized_sha256': digest(after)} for before, after in zip(before_row, after_row)]
    output['row_fingerprint'] = {'backup_normalized_sha256': digest(before_row), 'current_normalized_sha256': digest(after_row), 'equal': before_row == after_row}
    output['all_eight_fields_equal'] = all(item['equal'] for item in output['field_comparison'])


def selftest():
    statement = "INSERT INTO `project` VALUES (161,1,'测试,();','a\\'b\\\\c\\n\\r\\t\\0\\Z','it''s','NULL','2026-01-02 03:04:05','2026-01-02 03:04:05'),(162,1,'x',NULL,NULL,'active','2026-01-02 03:04:05','2026-01-02 03:04:05');"
    rows = list(ValuesParser(statement).rows())
    assert len(rows) == 2 and rows[0][3] == "a'b\\c\n\r\t\0\x1a"
    assert rows[0][4] == "it's" and rows[0][5] == 'NULL' and rows[1][3] is None
    for row in rows:
        normalize(row)
    invalid = [statement[:-1], statement.replace("'active'", "_binary'active'"), statement.replace("'active'", "'bad\\q'"), statement.replace("(162,1,", "(162,1.5,")]
    for item in invalid:
        try:
            list(ValuesParser(item).rows())
        except Blocked:
            continue
        raise AssertionError('invalid_input_not_blocked')
    print(json.dumps({'synthetic_parser_checks': 'passed', 'production_reads': 0}))


def main():
    output = {'schema': 'project161-backup-comparison-v2', 'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'status': '阻塞', 'target_id': 161}
    try:
        with open('/opt/code-review/deploy/.releases/current.env', encoding='utf-8') as ledger:
            values = dict(line.strip().split('=', 1) for line in ledger if '=' in line and not line.startswith('#'))
        output['release_sha_present_matches'] = RELEASE in [value.strip('\"\'') for value in values.values()]
        require(output['release_sha_present_matches'], 'release_changed_before_comparison')
        target, columns = stream_project(output)
        current_project(output, target, columns)
        output['status'] = '通过' if output['all_eight_fields_equal'] else '失败'
    except Blocked as error:
        output['blocker'] = str(error)
    except Exception as error:
        output['blocker'] = 'unexpected_' + type(error).__name__
    output['finished_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    output['elapsed_seconds'] = round(time.monotonic() - START, 3)
    print(json.dumps(output, ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        selftest()
    else:
        main()
