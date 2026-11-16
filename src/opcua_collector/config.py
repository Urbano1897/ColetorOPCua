"""Load the YAML configuration and resolve secrets from the environment.

Column names become SQL identifiers, so they are restricted to a safe
pattern and then quoted again at the database. The YAML file is never a
place for passwords: it only names the environment variables that hold them.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

import yaml

# PostgreSQL identifiers are at most 63 bytes. We stay inside ASCII so the
# limit is also the character limit, and we quote the names on top of this.
_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_RESERVED_COLUMNS = frozenset({"id"})


class ConfigError(ValueError):
    """The configuration file or its environment is not usable."""


class ValueType(StrEnum):
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    STRING = "string"


@dataclass(frozen=True)
class ReconnectConfig:
    initial_s: float
    max_s: float
    multiplier: float


@dataclass(frozen=True)
class OpcUaConfig:
    endpoint: str
    application_uri: str
    username: str | None
    password: str | None
    timeout_s: float
    session_timeout_ms: int
    publishing_interval_ms: float
    reconnect: ReconnectConfig


@dataclass(frozen=True)
class DatabaseConfig:
    dsn: str
    table: str
    timestamp_column: str
    reconnect: ReconnectConfig


@dataclass(frozen=True)
class TriggerConfig:
    node_id: str
    column: str


@dataclass(frozen=True)
class SignalConfig:
    node_id: str
    column: str
    value_type: ValueType
    unit: str | None
    description: str


@dataclass(frozen=True)
class AppConfig:
    opcua: OpcUaConfig
    database: DatabaseConfig
    trigger: TriggerConfig
    nodes: tuple[SignalConfig, ...]


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> AppConfig:
    """Read `path` and resolve secrets from `environ` (the process environment by default)."""
    env = os.environ if environ is None else environ
    document = _read_yaml(path)
    _reject_unknown(
        document,
        {"opcua", "database", "trigger", "nodes"},
        context=str(path),
    )
    opcua = _opcua(_require_mapping(document, "opcua"), env)
    database = _database(_require_mapping(document, "database"), env)
    trigger = _trigger(_require_mapping(document, "trigger"))
    nodes = _nodes(_require(document, "nodes"))
    _reject_duplicate_columns(database, trigger, nodes)
    return AppConfig(opcua=opcua, database=database, trigger=trigger, nodes=nodes)


def _read_yaml(path: Path) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def _opcua(raw: Mapping[str, object], env: Mapping[str, str]) -> OpcUaConfig:
    _reject_unknown(
        raw,
        {
            "endpoint",
            "application_uri",
            "username_env",
            "password_env",
            "timeout_s",
            "session_timeout_ms",
            "publishing_interval_ms",
            "reconnect",
        },
        context="opcua",
    )
    endpoint = _require_str(raw, "endpoint", context="opcua")
    _require_opc_tcp(endpoint)
    username, password = _credentials(raw, env)
    return OpcUaConfig(
        endpoint=endpoint,
        application_uri=_optional_str(raw, "application_uri", "urn:opcua-collector:client"),
        username=username,
        password=password,
        timeout_s=_optional_float(raw, "timeout_s", 10.0, minimum=0.1),
        session_timeout_ms=int(_optional_float(raw, "session_timeout_ms", 60_000, minimum=1_000)),
        publishing_interval_ms=_optional_float(raw, "publishing_interval_ms", 200.0, minimum=50.0),
        reconnect=_reconnect(raw.get("reconnect"), context="opcua.reconnect"),
    )


def _database(raw: Mapping[str, object], env: Mapping[str, str]) -> DatabaseConfig:
    _reject_unknown(
        raw,
        {"dsn_env", "table", "timestamp_column", "reconnect"},
        context="database",
    )
    dsn_env = _optional_str(raw, "dsn_env", "DATABASE_URL")
    _require_env_name(dsn_env, context="database.dsn_env")
    dsn = env.get(dsn_env, "").strip()
    if not dsn:
        raise ConfigError(
            f"Environment variable {dsn_env} is empty. "
            "Set it to a postgresql:// connection string. See .env.example."
        )
    scheme = urlsplit(dsn).scheme
    if scheme not in {"postgresql", "postgres"}:
        raise ConfigError(f"{dsn_env} must start with postgresql:// or postgres://")
    return DatabaseConfig(
        dsn=dsn,
        table=_identifier(_optional_str(raw, "table", "cycle_samples"), context="database.table"),
        timestamp_column=_identifier(
            _optional_str(raw, "timestamp_column", "sampled_at"),
            context="database.timestamp_column",
        ),
        reconnect=_reconnect(raw.get("reconnect"), context="database.reconnect"),
    )


def _trigger(raw: Mapping[str, object]) -> TriggerConfig:
    _reject_unknown(raw, {"node_id", "column"}, context="trigger")
    return TriggerConfig(
        node_id=_node_id(
            _require_str(raw, "node_id", context="trigger"),
            context="trigger.node_id",
        ),
        column=_identifier(
            _require_str(raw, "column", context="trigger"),
            context="trigger.column",
        ),
    )


def _nodes(raw: object) -> tuple[SignalConfig, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("nodes must be a non-empty list")
    nodes: list[SignalConfig] = []
    for index, item in enumerate(raw):
        context = f"nodes[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{context} must be a mapping")
        _reject_unknown(item, {"node_id", "column", "type", "unit", "description"}, context=context)
        unit = item.get("unit")
        description = item.get("description")
        nodes.append(
            SignalConfig(
                node_id=_node_id(
                    _require_str(item, "node_id", context=context),
                    context=f"{context}.node_id",
                ),
                column=_identifier(
                    _require_str(item, "column", context=context),
                    context=f"{context}.column",
                ),
                value_type=_value_type(
                    _require_str(item, "type", context=context),
                    context=f"{context}.type",
                ),
                unit=_optional_text(unit, context=f"{context}.unit"),
                description=_optional_text(description, context=f"{context}.description") or "",
            )
        )
    return tuple(nodes)


def _credentials(
    raw: Mapping[str, object],
    env: Mapping[str, str],
) -> tuple[str | None, str | None]:
    username_env = raw.get("username_env")
    password_env = raw.get("password_env")
    if username_env is None and password_env is None:
        return None, None
    if not isinstance(username_env, str) or not isinstance(password_env, str):
        raise ConfigError(
            "opcua.username_env and opcua.password_env must both be set, or both omitted"
        )
    _require_env_name(username_env, context="opcua.username_env")
    _require_env_name(password_env, context="opcua.password_env")
    username = env.get(username_env, "").strip()
    password = env.get(password_env)
    if username and password is None:
        raise ConfigError(f"{username_env} is set but {password_env} is missing")
    if not username and password:
        raise ConfigError(f"{password_env} is set but {username_env} is empty")
    if not username:
        return None, None
    return username, password


def _reconnect(raw: object, *, context: str) -> ReconnectConfig:
    if raw is None:
        return ReconnectConfig(initial_s=1.0, max_s=30.0, multiplier=2.0)
    if not isinstance(raw, dict):
        raise ConfigError(f"{context} must be a mapping")
    _reject_unknown(raw, {"initial_s", "max_s", "multiplier"}, context=context)
    initial = _optional_float(raw, "initial_s", 1.0, minimum=0.05)
    maximum = _optional_float(raw, "max_s", 30.0, minimum=initial)
    multiplier = _optional_float(raw, "multiplier", 2.0, minimum=1.0)
    return ReconnectConfig(initial_s=initial, max_s=maximum, multiplier=multiplier)


def _reject_duplicate_columns(
    database: DatabaseConfig,
    trigger: TriggerConfig,
    nodes: tuple[SignalConfig, ...],
) -> None:
    if database.timestamp_column == trigger.column:
        raise ConfigError(
            f"Column {trigger.column!r} is used by both "
            "database.timestamp_column and trigger.column"
        )
    seen: dict[str, str] = {
        database.timestamp_column: "database.timestamp_column",
        trigger.column: "trigger.column",
    }
    for index, node in enumerate(nodes):
        owner = seen.get(node.column)
        if owner is not None:
            raise ConfigError(f"Column {node.column!r} is used by both {owner} and nodes[{index}]")
        seen[node.column] = f"nodes[{index}]"
    trigger_ids = {node.node_id for node in nodes}
    if trigger.node_id in trigger_ids:
        raise ConfigError(
            "The trigger node is also listed under nodes. "
            "Its value is already stored in the trigger column."
        )


def _identifier(value: str, *, context: str) -> str:
    if value in _RESERVED_COLUMNS:
        raise ConfigError(f"{context} cannot be {value!r}; that column is reserved")
    if _IDENTIFIER.fullmatch(value) is None:
        raise ConfigError(
            f"{context} must be a lowercase SQL identifier "
            f"(letters, digits, underscores, max 63 characters), got {value!r}"
        )
    return value


def _node_id(value: str, *, context: str) -> str:
    if not value.strip() or any(char.isspace() for char in value):
        raise ConfigError(f"{context} must be an OPC UA node id without spaces, got {value!r}")
    return value


def _value_type(value: str, *, context: str) -> ValueType:
    try:
        return ValueType(value)
    except ValueError:
        allowed = ", ".join(item.value for item in ValueType)
        raise ConfigError(f"{context} must be one of {allowed}, got {value!r}") from None


def _require_opc_tcp(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "opc.tcp" or not parsed.hostname:
        raise ConfigError(f"opcua.endpoint must be an opc.tcp:// URL, got {endpoint!r}")


def _require_env_name(value: str, *, context: str) -> None:
    if _ENV_NAME.fullmatch(value) is None:
        raise ConfigError(f"{context} must look like an environment variable name, got {value!r}")


def _require_mapping(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = _require(document, key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _require(document: Mapping[str, object], key: str) -> object:
    if key not in document or document[key] is None:
        raise ConfigError(f"{key} is required")
    return document[key]


def _require_str(document: Mapping[str, object], key: str, *, context: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{context}.{key} is required and must be a string")
    return value.strip()


def _optional_str(document: Mapping[str, object], key: str, default: str) -> str:
    value = document.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a string")
    return value.strip()


def _optional_text(value: object, *, context: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{context} must be a string")
    stripped = value.strip()
    return stripped or None


def _optional_float(
    document: Mapping[str, object],
    key: str,
    default: float,
    *,
    minimum: float,
) -> float:
    value = document.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{key} must be a number")
    number = float(value)
    if number < minimum:
        raise ConfigError(f"{key} must be >= {minimum}, got {number}")
    return number


def _reject_unknown(document: Mapping[str, object], allowed: set[str], *, context: str) -> None:
    unknown = sorted(set(document) - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ConfigError(f"Unknown fields in {context}: {names}")
