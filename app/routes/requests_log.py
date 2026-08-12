from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.models import RequestRecord

router = APIRouter()


@router.get("/requests")
async def recent_requests(
    request: Request,
    limit: int = Query(default=20, ge=1, le=500),
    provider: str | None = None,
    failed_only: bool = False,
) -> dict:
    """The request log, newest first.

    Exists so the failover demo can show the recorded `provider_attempted` chain
    rather than asking you to believe the response body.
    """
    sessionmaker = request.app.state.sessionmaker
    if sessionmaker is None:
        return {"rows": []}

    # Rows are written in batches off the request path; flush so the log you read
    # is the log as of now.
    await request.app.state.accounting.flush()

    stmt = select(RequestRecord).order_by(RequestRecord.ts.desc()).limit(limit)
    if provider:
        stmt = stmt.where(RequestRecord.provider_served == provider)
    if failed_only:
        stmt = stmt.where(RequestRecord.status == "error")

    async with sessionmaker() as session:
        rows = (await session.execute(stmt)).scalars().all()

    return {
        "rows": [
            {
                "request_id": r.request_id,
                "ts": r.ts.isoformat(),
                "route_policy": r.route_policy,
                "provider_attempted": r.provider_attempted,
                "provider_served": r.provider_served,
                "model_served": r.model_served,
                "cache_hit": r.cache_hit,
                "latency_ms": r.latency_ms,
                "tokens_in": r.tokens_in,
                "tokens_out": r.tokens_out,
                "usage_estimated": r.usage_estimated,
                "cost_usd": r.cost_usd,
                "status": r.status,
                "failover_reason": r.failover_reason,
            }
            for r in rows
        ]
    }
