from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django.db.models import F

from ..models import OrchestrationRun, TokenUsageEvent


def capture_usage_event(
    *,
    run: OrchestrationRun,
    session_id: str,
    endpoint: str,
    payload: Any,
) -> TokenUsageEvent | None:
    usage_payload = _extract_usage_payload(payload)
    if not usage_payload:
        return None

    prompt_tokens = _int_or_zero(
        usage_payload.get("prompt_tokens")
        or usage_payload.get("input_tokens")
        or usage_payload.get("promptTokens")
        or usage_payload.get("inputTokens"),
    )
    completion_tokens = _int_or_zero(
        usage_payload.get("completion_tokens")
        or usage_payload.get("output_tokens")
        or usage_payload.get("completionTokens")
        or usage_payload.get("outputTokens"),
    )
    total_tokens = _int_or_zero(
        usage_payload.get("total_tokens")
        or usage_payload.get("totalTokens")
        or (prompt_tokens + completion_tokens),
    )

    if prompt_tokens <= 0 and completion_tokens <= 0 and total_tokens <= 0:
        return None

    usage_event = TokenUsageEvent.objects.create(
        run=run,
        session_id=session_id,
        endpoint=endpoint,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        raw_usage=dict(usage_payload),
    )
    OrchestrationRun.objects.filter(pk=run.pk).update(
        prompt_tokens=F("prompt_tokens") + prompt_tokens,
        completion_tokens=F("completion_tokens") + completion_tokens,
        total_tokens=F("total_tokens") + total_tokens,
    )
    run.refresh_from_db(fields=["prompt_tokens", "completion_tokens", "total_tokens", "updated_at"])
    return usage_event


def _extract_usage_payload(payload: Any) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        usage_value = payload.get("usage")
        if isinstance(usage_value, Mapping):
            return usage_value
        for value in payload.values():
            nested = _extract_usage_payload(value)
            if nested:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _extract_usage_payload(item)
            if nested:
                return nested
    return None


def _int_or_zero(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return max(parsed, 0)
