from aiocache import caches

from .config import settings

# Build cache configuration
cache_config = {
    "cache": "aiocache.RedisCache",
    "endpoint": settings.REDIS_HOST,
    "port": settings.REDIS_PORT,
    "db": settings.REDIS_DB,
    "password": settings.REDIS_PASSWORD or None,
    "serializer": {
        "class": "aiocache.serializers.PickleSerializer"
    },
}

# Add SSL for ElastiCache Serverless (cluster mode)
if settings.REDIS_CLUSTER_MODE:
    cache_config["ssl"] = True

# Configure aiocache with Redis backend
caches.set_config({"default": cache_config})


def get_cache():
    """Get the default cache instance."""
    return caches.get("default")
