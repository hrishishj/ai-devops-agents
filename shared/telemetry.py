"""
OpenTelemetry bootstrap for the AI DevOps Agents project.

Exports traces and metrics to Azure Monitor Application Insights.
Every service/agent imports `get_tracer()` to obtain a named tracer.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace, metrics
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

logger = logging.getLogger(__name__)

_INITIALIZED = False


def _get_resource() -> Resource:
    service_name = os.getenv("OTEL_SERVICE_NAME", "ai-devops-agents")
    return Resource(attributes={SERVICE_NAME: service_name})


def init_telemetry() -> None:
    """
    Call once at application startup (from main.py / agent entry-points).
    Safe to call multiple times — idempotent.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return

    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    resource = _get_resource()

    # ------------------------------------------------------------------ #
    # Tracing
    # ------------------------------------------------------------------ #
    tracer_provider = TracerProvider(resource=resource)

    if connection_string:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
            exporter = AzureMonitorTraceExporter(connection_string=connection_string)
            tracer_provider.add_span_processor(BatchSpanProcessor(exporter))
            logger.info("Azure Monitor trace exporter configured.")
        except ImportError:
            logger.warning("azure-monitor-opentelemetry-exporter not installed; traces will not be exported.")
    else:
        # Fallback: console span exporter for local dev
        try:
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
            tracer_provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        except Exception:
            pass

    trace.set_tracer_provider(tracer_provider)

    # ------------------------------------------------------------------ #
    # Metrics
    # ------------------------------------------------------------------ #
    if connection_string:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter
            metric_exporter = AzureMonitorMetricExporter(connection_string=connection_string)
            reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=60_000)
            meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
            metrics.set_meter_provider(meter_provider)
        except ImportError:
            pass

    _INITIALIZED = True
    logger.info("OpenTelemetry initialised for service '%s'.", resource.attributes.get(SERVICE_NAME))


def get_tracer(name: str) -> trace.Tracer:
    """Return a named tracer. Call init_telemetry() first."""
    return trace.get_tracer(name)


def get_meter(name: str) -> metrics.Meter:
    """Return a named meter. Call init_telemetry() first."""
    return metrics.get_meter(name)
