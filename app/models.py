from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RequestRecord(Base):
    """One row per client request, whatever happened to it.

    provider_attempted is an array so a failover chain is a fact in the database
    rather than a claim in the README.
    """

    __tablename__ = "requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    route_policy: Mapped[str] = mapped_column(String(16))
    provider_attempted: Mapped[list[str]] = mapped_column(ARRAY(String(32)), default=list)
    provider_served: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_served: Mapped[str | None] = mapped_column(String(64), nullable=True)

    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer)

    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    # False when the serving provider returned no usage block and we estimated it.
    usage_estimated: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    status: Mapped[str] = mapped_column(String(16))  # ok | error
    failover_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (Index("ix_requests_provider_served_ts", "provider_served", "ts"),)
