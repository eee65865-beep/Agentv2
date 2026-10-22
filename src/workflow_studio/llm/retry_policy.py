"""Pinned SDK retry hook: only transient HTTP failures may be retried."""

from __future__ import annotations
from typing import TYPE_CHECKING

from openai import OpenAI
from workflow_studio.budget import current_budget

if TYPE_CHECKING:
    from httpx2 import Response


class DeepSeekRetryClient(OpenAI):
    """Injected through ChatDeepSeek's supported root_client/client fields.

    The SDK owns backoff and network/timeout retry limits. Its version-pinned
    HTTP hook additionally refuses 400/401/403/422 even with retry advice.
    """
    def _prepare_request(self, request) -> None:
        tracker = current_budget()
        if tracker is not None:
            remaining = tracker.reserve_http()
            timeout = request.extensions.get("timeout", {})
            request.extensions["timeout"] = {
                key: min(value, remaining) if value is not None else remaining
                for key, value in timeout.items()
            }
        return super()._prepare_request(request)

    def _should_retry(self, response: Response) -> bool:
        if response.status_code not in (408, 409, 429) and response.status_code < 500:
            return False
        return super()._should_retry(response)
