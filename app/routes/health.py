from fastapi import APIRouter, Request, Response
from sqlalchemy import text

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Liveness. Deliberately dependency-free: the process is up."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request, response: Response) -> dict:
    """Readiness. Postgres is required (nothing can be accounted for without it);
    Redis is not — losing the cache degrades the gateway, it does not break it."""
    app = request.app
    out = {"postgres": "down", "redis": "down"}

    if app.state.sessionmaker is not None:
        try:
            async with app.state.sessionmaker() as session:
                await session.execute(text("SELECT 1"))
            out["postgres"] = "up"
        except Exception as e:  # noqa: BLE001
            out["postgres_error"] = str(e)[:200]

    if app.state.redis is not None:
        try:
            await app.state.redis.ping()
            out["redis"] = "up"
        except Exception as e:  # noqa: BLE001
            out["redis_error"] = str(e)[:200]

    out["status"] = "ready" if out["postgres"] == "up" else "not_ready"
    if out["status"] != "ready":
        response.status_code = 503
    return out
