"""OTLP in, span rows out. This is the trust boundary for telemetry, so most of it is about bad input."""

import base64
import json
import os
import time

import pytest
from google.protobuf import json_format
from hypothesis import given
from hypothesis import strategies as st
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from tests.conftest import otlp
from themis import otlp as parser

NOW = time.time()


def one(attrs=None, **over):
    span = dict(name="x", trace=os.urandom(16), id=os.urandom(8), parent=os.urandom(8), start=NOW - 1, end=NOW, attrs=attrs or {})
    return {**span, **over}


def rows(*spans, store_content=False):
    return parser.parse(otlp(list(spans)), "application/x-protobuf", NOW, store_content)


@pytest.mark.parametrize(
    "attrs, kind, agent, target",
    [
        ({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "coder"}, "agent", "coder", None),
        ({"gen_ai.operation.name": "chat", "gen_ai.request.model": "m1"}, "model", None, "m1"),
        ({"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "kb.search"}, "tool", None, "kb.search"),
        ({"gen_ai.tool.name": "kb.search"}, "tool", None, "kb.search"),  # some SDKs omit the operation name
        ({"themis.handoff.from": "coder", "themis.handoff.to": "reviewer"}, "handoff", "coder", "reviewer"),
        ({"http.method": "GET"}, "other", None, None),
    ],
)
def test_spans_are_classified_by_the_genai_conventions(attrs, kind, agent, target):
    (row,), rejected = rows(one(attrs))
    assert (row["kind"], row["agent"], row["target"], rejected) == (kind, agent, target, 0)


def test_tokens_status_pod_and_root_are_read():
    usage = {
        "gen_ai.operation.name": "chat",
        "gen_ai.usage.input_tokens": 900,
        "gen_ai.usage.output_tokens": 250,
        "error.type": "rate_limited",
    }
    body = otlp([one(usage, error=True, parent=b"")], resource={"service.name": "svc", "k8s.pod.name": "pod-1"})
    (row,), _ = parser.parse(body, "application/x-protobuf", NOW)
    assert (row["input_tokens"], row["output_tokens"], row["ok"], row["error_type"]) == (900, 250, False, "rate_limited")
    assert row["pod"] == "pod-1" and row["is_root"] and row["parent_id"] is None and row["attrs"]["service.name"] == "svc"


def test_json_and_protobuf_give_the_same_rows():
    body = otlp([one({"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": "coder"})])
    req = ExportTraceServiceRequest.FromString(body)
    as_json = json_format.MessageToDict(req)
    for span in as_json["resourceSpans"][0]["scopeSpans"][0]["spans"]:  # OTLP/JSON carries ids as hex, not base64
        for key in ("traceId", "spanId", "parentSpanId"):
            span[key] = base64.b64decode(span[key]).hex()
    from_json, _ = parser.parse(json.dumps(as_json).encode(), "application/json", NOW)
    from_proto, _ = parser.parse(body, "application/x-protobuf", NOW)
    assert from_json == from_proto


def test_prompts_and_tool_payloads_are_dropped_unless_asked_for():
    private = {
        "gen_ai.input.messages": "the customer's card ends 4417",
        "gen_ai.tool.call.arguments": "{}",
        "gen_ai.operation.name": "chat",
    }
    (kept,), _ = rows(one(private))
    assert set(kept["attrs"]) == {"gen_ai.operation.name", "service.name"}
    (stored,), _ = rows(one(private), store_content=True)
    assert "gen_ai.input.messages" in stored["attrs"]


@pytest.mark.parametrize(
    "bad",
    [
        dict(trace=b"short"),
        dict(id=b""),
        dict(start=NOW + 3600, end=NOW + 3601),  # from the future
        dict(start=NOW - 3 * 86400, end=NOW - 3 * 86400 + 1),  # days old: would rewrite closed SLO windows
        dict(start=NOW, end=NOW - 5),  # ends before it starts
    ],
)
def test_spans_that_would_corrupt_the_windows_are_rejected_not_stored(bad):
    good, rejected = rows(one(), one(**bad))
    assert len(good) == 1 and rejected == 1


def test_oversized_attributes_are_cut_down():
    huge = {f"k{i}": "v" * 5000 for i in range(200)}
    (row,), _ = rows(one(huge))
    assert len(row["attrs"]) <= parser.MAX_ATTRS + 1 and all(len(str(v)) <= parser.MAX_VALUE for v in row["attrs"].values())


@pytest.mark.parametrize(
    "body, ctype",
    [
        (b"\xff\xff\xff\xff", "application/x-protobuf"),
        (b"{not json", "application/json"),
        (b'{"resourceSpans": 7}', "application/json"),
    ],
)
def test_garbage_is_a_clean_400_not_a_crash(body, ctype):
    with pytest.raises(parser.BadPayload):
        parser.parse(body, ctype, NOW)


@given(body=st.binary(max_size=400), as_json=st.booleans())
def test_no_input_can_raise_anything_but_badpayload(body, as_json):
    try:
        parser.parse(body, "application/json" if as_json else "application/x-protobuf", NOW)
    except parser.BadPayload:
        pass
