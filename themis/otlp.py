"""OTLP/HTTP trace payloads in, flat span rows out.

Any agent that emits OpenTelemetry GenAI spans works without code changes: point its OTLP exporter at Themis.
Spans are read by the GenAI semantic conventions (gen_ai.operation.name, gen_ai.agent.name, gen_ai.tool.name,
gen_ai.request.model, gen_ai.usage.*). A few optional themis.* attributes add what the conventions do not cover
yet: handoffs, checks, action risk and gate decisions. docs/TELEMETRY.md has the full list.
"""

from __future__ import annotations

import base64
import binascii
import json

from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

MODEL_OPS = {"chat", "text_completion", "generate_content", "embeddings"}
# Prompts, completions and tool payloads can hold customer data, so they are dropped unless THEMIS_STORE_CONTENT=1.
CONTENT_PREFIXES = (
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.prompt",
    "gen_ai.completion",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
)
MAX_ATTRS, MAX_VALUE, MAX_SPANS = 64, 256, 20_000
STATUS_ERROR = 2


class BadPayload(ValueError):
    pass


def _value(v):
    kind = v.WhichOneof("value")
    if kind is None or kind == "bytes_value":
        return None
    if kind == "array_value":
        return [_value(x) for x in v.array_value.values[:16]]
    if kind == "kvlist_value":
        return {kv.key[:80]: _value(kv.value) for kv in v.kvlist_value.values[:16]}
    x = getattr(v, kind)
    return x[:MAX_VALUE] if isinstance(x, str) else x


def _attrs(pairs) -> dict:
    return {kv.key[:120]: _value(kv.value) for kv in pairs[:MAX_ATTRS]}


def _hex_ids_to_base64(node) -> None:
    """OTLP/JSON writes trace and span ids as hex; protobuf's JSON parser expects base64."""
    if isinstance(node, list):
        for item in node:
            _hex_ids_to_base64(item)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in ("traceId", "spanId", "parentSpanId") and isinstance(value, str):
                node[key] = base64.b64encode(bytes.fromhex(value)).decode()
            else:
                _hex_ids_to_base64(value)


def _int(x) -> int:
    try:
        return max(int(x or 0), 0)
    except (TypeError, ValueError):
        return 0


def parse(body: bytes, content_type: str, now: float, store_content: bool = False) -> tuple[list[dict], int]:
    """(span rows, number rejected). Raises BadPayload when the body is not OTLP at all."""
    req = ExportTraceServiceRequest()
    try:
        if "json" in content_type:
            data = json.loads(body)
            _hex_ids_to_base64(data)
            json_format.ParseDict(data, req, ignore_unknown_fields=True)
        else:
            req.ParseFromString(body)
    # TypeError and AttributeError: protobuf's JSON parser raises them for valid JSON of the wrong shape, such as `0`
    except (DecodeError, json_format.ParseError, ValueError, TypeError, AttributeError, binascii.Error, RecursionError) as e:
        raise BadPayload(f"not an OTLP trace export: {type(e).__name__}") from e

    rows, rejected = [], 0
    for rs in req.resource_spans:
        resource = _attrs(rs.resource.attributes)
        for ss in rs.scope_spans:
            for span in ss.spans:
                if len(rows) >= MAX_SPANS:
                    raise BadPayload(f"more than {MAX_SPANS} spans in one request")
                row = _row(span, resource, store_content)
                # a span from the far past or future would corrupt the SLO windows, so it is refused
                if row is None or not (now - 86400 <= row["start"] <= row["end"] <= now + 300):
                    rejected += 1
                else:
                    rows.append(row)
    return rows, rejected


def _row(span, resource: dict, store_content: bool) -> dict | None:
    if len(span.trace_id) != 16 or len(span.span_id) != 8:
        return None
    a = _attrs(span.attributes)
    if not store_content:
        a = {k: v for k, v in a.items() if not k.startswith(CONTENT_PREFIXES)}
    op, agent = a.get("gen_ai.operation.name"), a.get("gen_ai.agent.name")
    if a.get("themis.handoff.to"):
        kind, target, agent = "handoff", a["themis.handoff.to"], a.get("themis.handoff.from", agent)
    elif op in ("invoke_agent", "create_agent"):
        kind, target = "agent", None
    elif op == "execute_tool" or "gen_ai.tool.name" in a:
        kind, target = "tool", a.get("gen_ai.tool.name")
    elif op in MODEL_OPS:
        kind, target = "model", a.get("gen_ai.request.model") or a.get("gen_ai.response.model")
    else:
        kind, target = "other", None
    parent = span.parent_span_id.hex() or None
    cost = a.get("themis.cost.usd")
    a.setdefault("service.name", resource.get("service.name"))
    return {
        "trace_id": span.trace_id.hex(),
        "span_id": span.span_id.hex(),
        "parent_id": parent,
        "is_root": parent is None,
        "done": False,
        "kind": kind,
        "name": span.name[:200],
        "agent": str(agent)[:120] if agent else None,
        "target": str(target)[:160] if target else None,
        "pod": str(a.get("k8s.pod.name") or resource.get("k8s.pod.name") or resource.get("service.instance.id") or "")[:160]
        or None,
        "node": str(a.get("k8s.node.name") or resource.get("k8s.node.name") or "")[:160] or None,
        "start": span.start_time_unix_nano / 1e9,
        "end": span.end_time_unix_nano / 1e9,
        "ok": span.status.code != STATUS_ERROR,
        "error_type": str(a["error.type"])[:80] if a.get("error.type") else None,
        "input_tokens": _int(a.get("gen_ai.usage.input_tokens")),
        "output_tokens": _int(a.get("gen_ai.usage.output_tokens")),
        "cost": float(cost) if isinstance(cost, (int, float)) and cost >= 0 else None,
        "attrs": a,
    }
