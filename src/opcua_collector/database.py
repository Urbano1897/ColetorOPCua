"""Create the cycle table and insert one row per shot.

The table is owned by this process: it is created on startup, and new
signals in the config are added with ALTER TABLE. Columns that disappear
from the config are left in place so a typo cannot drop history.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

import psycopg
from psycopg import sql

from opcua_collector.config import AppConfig, SignalConfig, ValueType
from opcua_collector.logging import get_logger

_log = get_logger(__name__)

_PG_TYPES = {
    ValueType.INTEGER: "BIGINT",
    ValueType.FLOAT: "DOUBLE PRECISION",
    ValueType.BOOLEAN: "BOOLEAN",
    ValueType.STRING: "TEXT",
}


class DatabaseUnavailable(Exception):
    """PostgreSQL cannot take a write right now. The collector retries."""


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    pg_type: str
    comment: str


@dataclass(frozen=True)
class TableSchema:
    table: str
    timestamp: ColumnSpec
    cycle: ColumnSpec
    signals: tuple[ColumnSpec, ...]

    @property
    def data_columns(self) -> tuple[ColumnSpec, ...]:
        return (self.timestamp, self.cycle, *self.signals)


@dataclass(frozen=True)
class Sample:
    """One stored shot. `values` is keyed by column name and may contain None."""

    sampled_at: datetime
    cycle_number: int
    values: Mapping[str, object]


class SampleWriter(Protocol):
    async def open(self) -> None:
        """Open the database, or do nothing when it is already open."""

    async def ensure_schema(self, schema: TableSchema) -> None:
        """Create the table and any columns that the config gained."""

    async def insert_sample(self, schema: TableSchema, sample: Sample) -> None:
        """Insert one row. Raise DatabaseUnavailable when the connection is dead."""

    async def close(self) -> None:
        """Close the connection. Must be safe to call more than once."""


def schema_from_config(config: AppConfig) -> TableSchema:
    return TableSchema(
        table=config.database.table,
        timestamp=ColumnSpec(
            name=config.database.timestamp_column,
            pg_type="TIMESTAMPTZ",
            comment="UTC time when the collector read this cycle",
        ),
        cycle=ColumnSpec(
            name=config.trigger.column,
            pg_type="BIGINT",
            comment="Cycle-counter value that triggered this row",
        ),
        signals=tuple(_signal_column(signal) for signal in config.nodes),
    )


def sample_parameters(schema: TableSchema, sample: Sample) -> tuple[list[str], tuple[object, ...]]:
    """Column names and values in the order the INSERT statement uses."""
    if sample.sampled_at.tzinfo is None:
        raise ValueError("sampled_at must be timezone-aware")
    names = [column.name for column in schema.data_columns]
    signal_values = [sample.values.get(column.name) for column in schema.signals]
    values = (sample.sampled_at, sample.cycle_number, *signal_values)
    return names, values


class PostgresWriter:
    """One connection, reopened after a connection error.

    autocommit is on because each shot is its own transaction: a failure
    must not roll back a previous shot that was already stored.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: psycopg.AsyncConnection | None = None

    async def open(self) -> None:
        if self._conn is not None and not self._conn.closed:
            return
        try:
            self._conn = await psycopg.AsyncConnection.connect(
                self._dsn,
                autocommit=True,
                connect_timeout=10,
            )
        except (psycopg.OperationalError, psycopg.InterfaceError, OSError) as exc:
            raise DatabaseUnavailable(_safe_error(exc, self._dsn)) from exc
        _log.info("Connected to PostgreSQL", **_dsn_target(self._dsn))

    async def ensure_schema(self, schema: TableSchema) -> None:
        await self._execute(_create_table(schema))
        for column in schema.signals:
            await self._execute(_add_column(schema.table, column))
        for column in schema.data_columns:
            await self._execute(_comment(schema.table, column))
        await self._execute(_index(schema.table, schema.timestamp.name))
        await self._execute(_index(schema.table, schema.cycle.name))
        _log.info(
            "Database schema is ready",
            table=schema.table,
            signals=len(schema.signals),
        )

    async def insert_sample(self, schema: TableSchema, sample: Sample) -> None:
        names, values = sample_parameters(schema, sample)
        statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
            sql.Identifier(schema.table),
            sql.SQL(", ").join(sql.Identifier(name) for name in names),
            sql.SQL(", ").join(sql.Placeholder() for _ in names),
        )
        await self._execute(statement, values)

    async def close(self) -> None:
        connection = self._conn
        self._conn = None
        if connection is None or connection.closed:
            return
        await connection.close()

    async def _execute(self, statement: sql.Composable, params: tuple[object, ...] = ()) -> None:
        if self._conn is None or self._conn.closed:
            raise DatabaseUnavailable("PostgreSQL connection is not open")
        try:
            await self._conn.execute(statement, params)
        except (psycopg.OperationalError, psycopg.InterfaceError, OSError) as exc:
            # Drop the socket so the next open() actually reconnects.
            await self.close()
            raise DatabaseUnavailable(_safe_error(exc, self._dsn)) from exc


def _signal_column(signal: SignalConfig) -> ColumnSpec:
    comment = signal.description
    if signal.unit:
        comment = f"{comment} [{signal.unit}]" if comment else signal.unit
    if not comment:
        comment = signal.column
    return ColumnSpec(name=signal.column, pg_type=_PG_TYPES[signal.value_type], comment=comment)


def _create_table(schema: TableSchema) -> sql.Composable:
    # id is the only column this program does not take from the config.
    # The timestamp and the cycle counter are NOT NULL because a row without
    # them is not a sample. Signal columns are added afterwards and stay
    # nullable: a bad OPC UA read stores NULL instead of dropping the shot.
    return sql.SQL(
        "CREATE TABLE IF NOT EXISTS {} ("
        "id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
        "{} TIMESTAMPTZ NOT NULL, "
        "{} BIGINT NOT NULL)"
    ).format(
        sql.Identifier(schema.table),
        sql.Identifier(schema.timestamp.name),
        sql.Identifier(schema.cycle.name),
    )


def _add_column(table: str, column: ColumnSpec) -> sql.Composable:
    # pg_type comes from our own map, never from the config text.
    return sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} {}").format(
        sql.Identifier(table),
        sql.Identifier(column.name),
        sql.SQL(column.pg_type),
    )


def _comment(table: str, column: ColumnSpec) -> sql.Composable:
    return sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
        sql.Identifier(table),
        sql.Identifier(column.name),
        sql.Literal(column.comment),
    )


def _index(table: str, column: str) -> sql.Composable:
    # 63 bytes is the PostgreSQL identifier limit.
    name = f"ix_{table}_{column}"[:63]
    return sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} ({})").format(
        sql.Identifier(name),
        sql.Identifier(table),
        sql.Identifier(column),
    )


def _dsn_target(dsn: str) -> dict[str, object]:
    parsed = urlsplit(dsn)
    return {
        "host": parsed.hostname or "",
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
    }


def _safe_error(exc: BaseException, dsn: str) -> str:
    message = f"{type(exc).__name__}: {exc}"
    password = urlsplit(dsn).password
    if password:
        message = message.replace(password, "***")
    return message


def utcnow() -> datetime:
    return datetime.now(UTC)
