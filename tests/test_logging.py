import json
import logging

from opcua_collector.logging import JsonFormatter


def test_json_formatter_includes_structured_fields() -> None:
    record = logging.LogRecord(
        name="opcua_collector.collector",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Stored cycle %s",
        args=(7,),
        exc_info=None,
    )
    record.fields = {"cycle": 7, "table": "cycle_samples"}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "Stored cycle 7"
    assert payload["level"] == "INFO"
    assert payload["cycle"] == 7
    assert payload["table"] == "cycle_samples"
    assert payload["logger"] == "opcua_collector.collector"
