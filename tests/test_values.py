from opcua_collector.config import ValueType
from opcua_collector.values import coerce, parse_cycle


def test_parse_cycle_accepts_integers_and_whole_floats() -> None:
    assert parse_cycle(12) == 12
    assert parse_cycle(0) == 0
    assert parse_cycle(4.0) == 4
    assert parse_cycle(True) is None
    assert parse_cycle(4.2) is None
    assert parse_cycle("4") is None
    assert parse_cycle(None) is None


def test_coerce_numeric_boolean_and_string() -> None:
    assert coerce(12, ValueType.INTEGER) == (12, True)
    assert coerce(True, ValueType.INTEGER) == (None, False)
    assert coerce(1.5, ValueType.FLOAT) == (1.5, True)
    assert coerce(True, ValueType.BOOLEAN) == (True, True)
    assert coerce(0, ValueType.BOOLEAN) == (False, True)
    assert coerce("yes", ValueType.BOOLEAN) == (True, True)
    assert coerce("maybe", ValueType.BOOLEAN) == (None, False)
    assert coerce("a\x00b", ValueType.STRING) == ("ab", True)
    assert coerce(None, ValueType.FLOAT) == (None, True)
