"""OpenTelemetry tracing for the local incident agent and Action API."""
from __future__ import annotations

import os
import subprocess
import urllib.parse

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor


def _collector_endpoint() -> str:
    """Use an explicit endpoint, or discover this Compose project's host port."""
    configured = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
    if configured:
        parsed = urllib.parse.urlparse(configured)
        if parsed.scheme in ("http", "https"):
            return parsed.netloc
        return configured

    project = os.getenv("COMPOSE_PROJECT", "opentelemetry-demo")
    names = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
         "--filter", "label=com.docker.compose.service=otel-collector", "--format", "{{.Names}}"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout.splitlines()
    if not names:
        raise RuntimeError("No running otel-collector container found")
    mappings = subprocess.run(["docker", "port", names[0], "4317/tcp"],
                              capture_output=True, text=True, timeout=10, check=True).stdout.splitlines()
    if not mappings:
        raise RuntimeError("The otel-collector OTLP gRPC port 4317 is not published to localhost")
    return f"localhost:{mappings[0].rsplit(':', 1)[-1].strip()}"


def configure_tracing(service_name: str):
    """Configure OTLP export and requests spans; return a shutdown hook."""
    endpoint = _collector_endpoint()
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True)))
    trace.set_tracer_provider(provider)
    RequestsInstrumentor().instrument()
    return provider.shutdown
