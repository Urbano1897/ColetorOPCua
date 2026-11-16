"""Convert OPC UA variant values into the Python types we store.

A bad conversion becomes NULL for that column. Losing one signal is better
than dropping the whole shot, which is the row a quality engineer will look for.
"""

from __future__ import annotations

from opcua_collector.config import ValueType

# Strings a PLC or a gateway sometimes publishes instead of a real boolean.
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def parse_cycle(value: object) -> int | None:
    """Return an integer cycle count, or None when the value cannot be one.

    Booleans are rejected on purpose: `int(True)` is 1, and a boolean trigger
    would silently record a fake cycle.
    """
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def coerce(value: object, value_type: ValueType) -> tuple[object, bool]:
    """Return `(stored_value, ok)`.

    `ok` is False when the value was present but could not be converted.
    A missing value (`None`) is `(None, True)`: the server had nothing to give.
    """
    if value is None:
        return None, True
    try:
        if value_type is ValueType.INTEGER:
            if isinstance(value, bool):
                return None, False
            return int(value), True
        if value_type is ValueType.FLOAT:
            if isinstance(value, bool):
                return None, False
            return float(value), True
        if value_type is ValueType.BOOLEAN:
            return _coerce_bool(value)
        if value_type is ValueType.STRING:
            text = str(value).replace("\x00", "")
            return text, True
    except (TypeError, ValueError, OverflowError):
        return None, False
    return None, False


def _coerce_bool(value: object) -> tuple[object, bool]:
    if isinstance(value, bool):
        return value, True
    if isinstance(value, int) and value in (0, 1):
        return bool(value), True
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE:
            return True, True
        if token in _FALSE:
            return False, True
    return None, False
