"""Subscribe to a cycle counter, read the shot, store one row.

The cycle counter is the natural key of a discrete machine: it changes
once, after the shot is finished, when the process variables for that shot
are stable. Polling on a timer either stores the same shot twice or cuts
across two shots. A subscription plus one batch read keeps the row consistent.

Delivery is at-least-once for the latest cycle. A row that was stored is not
written again while this process remembers it. A process restart can store
the current cycle one extra time. Cycles that arrive while PostgreSQL is
down stay in the OPC UA subscription queue; they are not written to disk.
A durable spool is deliberately out of scope for this version.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit, urlunsplit

from asyncua import Client, ua
from asyncua.common.subscription import DataChangeEvent, StatusChangeEvent, SubEvent, Subscription
from asyncua.ua.uaerrors import UaError

from opcua_collector.backoff import Backoff, sleep_or_stop
from opcua_collector.config import AppConfig, SignalConfig
from opcua_collector.database import (
    DatabaseUnavailable,
    PostgresWriter,
    Sample,
    SampleWriter,
    TableSchema,
    schema_from_config,
    utcnow,
)
from opcua_collector.logging import get_logger
from opcua_collector.values import coerce, parse_cycle

_log = get_logger(__name__)


class Collector:
    """Run until `request_stop` or the task is cancelled."""

    def __init__(self, config: AppConfig, writer: SampleWriter | None = None) -> None:
        self._config = config
        self._writer = writer if writer is not None else PostgresWriter(config.database.dsn)
        self._stop = asyncio.Event()
        # Remembered only in this process. A restart may store the current
        # cycle once more; that is the duplicate we accept.
        self._last_cycle: int | None = None

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        backoff = Backoff(
            initial_s=self._config.opcua.reconnect.initial_s,
            max_s=self._config.opcua.reconnect.max_s,
            multiplier=self._config.opcua.reconnect.multiplier,
        )
        schema = schema_from_config(self._config)
        try:
            while not self._stop.is_set():
                try:
                    await self._run_session(schema, backoff)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._stop.is_set():
                        break
                    delay = backoff.failure_delay()
                    _log.warning(
                        f"Collection session ended; reconnecting in {delay:.1f}s",
                        error=f"{type(exc).__name__}: {exc}",
                        retry_in_s=delay,
                        endpoint=_public_endpoint(self._config.opcua.endpoint),
                    )
                    _log.debug("Collection session traceback", exc_info=True)
                    if await sleep_or_stop(delay, self._stop):
                        break
        finally:
            await self._writer.close()
            _log.info("Collector stopped")

    async def _run_session(self, schema: TableSchema, backoff: Backoff) -> None:
        await self._writer.open()
        await self._writer.ensure_schema(schema)

        client = self._client()
        connected = False
        subscription: Subscription | None = None
        try:
            await client.connect(auto_reconnect=False)
            connected = True
            _log.info(
                "Connected to OPC UA server",
                endpoint=_public_endpoint(self._config.opcua.endpoint),
            )
            subscription = await self._subscribe(client)
            backoff.reset()
            _log.info("Subscribed to cycle counter", node_id=self._config.trigger.node_id)
            await self._consume(client, subscription, schema)
        finally:
            if connected:
                await _shutdown_opcua(client, subscription)

    def _client(self) -> Client:
        opcua = self._config.opcua
        # auto_reconnect stays off. This class owns the backoff so the OPC UA
        # session and the database session come back together, and so the
        # policy is visible in one place instead of inside the library.
        client = Client(url=opcua.endpoint, timeout=opcua.timeout_s, auto_reconnect=False)
        client.name = "OPC UA Cycle Collector"
        client.description = "Stores one PostgreSQL row per machine cycle"
        client.application_uri = opcua.application_uri
        client.session_timeout = opcua.session_timeout_ms
        if opcua.username is not None:
            client.set_user(opcua.username)
            client.set_password(opcua.password or "")
        return client

    async def _subscribe(self, client: Client) -> Subscription:
        # handler=None selects asyncua's iterator mode. Notifications land on
        # a library task, so a slow INSERT cannot stall the OPC UA publish
        # loop. Reading other nodes from inside a data-change callback is a
        # classic way to deadlock this protocol.
        subscription = await client.create_subscription(self._config.opcua.publishing_interval_ms)
        trigger = client.get_node(self._config.trigger.node_id)
        await subscription.subscribe_data_change(trigger)
        return subscription

    async def _consume(
        self,
        client: Client,
        subscription: Subscription,
        schema: TableSchema,
    ) -> None:
        while not self._stop.is_set():
            event = await self._next_event(subscription)
            if event is None:
                return
            if isinstance(event, StatusChangeEvent):
                status = event.notification.Status
                if status.is_bad():
                    raise ConnectionError(f"OPC UA subscription status {status.name}")
                _log.info("OPC UA subscription status", status=status.name)
                continue
            if not isinstance(event, DataChangeEvent):
                continue
            await self._on_trigger(client, schema, event.value)

    async def _on_trigger(self, client: Client, schema: TableSchema, raw_value: object) -> None:
        cycle = parse_cycle(raw_value)
        if cycle is None:
            _log.warning("Ignored a trigger value that is not an integer", value=repr(raw_value))
            return
        if cycle == self._last_cycle:
            _log.info("Skipping a cycle that was already stored", cycle=cycle)
            return
        if self._last_cycle is not None and cycle < self._last_cycle:
            _log.warning(
                "Cycle counter moved backwards; storing the new value",
                cycle=cycle,
                previous=self._last_cycle,
            )
        values = await self._read_signals(client)
        sample = Sample(sampled_at=utcnow(), cycle_number=cycle, values=values)
        await self._insert(schema, sample)
        self._last_cycle = cycle
        _log.info(
            f"Stored cycle {cycle}",
            cycle=cycle,
            table=schema.table,
            signals=len(schema.signals),
        )
        _log.debug("Stored cycle values", cycle=cycle, values=dict(values))

    async def _insert(self, schema: TableSchema, sample: Sample) -> None:
        """Retry a dead database without tearing down the OPC UA session.

        The shot is already in memory. Giving up here would drop it, and the
        subscription queue is holding any newer cycles until this returns.
        """
        backoff = Backoff(
            initial_s=self._config.database.reconnect.initial_s,
            max_s=self._config.database.reconnect.max_s,
            multiplier=self._config.database.reconnect.multiplier,
        )
        while True:
            try:
                await self._writer.insert_sample(schema, sample)
                return
            except DatabaseUnavailable as exc:
                if self._stop.is_set():
                    raise
                delay = backoff.failure_delay()
                _log.warning(
                    f"PostgreSQL write failed; retrying in {delay:.1f}s",
                    error=str(exc),
                    cycle=sample.cycle_number,
                    retry_in_s=delay,
                )
                if await sleep_or_stop(delay, self._stop):
                    raise
                try:
                    await self._writer.open()
                except DatabaseUnavailable as reopen_error:
                    _log.warning("PostgreSQL is still unavailable", error=str(reopen_error))

    async def _read_signals(self, client: Client) -> dict[str, object]:
        """Read every configured node in one OPC UA Read.

        One service call is one snapshot: the values belong to the same
        moment, and a single dead session fails the whole read instead of
        storing a half-null row that looks like a real shot.
        """
        signals = self._config.nodes
        nodes = [client.get_node(signal.node_id) for signal in signals]
        try:
            data_values = await client.read_attributes(nodes, attr=ua.AttributeIds.Value)
        except (TimeoutError, ConnectionError, OSError, UaError) as exc:
            raise ConnectionError(f"Batch read failed: {exc}") from exc
        if len(data_values) != len(signals):
            raise ConnectionError(
                f"Batch read returned {len(data_values)} values for {len(signals)} nodes"
            )
        return {
            signal.column: _stored_value(signal, data_value)
            for signal, data_value in zip(signals, data_values, strict=True)
        }

    async def _next_event(self, subscription: Subscription) -> SubEvent | None:
        getter = asyncio.create_task(subscription.__anext__())
        stopper = asyncio.create_task(self._stop.wait())
        try:
            done, pending = await asyncio.wait(
                {getter, stopper},
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            getter.cancel()
            stopper.cancel()
            raise
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if self._stop.is_set() and getter not in done:
            return None
        if getter not in done:
            return None
        try:
            return getter.result()
        except StopAsyncIteration:
            if self._stop.is_set():
                return None
            raise ConnectionError("OPC UA subscription closed") from None


async def _shutdown_opcua(client: Client, subscription: Subscription | None) -> None:
    if subscription is not None:
        try:
            await subscription.delete()
        except Exception:
            _log.debug("Ignoring an error while deleting the subscription", exc_info=True)
    try:
        await client.disconnect()
    except Exception:
        _log.debug("Ignoring an error while disconnecting", exc_info=True)


def _stored_value(signal: SignalConfig, data_value: ua.DataValue) -> object:
    status = data_value.StatusCode
    status_name = status.name if status is not None else "Missing"
    if status is None or status.is_bad():
        # A node the server does not have, or a read that failed. Keep the
        # shot and store NULL for this column.
        _log.warning(
            "Signal read has a bad status; storing NULL",
            node_id=signal.node_id,
            column=signal.column,
            status=status_name,
        )
        return None
    if status.is_uncertain():
        _log.warning(
            "Signal read is uncertain; storing the value anyway",
            node_id=signal.node_id,
            column=signal.column,
            status=status_name,
        )
    raw = None if data_value.Value is None else data_value.Value.Value
    converted, ok = coerce(raw, signal.value_type)
    if not ok:
        _log.warning(
            "Signal value could not be converted; storing NULL",
            node_id=signal.node_id,
            column=signal.column,
            value_type=signal.value_type.value,
            value=repr(raw),
        )
    return converted


def _public_endpoint(url: str) -> str:
    """Drop userinfo so a URL copied from a log is not a secret."""
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
