"""initial requests table

Revision ID: 0001
Revises:
Create Date: 2025-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("route_policy", sa.String(length=16), nullable=False),
        sa.Column(
            "provider_attempted",
            postgresql.ARRAY(sa.String(length=32)),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("provider_served", sa.String(length=32), nullable=True),
        sa.Column("model_served", sa.String(length=64), nullable=True),
        sa.Column("cache_hit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), server_default="0", nullable=False),
        sa.Column("tokens_out", sa.Integer(), server_default="0", nullable=False),
        sa.Column("usage_estimated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("cost_usd", sa.Float(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("failover_reason", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_requests_request_id", "requests", ["request_id"], unique=True)
    op.create_index("ix_requests_ts", "requests", ["ts"])
    op.create_index("ix_requests_provider_served_ts", "requests", ["provider_served", "ts"])


def downgrade() -> None:
    op.drop_index("ix_requests_provider_served_ts", table_name="requests")
    op.drop_index("ix_requests_ts", table_name="requests")
    op.drop_index("ix_requests_request_id", table_name="requests")
    op.drop_table("requests")
