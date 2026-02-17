import logging
from contextlib import contextmanager
from typing import Any, Generator, List, Optional, Type

from .config import settings
from .grpc_clients import DatabaseAccessClient, rows_to_dicts

logger = logging.getLogger(__name__)


class Database:
    """Database access wrapper using the DatabaseAccess gRPC service."""

    @classmethod
    async def connect(cls) -> None:
        """Initialize the gRPC connection (called on startup)."""
        await DatabaseAccessClient.get_stub()
        logger.info("Database gRPC client initialized")

    @classmethod
    async def close(cls) -> None:
        """Close the gRPC connection (called on shutdown)."""
        await DatabaseAccessClient.close()

    @classmethod
    async def execute_query(
        cls, query: str, *args: Any, fetch: bool = True, use_replica: bool = False
    ) -> Optional[List[dict]]:
        """
        Execute a SQL query and return results if any.

        Args:
            query: SQL query string
            *args: Query parameters
            fetch: If True, returns the results. If False, just executes the query.
            use_replica: If True, routes read queries to replicas (only for fetch=True).

        Returns:
            List of dicts if fetch=True and query returns results
            None if fetch=False or query doesn't return results
        """
        try:
            if fetch:
                response = await DatabaseAccessClient.query(
                    sql=query,
                    args=list(args),
                    use_replica=use_replica,
                )
                return rows_to_dicts(response.rows)
            else:
                await DatabaseAccessClient.execute(
                    sql=query,
                    args=list(args),
                )
                return None

        except Exception as e:
            logger.error(f"Database error: {str(e)}\nQuery: {query}\nArgs: {args}")
            raise

    @classmethod
    async def bulk_insert(cls, query: str, data: List[tuple]) -> None:
        """
        Execute bulk insert using the gRPC service.

        Args:
            query: SQL INSERT query string with placeholders
            data: List of tuples containing the data to insert

        Example:
            await Database.bulk_insert(
                "INSERT INTO users (name, email) VALUES ($1, $2)",
                [("John", "john@example.com"), ("Jane", "jane@example.com")]
            )
        """
        if not data:
            return

        try:
            # Convert tuples to lists for the gRPC call
            rows = [list(row) for row in data]
            await DatabaseAccessClient.bulk_insert(sql=query, rows=rows)
            logger.info(f"Bulk inserted {len(data)} records")

        except Exception as e:
            logger.error(
                f"Bulk insert error: {str(e)}\nQuery: {query}\nData count: {len(data)}"
            )
            raise

    # Deprecated methods for backwards compatibility
    @classmethod
    async def get_pool(cls) -> None:
        """Deprecated: Use connect() instead."""
        logger.warning("get_pool() is deprecated. Use connect() instead.")
        await cls.connect()
        return None

    @classmethod
    async def close_pool(cls) -> None:
        """Deprecated: Use close() instead."""
        logger.warning("close_pool() is deprecated. Use close() instead.")
        await cls.close()


# Usage examples:
"""
# In FastAPI startup:
@app.on_event("startup")
async def startup():
    await Database.connect()

@app.on_event("shutdown")
async def shutdown():
    await Database.close()

# In your routes:
@app.get("/users")
async def get_users():
    # SELECT query (fetch=True by default, use_replica=True to route to replicas)
    users = await Database.execute_query("SELECT * FROM users", use_replica=True)
    return users

@app.post("/users")
async def create_user(user: UserCreate):
    # INSERT query (fetch=False since we don't need results)
    await Database.execute_query(
        "INSERT INTO users (name, email) VALUES ($1, $2)",
        user.name,
        user.email,
        fetch=False
    )
    return {"message": "User created"}

@app.get("/user/{user_id}")
async def get_user(user_id: int):
    # SELECT query for single user (use_replica=True for read queries)
    users = await Database.execute_query(
        "SELECT * FROM users WHERE id = $1",
        user_id,
        use_replica=True
    )
    return users[0] if users else None
"""
