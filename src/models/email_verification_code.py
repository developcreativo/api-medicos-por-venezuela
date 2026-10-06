"""Modelo ORM para códigos de verificación de correo (OTP de 6 dígitos).

Una fila por (email, purpose) con contadores de envío/intentos y expiración.
RLS habilitada sin policies: la API entra como dueña (service_role), PostgREST no toca la tabla.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID

from src.db.base import Base


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"
    __table_args__ = (
        CheckConstraint("purpose IN ('patient', 'doctor')", name="ck_email_verification_purpose"),
        UniqueConstraint("email", "purpose", name="uq_email_verification_email_purpose"),
        Index("ix_email_verification_email_purpose", "email", "purpose"),
        {"schema": "public"},
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(Text, nullable=False)  # normalizado a minúsculas
    purpose = Column(String(10), nullable=False)  # 'patient' | 'doctor'
    code_hash = Column(Text, nullable=False)  # HMAC-SHA256(secreto, "email:purpose:codigo")
    attempts = Column(SmallInteger, nullable=False, default=0)
    sends_count = Column(SmallInteger, nullable=False, default=0)
    window_started_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_sent_at = Column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
