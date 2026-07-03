import asyncpg
from fastapi import Header, HTTPException

from app.db.postgres import get_pool


async def get_tenant(pool: asyncpg.Pool, tenant_id: str) -> dict | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM tenants WHERE tenant_id = $1", tenant_id
        )
    if row is None:
        return None
    return dict(row)


async def require_tenant(x_tenant_id: str | None = Header(None)) -> dict:
    if x_tenant_id is None:
        raise HTTPException(status_code=400, detail="X-Tenant-Id header is required")

    pool = get_pool()
    tenant = await get_tenant(pool, x_tenant_id)
    if tenant is None:
        raise HTTPException(status_code=403, detail="Unknown tenant")

    return tenant
