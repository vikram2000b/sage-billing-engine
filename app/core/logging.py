# app/core/logging.py
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from .config import settings

from opentelemetry import trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
    OTLPLogExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter,
)

# Check if OpenTelemetry tracing is enabled
otel_enabled = settings.ENABLE_OTEL_TRACING.lower() == "true"

# Configure application logger
logger = logging.getLogger("feed-journey-engine")
logger.setLevel(getattr(logging, settings.LOG_LEVEL.upper()))
logger.propagate = False

if otel_enabled:
    # OpenTelemetry is enabled - send logs and traces to Signoz
    resource = Resource.create(
        {
            "service.name": "feed-journey-engine",
            "deployment.environment": settings.OTEL_SERVICE_ENVIRONMENT,
        }
    )

    # Configure tracer
    tracer_provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(tracer_provider)

    span_exporter = OTLPSpanExporter(
        insecure=True,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        headers={"signoz-ingestion-key": f"{settings.SIGNOZ_INGESTION_KEY}"},
    )
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))

    # Configure logger provider
    logger_provider = LoggerProvider(resource=resource)
    set_logger_provider(logger_provider)

    exporter = OTLPLogExporter(
        insecure=True,
        endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
        headers={
            "signoz-ingestion-key": f"{settings.SIGNOZ_INGESTION_KEY}",
        },
    )
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

    # Add OTLP handler to logger (logs go to Signoz, not console)
    otlp_handler = LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    logger.addHandler(otlp_handler)
else:
    # OpenTelemetry is disabled - use console logging only
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, settings.LOG_LEVEL.upper()))
    console_formatter = logging.Formatter(settings.LOG_FORMAT)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # Create a no-op tracer provider for local development
    tracer_provider = TracerProvider()
    trace.set_tracer_provider(tracer_provider)

# Configure third-party library log levels to reduce noise
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("opentelemetry").setLevel(logging.WARNING)
logging.getLogger("temporalio").setLevel(logging.INFO)

tracer = trace.get_tracer("feed-journey-engine")