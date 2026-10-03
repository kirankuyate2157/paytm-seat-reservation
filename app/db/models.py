"""Declarative mirror of migration 0001 (ORM for non-hot paths + Alembic autogenerate diffs).
The hot path (reserve/cancel) uses explicit SQL in app/services."""
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()"))


def _ts():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, server_default="user")
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (CheckConstraint("role IN ('admin','user','guest')", name="ck_users_role"),)


class Show(Base):
    __tablename__ = "shows"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    total_seats: Mapped[int] = mapped_column(Integer, nullable=False)
    price_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    per_user_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="4")
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (
        CheckConstraint("total_seats > 0", name="ck_shows_total_pos"),
        CheckConstraint("price_paise >= 0", name="ck_shows_price_nonneg"),
        CheckConstraint("per_user_limit > 0", name="ck_shows_limit_pos"),
    )


class Seat(Base):
    __tablename__ = "seats"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    show_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shows.id", ondelete="RESTRICT"), nullable=False)
    seat_code: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="available")
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    hold_expiry_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (
        Index("uq_seats_show_code", "show_id", "seat_code", unique=True),
        Index("ix_seats_show_state", "show_id", "state"),
        Index("ix_seats_hold_expiry", "hold_expiry_time", postgresql_where=text("state = 'held'")),
        Index("ix_seats_owner", "owner_user_id", "show_id", postgresql_where=text("state <> 'available'")),
        CheckConstraint("state IN ('available','held','confirmed')", name="ck_seats_state"),
        CheckConstraint("(state = 'available') = (owner_user_id IS NULL)", name="ck_seats_owner_matches_state"),
    )


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    show_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shows.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="confirmed")
    amount_paise: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    __table_args__ = (
        Index("uq_reservations_idem", "user_id", "show_id", "idempotency_key", unique=True),
        Index("ix_reservations_user_created", "user_id", "created_at"),
        CheckConstraint("status IN ('confirmed','cancelled')", name="ck_reservations_status"),
    )


class ReservationSeat(Base):
    __tablename__ = "reservation_seats"
    reservation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reservations.id"), primary_key=True)
    seat_id: Mapped[int] = mapped_column(ForeignKey("seats.id"), primary_key=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("uq_reservation_seats_active", "seat_id", unique=True, postgresql_where=text("released_at IS NULL")),
    )


class UserShowQuota(Base):
    __tablename__ = "user_show_quota"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    show_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("shows.id"), primary_key=True)
    seats_held: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    __table_args__ = (CheckConstraint("seats_held >= 0", name="ck_quota_nonneg"),)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str | None] = mapped_column(String(32))
    details: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(INET)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (
        Index("ix_audit_user_created", "user_id", "created_at"),
        Index("ix_audit_action_created", "action", "created_at"),
    )
