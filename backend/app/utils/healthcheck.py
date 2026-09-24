import json

from fastapi import APIRouter, Response
from sqlalchemy import text

from app.database import DbSession, async_engine, engine
from app.integrations.redis_client import get_redis_client

healthcheck_router = APIRouter()


def get_pool_status() -> dict[str, str]:
    """Get connection pool status for monitoring."""
    pool = engine.pool
    return {
        "max_pool_size": str(pool.size()),  # ty:ignore[unresolved-attribute]
        "connections_ready_for_reuse": str(pool.checkedin()),  # ty:ignore[unresolved-attribute]
        "active_connections": str(pool.checkedout()),  # ty:ignore[unresolved-attribute]
        "overflow": str(pool.overflow()),  # ty:ignore[unresolved-attribute]
    }


@healthcheck_router.get("")
async def health() -> dict[str, str]:
    """Liveness: Prozess lebt. Prüft bewusst KEINE Abhängigkeiten —
    sonst restartet k3s bei einem DB-Neustart die gesunde App mit."""
    return {"status": "ok"}


@healthcheck_router.get("/ready")
async def ready() -> Response:
    """Readiness: Abhängigkeiten erreichbar. Bei 503 nimmt k3s den Pod
    aus dem Serving, restartet ihn aber nicht."""
    checks: dict[str, str] = {}
    try:
        async with async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception:
        checks["db"] = "error"
    try:
        get_redis_client().ping()
        checks["redis"] = "ok"
    except Exception:
        checks["redis"] = "error"

    status = 200 if all(v == "ok" for v in checks.values()) else 503
    return Response(
        content=json.dumps(checks), status_code=status, media_type="application/json"
    )


@healthcheck_router.get("/db")
async def database_health(db: DbSession) -> dict[str, str | dict[str, str]]:
    """Database health check endpoint."""
    try:
        # Test connection
        db.execute(text("SELECT 1"))

        pool_status = get_pool_status()
        return {
            "status": "healthy",
            "pool": pool_status,
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
