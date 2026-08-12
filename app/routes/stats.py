from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Query, Request
from sqlalchemy import case, func, select

from app.models import RequestRecord
from app.pricing import PRICING_AS_OF
from app.schemas import Provider

router = APIRouter()

_P50 = func.percentile_cont(0.5).within_group(RequestRecord.latency_ms.asc())
_P95 = func.percentile_cont(0.95).within_group(RequestRecord.latency_ms.asc())


@router.get("/stats")
async def stats(request: Request, window_minutes: int = Query(default=60, ge=1, le=10_080)) -> dict:
    """Everything the benchmarks assert on, read straight out of Postgres."""
    app = request.app
    since = datetime.now(UTC) - timedelta(minutes=window_minutes)

    # Accounting rows are written in batches by a background task; flush first so
    # /stats never reports a number that is merely late.
    await app.state.accounting.flush()

    out: dict = {
        "window_minutes": window_minutes,
        "since": since.isoformat(),
        "pricing_as_of": PRICING_AS_OF,
        "cache_enabled": app.state.cache.enabled,
        "accounting": app.state.accounting.stats(),
    }

    if app.state.sessionmaker is not None:
        async with app.state.sessionmaker() as session:
            out["totals"] = await _totals(session, since)
            out["by_provider"] = await _by_provider(session, since)
            out["by_policy"] = await _by_policy(session, since)
            out["latency_by_cache"] = await _latency_by_cache(session, since)
    else:
        out["totals"] = None

    out["health"] = [await app.state.health.stats(p) for p in Provider]
    return out


async def _totals(session, since) -> dict:
    hits = func.sum(case((RequestRecord.cache_hit.is_(True), 1), else_=0))
    row = (
        await session.execute(
            select(
                func.count().label("requests"),
                hits.label("cache_hits"),
                func.sum(case((RequestRecord.status == "error", 1), else_=0)).label("errors"),
                func.coalesce(func.sum(RequestRecord.cost_usd), 0.0).label("cost_usd"),
                func.coalesce(func.sum(RequestRecord.tokens_in), 0).label("tokens_in"),
                func.coalesce(func.sum(RequestRecord.tokens_out), 0).label("tokens_out"),
                func.sum(case((RequestRecord.usage_estimated.is_(True), 1), else_=0)).label(
                    "estimated_usage_rows"
                ),
                _P50.label("p50_ms"),
                _P95.label("p95_ms"),
            ).where(RequestRecord.ts >= since)
        )
    ).one()
    total = row.requests or 0
    return {
        "requests": total,
        "cache_hits": int(row.cache_hits or 0),
        "cache_hit_rate": round((row.cache_hits or 0) / total, 4) if total else None,
        "errors": int(row.errors or 0),
        "cost_usd": round(float(row.cost_usd or 0.0), 6),
        "tokens_in": int(row.tokens_in or 0),
        "tokens_out": int(row.tokens_out or 0),
        "estimated_usage_rows": int(row.estimated_usage_rows or 0),
        "p50_ms": _num(row.p50_ms),
        "p95_ms": _num(row.p95_ms),
    }


async def _by_provider(session, since) -> list[dict]:
    rows = (
        await session.execute(
            select(
                RequestRecord.provider_served,
                func.count().label("requests"),
                func.coalesce(func.sum(RequestRecord.cost_usd), 0.0).label("cost_usd"),
                _P50.label("p50_ms"),
                _P95.label("p95_ms"),
            )
            .where(RequestRecord.ts >= since, RequestRecord.provider_served.isnot(None))
            .group_by(RequestRecord.provider_served)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        {
            "provider": r.provider_served,
            "requests": r.requests,
            "cost_usd": round(float(r.cost_usd), 6),
            "p50_ms": _num(r.p50_ms),
            "p95_ms": _num(r.p95_ms),
        }
        for r in rows
    ]


async def _by_policy(session, since) -> list[dict]:
    rows = (
        await session.execute(
            select(
                RequestRecord.route_policy,
                func.count().label("requests"),
                func.coalesce(func.sum(RequestRecord.cost_usd), 0.0).label("cost_usd"),
                func.coalesce(func.sum(RequestRecord.tokens_in), 0).label("tokens_in"),
                func.coalesce(func.sum(RequestRecord.tokens_out), 0).label("tokens_out"),
            )
            .where(RequestRecord.ts >= since)
            .group_by(RequestRecord.route_policy)
            .order_by(RequestRecord.route_policy)
        )
    ).all()
    return [
        {
            "policy": r.route_policy,
            "requests": r.requests,
            "cost_usd": round(float(r.cost_usd), 6),
            "tokens_in": int(r.tokens_in),
            "tokens_out": int(r.tokens_out),
        }
        for r in rows
    ]


async def _latency_by_cache(session, since) -> list[dict]:
    """The cache benchmark's headline number, straight from the request log."""
    rows = (
        await session.execute(
            select(
                RequestRecord.cache_hit,
                func.count().label("requests"),
                _P50.label("p50_ms"),
                _P95.label("p95_ms"),
            )
            .where(RequestRecord.ts >= since, RequestRecord.status == "ok")
            .group_by(RequestRecord.cache_hit)
            .order_by(RequestRecord.cache_hit)
        )
    ).all()
    return [
        {
            "cache_hit": bool(r.cache_hit),
            "requests": r.requests,
            "p50_ms": _num(r.p50_ms),
            "p95_ms": _num(r.p95_ms),
        }
        for r in rows
    ]


def _num(v) -> float | None:
    return round(float(v), 2) if v is not None else None
