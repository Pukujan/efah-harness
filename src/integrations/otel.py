"""OpenTelemetry adapter (contract Sections 5.1, 23).

Every external system goes behind an adapter. This is the only module in the
build that imports the OpenTelemetry SDK; ``src/observability`` depends on this
interface, not on the vendor package.

Endpoint policy is load-bearing, not cosmetic. ``environments.yaml`` records
that the build host already runs ``cortex-phoenix`` on 6006 and
``cortex-otel-collector`` on 4317/4318, belonging to an unrelated stack and
reachable tailnet-wide. Exporting EFAH spans there would publish this project's
provenance to anyone on the tailnet. So the forbidden ports are refused in code
with a typed error rather than left to a comment nobody reads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

from governance.envelope import CONTRACT_ID, CONTRACT_VERSION

#: environments.yaml -> observability.must_not_share_instance_with. These belong
#: to cortex-phoenix / cortex-otel-collector.
FORBIDDEN_PORTS: Final = frozenset({6006, 4317, 4318})

#: environments.yaml -> dev.observability
DEFAULT_OTLP_ENDPOINT: Final = "http://localhost:4319"
DEFAULT_PHOENIX_URL: Final = "http://localhost:6007"
DEFAULT_SERVICE_NAME: Final = "efah-control-plane"
DEFAULT_PHOENIX_PROJECT: Final = "efah"


class ForbiddenExporterEndpoint(RuntimeError):
    """The configured collector belongs to another project's stack.

    Typed as an infrastructure refusal, not a warning: contract Section 23
    traces carry provenance, and provenance must not be exported to a tool this
    project does not own.
    """

    def __init__(self, endpoint: str, port: int) -> None:
        self.endpoint = endpoint
        self.port = port
        super().__init__(
            f"refusing OTel endpoint {endpoint!r}: port {port} belongs to "
            "cortex-phoenix/cortex-otel-collector (environments.yaml -> "
            "observability.must_not_share_instance_with). EFAH exports to "
            f"{DEFAULT_OTLP_ENDPOINT}."
        )


@dataclass(frozen=True)
class OtelSettings:
    """Resolved exporter configuration.

    ``__post_init__`` validates rather than a separate ``validate()`` call, so a
    forbidden endpoint cannot be constructed at all.
    """

    endpoint: str = DEFAULT_OTLP_ENDPOINT
    phoenix_url: str = DEFAULT_PHOENIX_URL
    service_name: str = DEFAULT_SERVICE_NAME
    phoenix_project: str = DEFAULT_PHOENIX_PROJECT
    enabled: bool = True
    #: Export each span immediately. Only for tests and one-shot verification --
    #: batching is correct for a running server.
    synchronous_export: bool = False

    def __post_init__(self) -> None:
        check_endpoint_allowed(self.endpoint)
        check_endpoint_allowed(self.phoenix_url)

    @classmethod
    def from_pack(cls, pack: Any, *, environment: str = "dev", **overrides: Any) -> OtelSettings:
        """Build settings from a loaded :class:`integrations.pack.ProjectPack`."""
        observability = (
            pack.yaml("environments.yaml")["environments"][environment].get("observability") or {}
        )
        return cls(
            endpoint=observability.get("otel_exporter_endpoint", DEFAULT_OTLP_ENDPOINT),
            phoenix_url=observability.get("phoenix_url", DEFAULT_PHOENIX_URL),
            **overrides,
        )

    @property
    def grpc_target(self) -> str:
        """The OTLP/gRPC exporter wants ``host:port``, not a URL."""
        parsed = urlparse(self.endpoint)
        if parsed.scheme:
            return parsed.netloc
        return self.endpoint


def check_endpoint_allowed(endpoint: str) -> None:
    """Raise :class:`ForbiddenExporterEndpoint` for another project's collector."""
    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
    port = parsed.port
    if port is None:
        return
    if port in FORBIDDEN_PORTS:
        raise ForbiddenExporterEndpoint(endpoint, port)


def build_exporter(settings: OtelSettings) -> SpanExporter:
    """Construct the OTLP/gRPC exporter for the EFAH-dedicated collector.

    Measured 2026-08-02: ``efah-phoenix`` publishes ``127.0.0.1:4319 -> 4317``,
    i.e. the dedicated endpoint speaks OTLP/gRPC, not OTLP/HTTP.
    """
    check_endpoint_allowed(settings.endpoint)
    return OTLPSpanExporter(endpoint=settings.grpc_target, insecure=True)


def build_tracer_provider(
    settings: OtelSettings, *, exporter: SpanExporter | None = None
) -> TracerProvider:
    """Build a provider bound to the EFAH contract resource attributes."""
    resource = Resource.create(
        {
            "service.name": settings.service_name,
            # Phoenix groups traces by this resource attribute and files
            # anything without it under "default". Naming the project keeps EFAH
            # traces identifiable even on the dedicated instance.
            "openinference.project.name": settings.phoenix_project,
            "efah.contract_id": CONTRACT_ID,
            "efah.contract_version": CONTRACT_VERSION,
        }
    )
    provider = TracerProvider(resource=resource)
    span_exporter = exporter if exporter is not None else build_exporter(settings)
    processor = (
        SimpleSpanProcessor(span_exporter)
        if settings.synchronous_export
        else BatchSpanProcessor(span_exporter)
    )
    provider.add_span_processor(processor)
    return provider


_INSTALLED: TracerProvider | None = None


def install_tracer_provider(
    settings: OtelSettings, *, exporter: SpanExporter | None = None
) -> TracerProvider:
    """Install the provider globally, once.

    OpenTelemetry ignores a second ``set_tracer_provider`` and logs a warning;
    returning the already-installed provider keeps repeated ``create_app()``
    calls (tests, reloads) from silently tracing into a dead exporter.
    """
    global _INSTALLED
    if _INSTALLED is not None:
        return _INSTALLED
    provider = build_tracer_provider(settings, exporter=exporter)
    trace.set_tracer_provider(provider)
    _INSTALLED = provider
    return provider


def installed_provider() -> TracerProvider | None:
    return _INSTALLED


def reset_tracer_provider() -> None:
    """Test hook. Flushes and forgets the installed provider."""
    global _INSTALLED
    if _INSTALLED is not None:
        _INSTALLED.shutdown()
    _INSTALLED = None
