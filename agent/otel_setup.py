"""
agent/otel_setup.py — 可选 OpenTelemetry 接入 (P3 可观测性栈)

用法 (app 层, 在创建 FastAPI app 之后调用一次):

    from agent.otel_setup import setup_otel
    setup_otel(app)   # opentelemetry 未安装或未配置 endpoint 时安全 no-op

环境变量:
    OTEL_EXPORTER_OTLP_ENDPOINT  OTLP 接收端 (如 http://otel-collector:4318)。
                                 未设置时不导出 (仍可 no-op 返回)。
    OTEL_SERVICE_NAME            服务名, 默认 langgraph-customer-service-agent。

全部 opentelemetry 导入均守卫; 任何失败都降级为 no-op, 绝不影响启动。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger("agent.otel_setup")

_initialized = False


def setup_otel(app: Any = None) -> Optional[Any]:
    """初始化 OTel tracing。返回 tracer 或 None (no-op)。幂等。"""
    global _initialized
    if _initialized:
        try:
            from opentelemetry import trace  # type: ignore
            return trace.get_tracer("agent")
        except Exception:
            return None

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        logger.info("OTEL_EXPORTER_OTLP_ENDPOINT not set; OpenTelemetry disabled")
        return None

    try:
        from opentelemetry import trace  # type: ignore
        from opentelemetry.sdk.resources import Resource  # type: ignore
        from opentelemetry.sdk.trace import TracerProvider  # type: ignore
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # type: ignore
    except Exception:
        logger.info("opentelemetry sdk not installed; OpenTelemetry disabled")
        return None

    exporter = None
    try:  # 优先 HTTP exporter, 其次 gRPC
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore
            OTLPSpanExporter,
        )
        exporter = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces"
                                    if not endpoint.endswith("/v1/traces") else endpoint)
    except Exception:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore
                OTLPSpanExporter,
            )
            exporter = OTLPSpanExporter(endpoint=endpoint)
        except Exception:
            logger.warning("no OTLP exporter available; OpenTelemetry disabled")
            return None

    try:
        resource = Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME",
                                      "langgraph-customer-service-agent"),
        })
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except Exception:
        logger.warning("failed to configure OTel tracer provider", exc_info=True)
        return None

    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import (  # type: ignore
                FastAPIInstrumentor,
            )
            FastAPIInstrumentor.instrument_app(app)
        except Exception:
            logger.info("FastAPI auto-instrumentation unavailable; "
                        "manual spans only")

    _initialized = True
    logger.info("OpenTelemetry initialized, exporting to %s", endpoint)
    return trace.get_tracer("agent")


__all__ = ["setup_otel"]
