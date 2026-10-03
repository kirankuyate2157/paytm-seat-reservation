"""initial schema: users, shows, seats, reservations, reservation_seats, user_show_quota, audit_logs

Purpose of each object (why it exists):
- seats UNIQUE(show_id, seat_code): lookup path for reserve + guarantees one row per seat.
- seats CHECK owner matches state: a seat can never be 'confirmed' without an owner.
- reservations UNIQUE(user_id, show_id, idempotency_key): exactly-once guard for retries.
- reservation_seats partial UNIQUE(seat_id) WHERE released_at IS NULL: DB-level double-sell backstop.
- user_show_quota: per-user limit counter; its row lock serialises one user's parallel requests.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email varchar(320) NOT NULL UNIQUE,
            password_hash text NOT NULL,
            role varchar(16) NOT NULL DEFAULT 'user',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_users_role CHECK (role IN ('admin','user','guest'))
        )"""
    )
    op.execute(
        """
        CREATE TABLE shows (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL,
            total_seats integer NOT NULL,
            price_paise bigint NOT NULL,
            per_user_limit integer NOT NULL DEFAULT 4,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_shows_total_pos CHECK (total_seats > 0),
            CONSTRAINT ck_shows_price_nonneg CHECK (price_paise >= 0),
            CONSTRAINT ck_shows_limit_pos CHECK (per_user_limit > 0)
        )"""
    )
    op.execute(
        """
        CREATE TABLE seats (
            id bigserial PRIMARY KEY,
            show_id uuid NOT NULL REFERENCES shows(id) ON DELETE RESTRICT,
            seat_code varchar(32) NOT NULL,
            state varchar(16) NOT NULL DEFAULT 'available',
            owner_user_id uuid REFERENCES users(id),
            reservation_id uuid,
            hold_expiry_time timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_seats_state CHECK (state IN ('available','held','confirmed')),
            CONSTRAINT ck_seats_owner_matches_state CHECK ((state = 'available') = (owner_user_id IS NULL))
        )"""
    )
    op.execute("CREATE UNIQUE INDEX uq_seats_show_code ON seats (show_id, seat_code)")
    op.execute("CREATE INDEX ix_seats_show_state ON seats (show_id, state)")
    op.execute("CREATE INDEX ix_seats_hold_expiry ON seats (hold_expiry_time) WHERE state = 'held'")
    op.execute("CREATE INDEX ix_seats_owner ON seats (owner_user_id, show_id) WHERE state <> 'available'")
    op.execute(
        """
        CREATE TABLE reservations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES users(id),
            show_id uuid NOT NULL REFERENCES shows(id),
            idempotency_key varchar(200) NOT NULL,
            request_hash varchar(64) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'confirmed',
            amount_paise bigint NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_reservations_status CHECK (status IN ('confirmed','cancelled'))
        )"""
    )
    op.execute("CREATE UNIQUE INDEX uq_reservations_idem ON reservations (user_id, show_id, idempotency_key)")
    op.execute("CREATE INDEX ix_reservations_user_created ON reservations (user_id, created_at DESC)")
    op.execute(
        """
        CREATE TABLE reservation_seats (
            reservation_id uuid NOT NULL REFERENCES reservations(id),
            seat_id bigint NOT NULL REFERENCES seats(id),
            released_at timestamptz,
            PRIMARY KEY (reservation_id, seat_id)
        )"""
    )
    op.execute("CREATE UNIQUE INDEX uq_reservation_seats_active ON reservation_seats (seat_id) WHERE released_at IS NULL")
    op.execute(
        """
        CREATE TABLE user_show_quota (
            user_id uuid NOT NULL REFERENCES users(id),
            show_id uuid NOT NULL REFERENCES shows(id),
            seats_held integer NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, show_id),
            CONSTRAINT ck_quota_nonneg CHECK (seats_held >= 0)
        )"""
    )
    op.execute(
        """
        CREATE TABLE audit_logs (
            id bigserial PRIMARY KEY,
            user_id uuid,
            action varchar(64) NOT NULL,
            resource varchar(64),
            resource_id varchar(64),
            outcome varchar(32),
            details jsonb,
            ip_address inet,
            correlation_id varchar(64),
            created_at timestamptz NOT NULL DEFAULT now()
        )"""
    )
    op.execute("CREATE INDEX ix_audit_user_created ON audit_logs (user_id, created_at)")
    op.execute("CREATE INDEX ix_audit_action_created ON audit_logs (action, created_at)")


def downgrade() -> None:
    for t in ("audit_logs", "user_show_quota", "reservation_seats", "reservations", "seats", "shows", "users"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
