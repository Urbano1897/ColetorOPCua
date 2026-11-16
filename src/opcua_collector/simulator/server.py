"""OPC UA server for the fictional press.

The cycle counter is written last, after the snapshot nodes. A subscriber
that reads the process variables on that data change therefore sees the
finished shot, not a mix of the previous shot and the new one.

Summary nodes stay still during the cycle. `State`, `ScrewPosition` and
`HydraulicPressure` move, for anyone watching the server live, and they are
not part of the default collector config.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import signal
from dataclasses import dataclass
from urllib.parse import urlsplit

from asyncua import Server, ua
from asyncua.common.node import Node

from opcua_collector.logging import configure_logging, get_logger
from opcua_collector.simulator.catalog import (
    CATALOG,
    NAMESPACE_URI,
    SIMULATOR_NAMESPACE_INDEX,
    CatalogNode,
    node_id,
)
from opcua_collector.simulator.process import (
    PHASES,
    CycleSummary,
    LiveSignals,
    build_cycle,
    idle_summary,
    live_signals,
    summary_by_identifier,
)

_log = get_logger(__name__)

DEFAULT_ENDPOINT = "opc.tcp://127.0.0.1:4840/industrial/simulator"
_MIN_PERIOD_S = 0.5

_VARIANTS = {
    "integer": ua.VariantType.Int32,
    "float": ua.VariantType.Double,
    "boolean": ua.VariantType.Boolean,
    "string": ua.VariantType.String,
}


@dataclass(frozen=True)
class SimulatorSettings:
    endpoint: str
    cycle_period_s: float
    seed: int


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="opcua-simulator",
        description="Run a generic OPC UA injection-molding simulator.",
    )
    parser.add_argument(
        "--endpoint",
        default=os.environ.get("SIM_ENDPOINT", DEFAULT_ENDPOINT),
        help=f"OPC UA endpoint URL to advertise (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--cycle-period",
        type=float,
        default=_env_float("SIM_CYCLE_PERIOD_S", 5.0),
        help="Accelerated wall-clock length of one shot, in seconds (env: SIM_CYCLE_PERIOD_S)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=_env_int("SIM_SEED", 0),
        help="Random seed for the process model. Default 0 keeps a demo repeatable.",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument(
        "--log-format",
        choices=("json", "text"),
        default=os.environ.get("LOG_FORMAT", "json"),
    )
    args = parser.parse_args(argv)
    if args.cycle_period < _MIN_PERIOD_S:
        parser.error(f"--cycle-period must be at least {_MIN_PERIOD_S} seconds")
    configure_logging(args.log_level, args.log_format)
    settings = SimulatorSettings(
        endpoint=args.endpoint,
        cycle_period_s=args.cycle_period,
        seed=args.seed,
    )
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


async def serve(settings: SimulatorSettings, stop: asyncio.Event | None = None) -> None:
    if stop is None:
        stop = asyncio.Event()
        _install_stop_signals(stop)
    server = _new_server(settings.endpoint)
    await server.init()
    index = await server.register_namespace(NAMESPACE_URI)
    if index != SIMULATOR_NAMESPACE_INDEX:
        raise RuntimeError(
            f"Expected namespace index {SIMULATOR_NAMESPACE_INDEX} for {NAMESPACE_URI}, "
            f"got {index}. The node ids in config.example.yaml would not match."
        )
    nodes = await _create_nodes(server, index)
    await _write_summary(nodes, idle_summary())
    await _write_counter(nodes, 0)
    await _write_live(nodes, LiveSignals(state="idle", screw_position=80.0, hydraulic_pressure=0.0))
    _log.info(
        "Simulator address space is ready",
        endpoint=settings.endpoint,
        trigger=node_id("Machine.CycleCounter", index),
        cycle_period_s=settings.cycle_period_s,
        seed=settings.seed,
    )
    await server.start()
    try:
        await _run_cycles(nodes, settings, stop)
    finally:
        await server.stop()
        _log.info("Simulator stopped")


def _new_server(endpoint: str) -> Server:
    parsed = urlsplit(endpoint)
    if parsed.scheme != "opc.tcp" or not parsed.hostname or not parsed.port:
        raise SystemExit(
            f"error: simulator endpoint must be opc.tcp://host:port/path, got {endpoint!r}"
        )
    server = Server()
    server.set_endpoint(endpoint)
    # Listen on every interface when the advertised host is a DNS name
    # (the Compose service name is not a local interface address).
    if parsed.hostname in {"127.0.0.1", "localhost"}:
        server.socket_address = (parsed.hostname, parsed.port)
    else:
        server.socket_address = ("0.0.0.0", parsed.port)
    server.set_server_name("Generic Injection Molding Simulator")
    server.manufacturer_name = "opcua-collector"
    server.product_uri = "urn:opcua-collector:simulator"
    # Anonymous and unencrypted, on purpose: this process is a local demo.
    # Do not put it on a plant network.
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
    server.set_identity_tokens([ua.AnonymousIdentityToken])
    return server


async def _create_nodes(server: Server, index: int) -> dict[str, Node]:
    machine = await server.nodes.objects.add_object(
        ua.NodeId("Machine", index),
        "InjectionMoldingMachine",
    )
    nodes: dict[str, Node] = {}
    for spec in CATALOG:
        initial = _initial_value(spec)
        node = await machine.add_variable(
            ua.NodeId(spec.identifier, index),
            spec.browse_name,
            initial,
            varianttype=_VARIANTS[spec.value_type],
        )
        await _write_description(node, spec)
        nodes[spec.identifier] = node
    return nodes


async def _run_cycles(
    nodes: dict[str, Node],
    settings: SimulatorSettings,
    stop: asyncio.Event,
) -> None:
    rng = random.Random(settings.seed)
    cycle_number = 0
    while not stop.is_set():
        cycle_number += 1
        summary = build_cycle(cycle_number, rng)
        finished = await _animate(nodes, summary, settings.cycle_period_s, stop)
        if not finished:
            # Stop arrived mid-shot. Leave the previous snapshot published
            # so a subscriber does not read a half-finished cycle.
            return
        # Snapshot first, counter last. The data-change is the trigger.
        await _write_summary(nodes, summary)
        await _write_counter(nodes, summary.cycle_number)
        _log.info(
            f"Completed cycle {summary.cycle_number} ({summary.part_quality})",
            cycle=summary.cycle_number,
            part_quality=summary.part_quality,
            melt_temperature_c=summary.melt_temperature,
            injection_pressure_mpa=summary.injection_pressure,
            cycle_time_s=summary.cycle_time,
        )


async def _animate(
    nodes: dict[str, Node],
    summary: CycleSummary,
    period_s: float,
    stop: asyncio.Event,
) -> bool:
    for phase, fraction in PHASES:
        duration = period_s * fraction
        steps = max(1, int(duration / 0.25))
        step = duration / steps
        for index in range(steps):
            progress = (index + 1) / steps
            await _write_live(nodes, live_signals(phase, progress, summary))
            if await _wait(step, stop):
                return False
    return True


async def _write_summary(nodes: dict[str, Node], summary: CycleSummary) -> None:
    for identifier, value in summary_by_identifier(summary).items():
        spec = _spec(identifier)
        await nodes[identifier].write_value(value, _VARIANTS[spec.value_type])


async def _write_counter(nodes: dict[str, Node], cycle_number: int) -> None:
    await nodes["Machine.CycleCounter"].write_value(cycle_number, ua.VariantType.Int32)


async def _write_live(nodes: dict[str, Node], signals: LiveSignals) -> None:
    await nodes["Machine.State"].write_value(signals.state, ua.VariantType.String)
    await nodes["Machine.ScrewPosition"].write_value(signals.screw_position, ua.VariantType.Double)
    await nodes["Machine.HydraulicPressure"].write_value(
        signals.hydraulic_pressure,
        ua.VariantType.Double,
    )


async def _write_description(node: Node, spec: CatalogNode) -> None:
    text = spec.description
    if spec.unit:
        text = f"{text} [{spec.unit}]"
    await node.write_attribute(
        ua.AttributeIds.Description,
        ua.DataValue(ua.Variant(ua.LocalizedText(text), ua.VariantType.LocalizedText)),
    )


def _spec(identifier: str) -> CatalogNode:
    for spec in CATALOG:
        if spec.identifier == identifier:
            return spec
    raise KeyError(identifier)


def _initial_value(spec: CatalogNode) -> object:
    if spec.value_type == "integer":
        return 0
    if spec.value_type == "float":
        return 0.0
    if spec.value_type == "boolean":
        return False
    return ""


def _install_stop_signals(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            return


async def _wait(seconds: float, stop: asyncio.Event) -> bool:
    """Return True when stop was requested during the wait."""
    if stop.is_set():
        return True
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        raise SystemExit(f"error: {name} must be a number, got {raw!r}") from None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"error: {name} must be an integer, got {raw!r}") from None
