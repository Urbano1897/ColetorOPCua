from __future__ import annotations

import asyncio
import contextlib
import socket
from datetime import UTC

import pytest
from asyncua import Client, Server, ua

from opcua_collector.collector import Collector, _public_endpoint
from opcua_collector.config import (
    AppConfig,
    DatabaseConfig,
    OpcUaConfig,
    ReconnectConfig,
    SignalConfig,
    TriggerConfig,
    ValueType,
)
from opcua_collector.database import DatabaseUnavailable, Sample, TableSchema
from opcua_collector.simulator.server import SimulatorSettings, serve


class MemoryWriter:
    def __init__(self) -> None:
        self.samples: list[Sample] = []
        self.schemas: list[TableSchema] = []
        self.opens = 0
        self.closed = False

    async def open(self) -> None:
        self.opens += 1

    async def ensure_schema(self, schema: TableSchema) -> None:
        self.schemas.append(schema)

    async def insert_sample(self, schema: TableSchema, sample: Sample) -> None:
        self.samples.append(sample)

    async def close(self) -> None:
        self.closed = True


class FlakyWriter(MemoryWriter):
    """Fail the first insert the way a restarting PostgreSQL would."""

    def __init__(self) -> None:
        super().__init__()
        self.failures_left = 1

    async def insert_sample(self, schema: TableSchema, sample: Sample) -> None:
        if self.failures_left:
            self.failures_left -= 1
            raise DatabaseUnavailable("simulated database restart")
        await super().insert_sample(schema, sample)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _config(port: int, *, timeout_s: float = 5.0) -> AppConfig:
    return AppConfig(
        opcua=OpcUaConfig(
            endpoint=f"opc.tcp://127.0.0.1:{port}/test/collector",
            application_uri="urn:opcua-collector:test",
            username=None,
            password=None,
            timeout_s=timeout_s,
            session_timeout_ms=30_000,
            publishing_interval_ms=100.0,
            reconnect=ReconnectConfig(initial_s=0.1, max_s=0.3, multiplier=2.0),
        ),
        database=DatabaseConfig(
            dsn="postgresql://collector:collector@localhost:5432/machine_data",
            table="cycle_samples",
            timestamp_column="sampled_at",
            reconnect=ReconnectConfig(initial_s=0.05, max_s=0.2, multiplier=2.0),
        ),
        trigger=TriggerConfig(node_id="ns=2;s=Trigger", column="cycle_number"),
        nodes=(
            SignalConfig(
                node_id="ns=2;s=Temperature",
                column="temperature",
                value_type=ValueType.FLOAT,
                unit="°C",
                description="Test temperature",
            ),
            SignalConfig(
                node_id="ns=2;s=Missing",
                column="missing_signal",
                value_type=ValueType.FLOAT,
                unit=None,
                description="Not published by the test server",
            ),
        ),
    )


async def _wait_for_cycles(
    writer: MemoryWriter,
    task: asyncio.Task[None],
    expected: list[int],
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 20
    while True:
        if task.done():
            task.result()
        have = [sample.cycle_number for sample in writer.samples]
        if have[: len(expected)] == expected:
            return
        if loop.time() > deadline:
            raise AssertionError(f"timed out waiting for {expected}, have {have}")
        await asyncio.sleep(0.05)


async def test_trigger_reads_the_shot_and_inserts_one_row() -> None:
    port = _free_port()
    endpoint = f"opc.tcp://127.0.0.1:{port}/test/collector"
    server = Server()
    server.set_endpoint(endpoint)
    server.socket_address = ("127.0.0.1", port)
    server.set_server_name("Collector Test Server")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    server.set_identity_tokens([ua.AnonymousIdentityToken])
    await server.init()
    index = await server.register_namespace("urn:opcua-collector:test")
    assert index == 2
    trigger = await server.nodes.objects.add_variable(
        ua.NodeId("Trigger", index),
        "Trigger",
        0,
        varianttype=ua.VariantType.Int32,
    )
    temperature = await server.nodes.objects.add_variable(
        ua.NodeId("Temperature", index),
        "Temperature",
        20.0,
        varianttype=ua.VariantType.Double,
    )

    writer = FlakyWriter()
    collector = Collector(_config(port), writer=writer)
    await server.start()
    task = asyncio.create_task(collector.run())
    try:
        # Cycle 0 is the value present when the subscription is created.
        # The first insert fails once; the collector retries without
        # dropping the shot or the OPC UA session.
        await _wait_for_cycles(writer, task, [0])
        assert writer.failures_left == 0
        assert writer.opens >= 2
        assert writer.samples[0].values["temperature"] == pytest.approx(20.0)
        assert writer.samples[0].values["missing_signal"] is None
        assert writer.samples[0].sampled_at.tzinfo is UTC

        await temperature.write_value(221.5, ua.VariantType.Double)
        await trigger.write_value(1, ua.VariantType.Int32)
        await _wait_for_cycles(writer, task, [0, 1])

        await temperature.write_value(230.0, ua.VariantType.Double)
        await trigger.write_value(5, ua.VariantType.Int32)
        await _wait_for_cycles(writer, task, [0, 1, 5])

        # A counter that steps backwards is still a new shot (a reset).
        await temperature.write_value(210.25, ua.VariantType.Double)
        await trigger.write_value(4, ua.VariantType.Int32)
        await _wait_for_cycles(writer, task, [0, 1, 5, 4])

        temperatures = [sample.values["temperature"] for sample in writer.samples]
        assert temperatures == pytest.approx([20.0, 221.5, 230.0, 210.25])
        assert all(sample.values["missing_signal"] is None for sample in writer.samples)
        assert writer.schemas[0].table == "cycle_samples"
        signal_names = [column.name for column in writer.schemas[0].signals]
        assert signal_names == ["temperature", "missing_signal"]
    finally:
        collector.request_stop()
        try:
            await asyncio.wait_for(task, timeout=5)
        finally:
            await server.stop()

    assert writer.closed is True


async def test_simulator_completes_a_shot_and_bumps_the_counter() -> None:
    port = _free_port()
    endpoint = f"opc.tcp://127.0.0.1:{port}/industrial/simulator"
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(SimulatorSettings(endpoint=endpoint, cycle_period_s=0.5, seed=0), stop)
    )
    client = Client(url=endpoint, timeout=2)
    try:
        await _connect_when_ready(client, task)
        counter = client.get_node("ns=2;s=Machine.CycleCounter")
        deadline = asyncio.get_running_loop().time() + 10
        value = 0
        while asyncio.get_running_loop().time() < deadline:
            if task.done():
                task.result()
            value = int(await counter.read_value())
            if value >= 1:
                break
            await asyncio.sleep(0.1)
        assert value >= 1
        melt = await client.get_node("ns=2;s=Machine.MeltTemperature").read_value()
        quality = await client.get_node("ns=2;s=Machine.PartQuality").read_value()
        assert 180.0 <= float(melt) <= 260.0
        assert quality in {"good", "review", "scrap"}
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5)
        with contextlib.suppress(Exception):
            await client.disconnect()


async def _connect_when_ready(client: Client, task: asyncio.Task[None]) -> None:
    deadline = asyncio.get_running_loop().time() + 15
    while True:
        if task.done():
            task.result()
        try:
            await client.connect()
            return
        except (TimeoutError, ConnectionError, OSError):
            if asyncio.get_running_loop().time() > deadline:
                raise
            await asyncio.sleep(0.1)


async def test_collector_keeps_running_when_the_server_is_down() -> None:
    port = _free_port()
    writer = MemoryWriter()
    collector = Collector(_config(port, timeout_s=0.5), writer=writer)
    task = asyncio.create_task(collector.run())
    try:
        await asyncio.sleep(0.4)
        assert not task.done()
        assert writer.samples == []
    finally:
        collector.request_stop()
        await asyncio.wait_for(task, timeout=5)
    assert writer.closed is True


def test_endpoint_logging_drops_userinfo() -> None:
    assert _public_endpoint("opc.tcp://user:secret@press.internal:4840/machine") == (
        "opc.tcp://press.internal:4840/machine"
    )
    plain = "opc.tcp://127.0.0.1:4840/industrial/simulator"
    assert _public_endpoint(plain) == plain
