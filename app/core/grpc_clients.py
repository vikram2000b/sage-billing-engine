"""gRPC client for DatabaseAccess service."""

import logging
from typing import Optional
from datetime import date, datetime, timezone
import grpc

from .config import settings

# Generated proto imports - run `buf generate` to generate these
from sagepilot.databaseaccess import databaseaccess_pb2, databaseaccess_pb2_grpc

logger = logging.getLogger(__name__)


class DatabaseAccessClient:
    """Client for the DatabaseAccess gRPC service."""

    _channel: Optional[grpc.aio.Channel] = None
    _stub: Optional[databaseaccess_pb2_grpc.DatabaseAccessStub] = None

    @classmethod
    def _get_address(cls) -> str:
        """Get the gRPC server address."""
        return (
            f"{settings.DATABASE_ACCESS_GRPC_HOST}:{settings.DATABASE_ACCESS_GRPC_PORT}"
        )

    @classmethod
    async def get_stub(cls) -> databaseaccess_pb2_grpc.DatabaseAccessStub:
        """Get or create the gRPC stub."""
        if cls._stub is None:
            address = cls._get_address()
            # Increase max message size to 50MB for large query responses
            options = [
                ("grpc.max_receive_message_length", 50 * 1024 * 1024),  # 50MB
                ("grpc.max_send_message_length", 50 * 1024 * 1024),  # 50MB
            ]
            cls._channel = grpc.aio.insecure_channel(address, options=options)
            cls._stub = databaseaccess_pb2_grpc.DatabaseAccessStub(cls._channel)
            logger.info(f"DatabaseAccess gRPC client connected to {address}")
        return cls._stub

    @classmethod
    async def close(cls) -> None:
        """Close the gRPC channel."""
        if cls._channel is not None:
            await cls._channel.close()
            cls._channel = None
            cls._stub = None
            logger.info("DatabaseAccess gRPC client closed")

    @classmethod
    async def query(
        cls,
        sql: str,
        args: list = None,
        use_replica: bool = False,
    ) -> databaseaccess_pb2.QueryResponse:
        """Execute a SQL query and return multiple rows."""
        stub = await cls.get_stub()
        request = databaseaccess_pb2.QueryRequest(
            sql=sql,
            args=_convert_args_to_values(args or []),
            use_replica=use_replica,
        )
        return await stub.Query(request)

    @classmethod
    async def query_one(
        cls,
        sql: str,
        args: list = None,
        use_replica: bool = False,
    ) -> databaseaccess_pb2.RowResponse:
        """Execute a SQL query and return a single row."""
        stub = await cls.get_stub()
        request = databaseaccess_pb2.QueryRequest(
            sql=sql,
            args=_convert_args_to_values(args or []),
            use_replica=use_replica,
        )
        return await stub.QueryOne(request)

    @classmethod
    async def query_value(
        cls,
        sql: str,
        args: list = None,
        use_replica: bool = False,
    ) -> databaseaccess_pb2.ValueResponse:
        """Execute a SQL query and return a single value."""
        stub = await cls.get_stub()
        request = databaseaccess_pb2.QueryRequest(
            sql=sql,
            args=_convert_args_to_values(args or []),
            use_replica=use_replica,
        )
        return await stub.QueryValue(request)

    @classmethod
    async def execute(
        cls,
        sql: str,
        args: list = None,
    ) -> databaseaccess_pb2.ExecuteResponse:
        """Execute a SQL statement (INSERT, UPDATE, DELETE)."""
        stub = await cls.get_stub()
        request = databaseaccess_pb2.ExecuteRequest(
            sql=sql,
            args=_convert_args_to_values(args or []),
        )
        return await stub.Execute(request)

    @classmethod
    async def bulk_insert(
        cls,
        sql: str,
        rows: list[list],
    ) -> databaseaccess_pb2.ExecuteResponse:
        """Execute a bulk insert operation."""
        stub = await cls.get_stub()

        proto_rows = []
        for row_data in rows:
            columns = []
            for i, value in enumerate(row_data):
                columns.append(
                    databaseaccess_pb2.Column(
                        name=str(i),
                        value=_python_to_value(value),
                    )
                )
            proto_rows.append(databaseaccess_pb2.Row(columns=columns))

        request = databaseaccess_pb2.BulkInsertRequest(
            sql=sql,
            rows=proto_rows,
        )
        return await stub.BulkInsert(request)

    @classmethod
    async def health(cls) -> databaseaccess_pb2.HealthResponse:
        """Check the health of the database service."""
        stub = await cls.get_stub()
        request = databaseaccess_pb2.HealthRequest()
        return await stub.Health(request)


def _python_to_value(value) -> databaseaccess_pb2.Value:
    """Convert a Python value to a protobuf Value."""
    def _datetime_to_rfc3339(dt: datetime) -> str:
        """
        Encode datetimes as RFC3339 (UTC) so downstream services can reliably parse
        them back into real datetime objects for timestamp query parameters.
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(timezone.utc)
        timespec = "microseconds" if dt.microsecond else "seconds"
        s = dt.isoformat(timespec=timespec)
        # Prefer the canonical UTC "Z" suffix.
        if s.endswith("+00:00"):
            s = s[:-6] + "Z"
        return s

    if value is None:
        return databaseaccess_pb2.Value(null_value=True)
    elif isinstance(value, bool):
        return databaseaccess_pb2.Value(bool_value=value)
    elif isinstance(value, datetime):
        # Use timestamp_value for datetime objects
        return databaseaccess_pb2.Value(timestamp_value=_datetime_to_rfc3339(value))
    elif isinstance(value, date):
        # Use timestamp_value for date objects
        return databaseaccess_pb2.Value(timestamp_value=value.isoformat())
    elif isinstance(value, int):
        return databaseaccess_pb2.Value(int_value=value)
    elif isinstance(value, float):
        return databaseaccess_pb2.Value(float_value=value)
    elif isinstance(value, bytes):
        return databaseaccess_pb2.Value(bytes_value=value)
    elif isinstance(value, str):
        return databaseaccess_pb2.Value(string_value=value)
    elif isinstance(value, (list, dict)):
        import json

        return databaseaccess_pb2.Value(json_value=json.dumps(value))
    else:
        # Default: convert to string
        return databaseaccess_pb2.Value(string_value=str(value))


def _convert_args_to_values(args: list) -> list[databaseaccess_pb2.Value]:
    """Convert a list of Python values to protobuf Values."""
    return [_python_to_value(arg) for arg in args]


def _value_to_python(value: databaseaccess_pb2.Value):
    """Convert a protobuf Value to a Python value."""
    kind = value.WhichOneof("kind")
    if kind == "null_value":
        return None
    elif kind == "string_value":
        return value.string_value
    elif kind == "int_value":
        return value.int_value
    elif kind == "float_value":
        return value.float_value
    elif kind == "bool_value":
        return value.bool_value
    elif kind == "bytes_value":
        return value.bytes_value
    elif kind == "json_value":
        import json

        return json.loads(value.json_value)
    elif kind == "uuid_value":
        return value.uuid_value
    elif kind == "timestamp_value":
        from datetime import datetime
        # Parse ISO format string back to datetime object
        return datetime.fromisoformat(value.timestamp_value)
    elif kind == "array_value":
        import json

        return json.loads(value.array_value)
    else:
        return None


def row_to_dict(row: databaseaccess_pb2.Row) -> dict:
    """Convert a protobuf Row to a Python dict."""
    return {col.name: _value_to_python(col.value) for col in row.columns}


def rows_to_dicts(rows: list[databaseaccess_pb2.Row]) -> list[dict]:
    """Convert a list of protobuf Rows to a list of Python dicts."""
    return [row_to_dict(row) for row in rows]
