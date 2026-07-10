from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.responses import JSONResponse

from app.core.admission import (
    check_db_load_shedding,
    check_partition_depth,
    check_rate_limit,
    check_tenant_quota,
)


def _async_pool(conn: AsyncMock) -> AsyncMock:
    pool = AsyncMock()

    @asynccontextmanager
    async def acquire():
        yield conn

    pool.acquire = acquire
    return pool


def _async_redis(pipe: AsyncMock) -> AsyncMock:
    redis = AsyncMock()

    @asynccontextmanager
    async def pipeline():
        yield pipe

    redis.pipeline = pipeline
    return redis


@pytest.mark.asyncio
async def test_rate_limit_returns_contract_error_shape() -> None:
    pipe = AsyncMock()
    pipe.incr = MagicMock(return_value=pipe)
    pipe.expire = MagicMock(return_value=pipe)
    pipe.execute = AsyncMock(return_value=[301, True])
    redis = _async_redis(pipe)

    tenant = {
        "tenant_id": "studio_a",
        "max_tickets_per_second": 300,
        "max_tickets_in_flight": 5000,
        "max_partition_depth": 1000,
    }

    response = await check_rate_limit(redis, tenant)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 429
    body = response.body.decode()
    assert "tenant_rate_limit_exceeded" in body
    assert "retry_after_seconds" in body


@pytest.mark.asyncio
async def test_tenant_quota_returns_contract_error_shape() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=5000)
    pool = _async_pool(conn)

    tenant = {
        "tenant_id": "studio_a",
        "max_tickets_per_second": 300,
        "max_tickets_in_flight": 5000,
        "max_partition_depth": 1000,
    }

    response = await check_tenant_quota(pool, tenant)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 429
    assert "tenant_rate_limit_exceeded" in response.body.decode()


@pytest.mark.asyncio
async def test_partition_depth_uses_tenant_limit() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1000)
    pool = _async_pool(conn)

    tenant = {
        "tenant_id": "studio_a",
        "max_tickets_per_second": 300,
        "max_tickets_in_flight": 5000,
        "max_partition_depth": 1000,
    }

    response = await check_partition_depth(pool, tenant, partition_id=42)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    body = response.body.decode()
    assert "partition_overloaded" in body
    assert "retry_after_seconds" in body


@pytest.mark.asyncio
async def test_load_shedding_returns_503_when_latency_high() -> None:
    conn = AsyncMock()
    conn.fetchval = AsyncMock(return_value=1)
    pool = _async_pool(conn)

    with patch("app.core.admission.settings") as mock_settings:
        mock_settings.load_shedding_enabled = True
        mock_settings.db_latency_shed_threshold_ms = 1
        with patch("app.core.admission.time.perf_counter", side_effect=[0.0, 1.0]):
            response = await check_db_load_shedding(pool)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert "partition_overloaded" in response.body.decode()


@pytest.mark.asyncio
async def test_load_shedding_skipped_when_disabled() -> None:
    pool = AsyncMock()

    with patch("app.core.admission.settings") as mock_settings:
        mock_settings.load_shedding_enabled = False
        response = await check_db_load_shedding(pool)

    assert response is None
