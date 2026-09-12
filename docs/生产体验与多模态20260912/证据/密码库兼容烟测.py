"""用合成测试材料验证跨版本Fernet、RSA与JWT；不读取任何真实密钥。"""
import base64
import json
import platform
import sys
from pathlib import Path

import cryptography
import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

fixture = json.loads(Path(sys.argv[1]).read_text())
fernet = Fernet(base64.urlsafe_b64encode(bytes(range(32))))
assert fernet.decrypt(fixture['token'].encode()).decode() == 'Prism兼容测试'
try:
    fernet.decrypt(b'invalid-test-token')
except InvalidToken:
    pass
else:
    raise AssertionError('损坏密文未拒绝')
private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
loaded = serialization.load_pem_private_key(pem, password=None)
message = b'Prism dependency smoke'
signature = loaded.sign(message, padding.PKCS1v15(), hashes.SHA256())
loaded.public_key().verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
try:
    loaded.public_key().verify(signature, b'changed', padding.PKCS1v15(), hashes.SHA256())
except InvalidSignature:
    pass
else:
    raise AssertionError('篡改消息未拒绝')
token = jwt.encode({'sub': 'synthetic-user'}, loaded, algorithm='RS256')
assert jwt.decode(token, loaded.public_key(), algorithms=['RS256'])['sub'] == 'synthetic-user'
print(json.dumps({'python': platform.python_version(), 'cryptography': cryptography.__version__, 'source_cipher_version': fixture['source_version'], 'fernet_cross_version': True, 'invalid_cipher_rejected': True, 'rsa_pem_roundtrip': True, 'rsa_signature': True, 'tampered_message_rejected': True, 'pyjwt_rs256': True}, ensure_ascii=False, indent=2))
