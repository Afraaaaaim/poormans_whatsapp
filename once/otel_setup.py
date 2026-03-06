"""
otel_setup.py — OpenTelemetry bootstrap

Call setup_otel() once at app startup before anything else.
After that, use once.logger as normal — trace_id and span_id
will automatically be real OTEL IDs.
"""
import os
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter


def setup_otel(service_name: str = "poormans_whatsapp") -> None:
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://homeserver:4317") 

    resource = Resource.create({
        "service.name": service_name,
        "service.version": os.getenv("VERSION", "unknown"),
        "deployment.environment": os.getenv("ENV", "production"),
    })

    provider = TracerProvider(resource=resource)

    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        insecure=True,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)