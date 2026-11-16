from datetime import UTC, datetime
from pathlib import Path

import pytest

from opcua_collector.config import load_config
from opcua_collector.database import (
    ColumnSpec,
    Sample,
    TableSchema,
    _safe_error,
    sample_parameters,
    schema_from_config,
)
from opcua_collector.simulator.catalog import collected_nodes

ROOT = Path(__file__).resolve().parents[1]
DSN = "postgresql://collector:collector@localhost:5432/machine_data"


def test_example_schema_maps_signal_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("OPCUA_USERNAME", raising=False)
    monkeypatch.delenv("OPCUA_PASSWORD", raising=False)
    schema = schema_from_config(load_config(ROOT / "config.example.yaml"))
    assert schema.table == "cycle_samples"
    assert schema.timestamp.pg_type == "TIMESTAMPTZ"
    assert schema.cycle.pg_type == "BIGINT"
    by_column = {column.name: column for column in schema.signals}
    assert by_column["melt_temperature"].pg_type == "DOUBLE PRECISION"
    assert by_column["melt_temperature"].comment.endswith("[°C]")
    assert by_column["part_quality"].pg_type == "TEXT"
    assert by_column["alarm_active"].pg_type == "BOOLEAN"
    assert [column.name for column in schema.signals] == [node.column for node in collected_nodes()]


def test_sample_parameters_follow_the_schema_and_fill_missing_with_null() -> None:
    schema = TableSchema(
        table="cycle_samples",
        timestamp=ColumnSpec("sampled_at", "TIMESTAMPTZ", "when"),
        cycle=ColumnSpec("cycle_number", "BIGINT", "cycle"),
        signals=(ColumnSpec("melt_temperature", "DOUBLE PRECISION", "Melt [°C]"),),
    )
    sampled_at = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    names, values = sample_parameters(
        schema,
        Sample(sampled_at=sampled_at, cycle_number=3, values={}),
    )
    assert names == ["sampled_at", "cycle_number", "melt_temperature"]
    assert values == (sampled_at, 3, None)


def test_naive_timestamps_are_rejected() -> None:
    schema = TableSchema(
        table="cycle_samples",
        timestamp=ColumnSpec("sampled_at", "TIMESTAMPTZ", "when"),
        cycle=ColumnSpec("cycle_number", "BIGINT", "cycle"),
        signals=(),
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        sample_parameters(
            schema,
            Sample(sampled_at=datetime(2026, 11, 15, 12, 0), cycle_number=1, values={}),
        )


def test_database_errors_do_not_keep_the_password() -> None:
    message = _safe_error(
        RuntimeError("authentication failed for password s3cret"),
        "postgresql://collector:s3cret@localhost:5432/machine_data",
    )
    assert "s3cret" not in message
    assert "***" in message
