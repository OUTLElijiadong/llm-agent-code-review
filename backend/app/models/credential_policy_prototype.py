"""Detached experiment; no production metadata, migrations, or enabled caller."""

from sqlalchemy import BigInteger, CheckConstraint, Column, Integer, LargeBinary, String, UniqueConstraint
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import declarative_base

CredentialPolicyBase = declarative_base()
_PK = BigInteger().with_variant(Integer, "sqlite")
_BYTES_16 = LargeBinary(16).with_variant(mysql.VARBINARY(16), "mysql")
_BYTES_32 = LargeBinary(32).with_variant(mysql.VARBINARY(32), "mysql")


class CredentialPolicyPrototype(CredentialPolicyBase):
    __tablename__ = "credential_policy_prototype"
    __table_args__ = (
        UniqueConstraint("kind", "owner_id", name="uq_prototype_kind_owner"),
        CheckConstraint("(kind = 'global' AND owner_id = 0) OR (kind = 'user' AND owner_id > 0)",
                        name="ck_prototype_kind_owner"),
        CheckConstraint("state IN ('active', 'system', 'unknown', 'failed')", name="ck_prototype_state"),
        CheckConstraint("revision > 0", name="ck_prototype_revision"),
    )

    id = Column(_PK, primary_key=True, autoincrement=True)
    kind = Column(String(16), nullable=False)
    owner_id = Column(_PK, nullable=False)
    state = Column(String(16), nullable=False)
    revision = Column(BigInteger, nullable=False)
    incarnation = Column(_BYTES_16, nullable=False)
    config_digest = Column(_BYTES_32, nullable=False)
