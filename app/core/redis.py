"""Redis client using redis-py (asyncio) for ElastiCache/Redis compatibility."""

from typing import Optional, Any
import json

import redis.asyncio as aioredis

from app.core.config import settings
from app.core.logging import logger, tracer


class RedisClient:
    """Async Redis client using redis-py.

    Supports both standalone and cluster modes (ElastiCache Serverless) with TLS.
    """

    def __init__(self) -> None:
        """Initialize Redis client."""
        self._client: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        """Create Redis connection using redis-py."""
        with tracer.start_as_current_span(
            "redis.connect",
            attributes={
                "redis.host": settings.REDIS_HOST,
                "redis.port": settings.REDIS_PORT,
                "redis.cluster_mode": settings.REDIS_CLUSTER_MODE,
            },
        ):
            try:
                mode = "cluster" if settings.REDIS_CLUSTER_MODE else "standalone"
                logger.info(
                    f"Connecting to Redis ({mode}) at {settings.REDIS_HOST}:{settings.REDIS_PORT}"
                )

                # Build connection parameters
                connection_kwargs = {
                    "host": settings.REDIS_HOST,
                    "port": settings.REDIS_PORT,
                    "db": settings.REDIS_DB,
                    "socket_timeout": settings.REDIS_SOCKET_TIMEOUT,
                    "socket_connect_timeout": settings.REDIS_SOCKET_CONNECT_TIMEOUT,
                    "max_connections": settings.REDIS_MAX_CONNECTIONS,
                    "decode_responses": True,
                }

                # Add SSL for cluster mode (ElastiCache Serverless)
                if settings.REDIS_CLUSTER_MODE:
                    connection_kwargs["ssl"] = True

                # Add password if configured
                if settings.REDIS_PASSWORD:
                    connection_kwargs["password"] = settings.REDIS_PASSWORD

                # Create Redis client
                self._client = aioredis.Redis(**connection_kwargs)

                # Test connection
                await self._client.ping()

                tls_status = "with TLS" if settings.REDIS_CLUSTER_MODE else "without TLS"
                logger.info(
                    f"Connected to Redis ({mode}) at {settings.REDIS_HOST}:{settings.REDIS_PORT} {tls_status}"
                )

            except Exception as e:
                logger.error(f"Failed to connect to Redis: {e}")
                raise

    async def disconnect(self) -> None:
        """Close Redis connection."""
        with tracer.start_as_current_span("redis.disconnect"):
            if self._client:
                await self._client.close()
                logger.info("Disconnected from Redis")

    async def ensure_connected(self) -> None:
        """Connect if not already connected."""
        if not self.is_connected:
            await self.connect()

    @property
    def client(self) -> aioredis.Redis:
        """Get Redis client instance."""
        if not self._client:
            raise RuntimeError("Redis client not initialized. Call connect() first.")
        return self._client

    @property
    def is_connected(self) -> bool:
        """Check if Redis client is initialized."""
        return self._client is not None

    async def health_check(self) -> bool:
        """Check Redis connection health."""
        try:
            await self.client.ping()
            return True
        except Exception as e:
            logger.error(f"Redis health check failed: {e}")
            return False

    async def set_if_not_exists(self, key: str, value: Any, ttl_seconds: int) -> bool:
        """Atomically set a key with TTL only if it does not exist."""
        await self.ensure_connected()
        res = await self.client.set(name=key, value=value, nx=True, ex=int(ttl_seconds))
        return bool(res)

    # ── Billing-specific cache helpers ──

    async def get_cached_json(self, key: str) -> Optional[dict]:
        """Get a JSON-serialized cached value."""
        await self.ensure_connected()
        raw = await self.client.get(key)
        if raw is None:
            return None
        return json.loads(raw)

    async def set_cached_json(self, key: str, value: dict, ttl_seconds: int) -> None:
        """Set a JSON-serialized cached value with TTL."""
        await self.ensure_connected()
        await self.client.setex(key, ttl_seconds, json.dumps(value))

    async def delete_cached(self, key: str) -> None:
        """Delete a cached key."""
        await self.ensure_connected()
        await self.client.delete(key)

    async def increment_float(self, key: str, amount: float) -> float:
        """Increment a float counter (for real-time usage tracking)."""
        await self.ensure_connected()
        return await self.client.incrbyfloat(key, amount)

    async def get_float(self, key: str) -> float:
        """Get a float counter value."""
        await self.ensure_connected()
        val = await self.client.get(key)
        return float(val) if val else 0.0

    async def set_with_ttl(self, key: str, value: str, ttl_seconds: int) -> None:
        """Set a string value with TTL."""
        await self.ensure_connected()
        await self.client.setex(key, ttl_seconds, value)


# Global Redis client instance
redis_client = RedisClient()


async def get_redis() -> aioredis.Redis:
    """Dependency injection for Redis client."""
    return redis_client.client
