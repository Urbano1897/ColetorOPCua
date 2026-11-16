from __future__ import annotations

from pathlib import Path

import pytest

from opcua_collector.config import ConfigError, load_config
from opcua_collector.simulator.catalog import collected_nodes, node_id, trigger_node

ROOT = Path(__file__).resolve().parents[1]
DSN = "postgresql://collector:collector@localhost:5432/machine_data"

BASE = """
opcua:
  endpoint: opc.tcp://127.0.0.1:4840/industrial/simulator
database:
  dsn_env: DATABASE_URL
trigger:
  node_id: ns=2;s=Machine.CycleCounter
  column: cycle_number
nodes:
  - node_id: ns=2;s=Machine.MeltTemperature
    column: melt_temperature
    type: float
    unit: "°C"
    description: Melt temperature
"""


def _load(tmp_path: Path, text: str, monkeypatch: pytest.MonkeyPatch, **env: str):
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("OPCUA_USERNAME", raising=False)
    monkeypatch.delenv("OPCUA_PASSWORD", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_example_and_docker_configs_match_the_simulator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", DSN)
    monkeypatch.delenv("OPCUA_USERNAME", raising=False)
    monkeypatch.delenv("OPCUA_PASSWORD", raising=False)

    example = load_config(ROOT / "config.example.yaml")
    docker = load_config(ROOT / "deploy/config.docker.yaml")

    assert example.opcua.endpoint == "opc.tcp://127.0.0.1:4840/industrial/simulator"
    assert docker.opcua.endpoint == "opc.tcp://simulator:4840/industrial/simulator"
    assert example.opcua.username is None
    assert example.database.dsn == DSN

    for config in (example, docker):
        assert config.trigger.node_id == node_id(trigger_node().identifier)
        assert config.trigger.column == trigger_node().column
        collected = collected_nodes()
        assert len(config.nodes) == len(collected)
        for signal, spec in zip(config.nodes, collected, strict=True):
            assert signal.node_id == node_id(spec.identifier)
            assert signal.column == spec.column
            assert signal.value_type.value == spec.value_type
            assert signal.unit == spec.unit
            assert signal.description == spec.description


def test_username_and_password_come_from_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = BASE.replace(
        "endpoint: opc.tcp://127.0.0.1:4840/industrial/simulator",
        "endpoint: opc.tcp://127.0.0.1:4840/industrial/simulator\n"
        "  username_env: OPCUA_USERNAME\n"
        "  password_env: OPCUA_PASSWORD",
    )
    config = _load(
        tmp_path,
        text,
        monkeypatch,
        OPCUA_USERNAME="operator",
        OPCUA_PASSWORD="local-only",
    )
    assert config.opcua.username == "operator"
    assert config.opcua.password == "local-only"


def test_password_without_username_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = BASE.replace(
        "endpoint: opc.tcp://127.0.0.1:4840/industrial/simulator",
        "endpoint: opc.tcp://127.0.0.1:4840/industrial/simulator\n"
        "  username_env: OPCUA_USERNAME\n"
        "  password_env: OPCUA_PASSWORD",
    )
    with pytest.raises(ConfigError, match="OPCUA_USERNAME"):
        _load(tmp_path, text, monkeypatch, OPCUA_PASSWORD="local-only")


def test_missing_database_url_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(BASE, encoding="utf-8")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        load_config(path)


def test_uppercase_column_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = BASE.replace("column: melt_temperature", "column: MeltTemperature")
    with pytest.raises(ConfigError, match="lowercase SQL identifier"):
        _load(tmp_path, text, monkeypatch)


def test_duplicate_column_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = BASE.replace("column: cycle_number", "column: melt_temperature")
    with pytest.raises(ConfigError, match="used by both"):
        _load(tmp_path, text, monkeypatch)


def test_trigger_cannot_also_be_a_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = BASE.replace("ns=2;s=Machine.MeltTemperature", "ns=2;s=Machine.CycleCounter")
    with pytest.raises(ConfigError, match="trigger node"):
        _load(tmp_path, text, monkeypatch)


def test_unknown_field_and_bad_endpoint_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigError, match="Unknown fields"):
        _load(tmp_path, BASE + "\nnot_a_real_section: 1\n", monkeypatch)
    edited = BASE.replace("opc.tcp://127.0.0.1:4840/industrial/simulator", "http://localhost")
    with pytest.raises(ConfigError, match=r"opc\.tcp"):
        _load(tmp_path, edited, monkeypatch)


def test_integer_boolean_and_string_types_are_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = """
opcua:
  endpoint: opc.tcp://127.0.0.1:4840/industrial/simulator
database:
  dsn_env: DATABASE_URL
trigger:
  node_id: ns=2;s=Machine.CycleCounter
  column: cycle_number
nodes:
  - node_id: ns=2;s=Machine.Cavities
    column: cavities
    type: integer
  - node_id: ns=2;s=Machine.AlarmActive
    column: alarm_active
    type: boolean
  - node_id: ns=2;s=Machine.PartQuality
    column: part_quality
    type: string
"""
    config = _load(tmp_path, text, monkeypatch)
    assert [node.value_type.value for node in config.nodes] == ["integer", "boolean", "string"]
