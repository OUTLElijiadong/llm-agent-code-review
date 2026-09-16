import datetime
import json
import re
from urllib.parse import urlparse
from app.core.config import settings
from app.core.database import engine
from app.utils.api_resolver import decrypt_api_key_with_metadata


def safe_model(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', value) else 'other_or_missing'


def endpoint_kind(value):
    return 'official_deepseek' if urlparse(value or '').hostname == 'api.deepseek.com' else 'other_or_missing'

report = {'observed_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
          'business_writes': False, 'llm_requests': 0}
connection = None
try:
    connection = engine.raw_connection()
    cursor = connection.cursor()
    cursor.execute('SET SESSION MAX_EXECUTION_TIME=10000')
    cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
    cursor.execute('SET SESSION TRANSACTION READ ONLY')
    cursor.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
    cursor.execute('SELECT @@transaction_read_only, DATABASE()')
    mode, database = cursor.fetchone()
    if mode != 1 or database != 'code_review':
        raise RuntimeError('readonly_database_contract')
    report['transaction_read_only'] = mode
    cursor.execute('SELECT config_value FROM system_config WHERE config_key=%s', ('llm_provider',))
    row = cursor.fetchone()
    try:
        stored = json.loads(row[0]) if row and row[0] else None
    except (TypeError, ValueError):
        stored = None
    report['stored_row_present'] = bool(row)
    report['stored_json_object'] = isinstance(stored, dict)
    if isinstance(stored, dict):
        encrypted = stored.get('api_key_enc')
        decrypted = decrypt_api_key_with_metadata(encrypted) if encrypted else None
        report['stored'] = {
            'active': bool(stored.get('active')),
            'model': safe_model(stored.get('model')),
            'endpoint_kind': endpoint_kind(stored.get('base_url')),
            'encrypted_credential_present': bool(encrypted),
            'decryptable_by_current_keyring': decrypted is not None,
            'decrypted_credential_nonempty': bool(decrypted and decrypted.plaintext),
            'decryption_source': decrypted.source if decrypted else None,
            'needs_rotation': decrypted.needs_rotation if decrypted else None,
        }
    report['runtime_default'] = {
        'model': safe_model(settings.deepseek_model),
        'orchestrator_model': safe_model(settings.deepseek_orchestrator_model),
        'endpoint_kind': endpoint_kind(settings.deepseek_base_url),
        'credential_present': bool(settings.deepseek_api_key.strip()),
        'configured_encryption_key_count': len(set(x.strip() for x in settings.api_key_encryption_keys if x.strip())),
        'legacy_jwt_decryption_available': bool(settings.jwt_secret),
    }
    cursor.execute("SELECT model_name,status,COUNT(*),MAX(create_time) FROM ai_call_log WHERE create_time>=UTC_TIMESTAMP()-INTERVAL 24 HOUR GROUP BY model_name,status ORDER BY model_name,status")
    report['last_24h_logged_calls'] = [
        {'model': safe_model(model), 'status': status if status in ('success','failed','retry') else 'other', 'count': count, 'latest_utc': str(latest)}
        for model, status, count, latest in cursor.fetchall()
    ]
    report['availability_limit'] = '配置存在和历史成功日志不证明当前凭据可用；未发模型请求，也不能把调用日志归因到某个密钥'
    report['status'] = 'passed'
except Exception as error:
    report['status'] = 'failed'
    report['error_type'] = type(error).__name__
finally:
    if connection is not None:
        connection.rollback()
        connection.close()
print(json.dumps(report, ensure_ascii=False, indent=2))
