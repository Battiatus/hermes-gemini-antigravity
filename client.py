"""Inference client: Hermes' native Gemini translation (messages, tools, streaming chunks) on Antigravity's
v1internal endpoints, with the account's OAuth access token. Request and response shapes are Hermes' own, so
fixes to its Gemini adapter reach this provider too; only the wire around them is Antigravity's.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterator, Optional

from agent.gemini_native_adapter import (
    GeminiAPIError, GeminiNativeClient, build_gemini_request, translate_gemini_response, translate_stream_event,
)

from . import antigravity as ag

BASE_URL = "https://cloudcode-pa.googleapis.com/v1internal"
_sessions = ag.Sessions()
_catalog: dict[str, Any] = {"at": 0.0, "models": {}}
_catalog_lock = threading.Lock()


def catalog_models(access: str, *, force: bool = False) -> dict[str, dict]:
    """Google's catalog for this account ({id: info}), cached 30 minutes for the whole process."""
    with _catalog_lock:
        if force or time.time() - _catalog["at"] > 1800 or not _catalog["models"]:
            data = ag.fetch_catalog(access)
            _catalog.update(at=time.time(), models=data.get("models") or {}, offered=ag.offered_models(data))
        return _catalog["models"]


def _api_error(exc: ag.AntigravityError) -> GeminiAPIError:
    code = "antigravity_quota" if exc.status == 429 and not exc.retryable else "antigravity_error"
    return GeminiAPIError(str(exc), code=code, status_code=exc.status or (503 if exc.retryable else None), retry_after=exc.retry_after)


class AntigravityClient(GeminiNativeClient):
    """OpenAI-SDK-shaped facade (``client.chat.completions.create``) over Antigravity."""

    MISSING_KEY_ERROR = "Antigravity needs a signed-in Google account. Run `hermes auth add gemini-antigravity`."

    def __init__(self, *, base_url: Any = None, **kwargs: Any) -> None:
        super().__init__(base_url=base_url or BASE_URL, **kwargs)
        self._live: set[Any] = set()

    def cancel(self) -> None:
        """Interrupt from another thread: close the open connections so the request thread returns."""
        for conn in list(self._live):
            try:
                if conn.sock:
                    conn.sock.shutdown(2)
            except OSError:
                pass

    def _track(self, conn: Any) -> None:
        self._live.add(conn)

    def _create_chat_completion(
        self, *, model: str = "", messages: Optional[list[dict[str, Any]]] = None, stream: bool = False, tools: Any = None,
        tool_choice: Any = None, temperature: Optional[float] = None, max_tokens: Optional[int] = None, top_p: Optional[float] = None,
        stop: Any = None, response_format: Any = None, extra_body: Optional[dict[str, Any]] = None, timeout: Any = None, **_: Any,
    ) -> Any:
        extra = extra_body if isinstance(extra_body, dict) else {}
        self._live = set()  # one request at a time per client: forget the previous one's connections
        gemini = model.startswith("gemini")
        request = build_gemini_request(
            messages=messages or [], tools=tools, tool_choice=tool_choice, temperature=temperature, max_tokens=max_tokens,
            top_p=top_p, stop=stop, thinking_config=extra.get("thinking_config"), response_format=response_format,
            # Claude and GPT-OSS behind Antigravity also pair tool calls by id; Claude wants the plain JSON Schema
            model=model if gemini else "gemini-3", tools_as_json_schema=not gemini,
        )
        config = request["generationConfig"]
        if "thinkingConfig" not in config and (thinking := ag.thinking_for(model)):
            config["thinkingConfig"] = thinking
        try:
            limit = (catalog_models(self.api_key).get(model) or {}).get("maxOutputTokens")
        except ag.AntigravityError:
            limit = None
        if limit and config.get("maxOutputTokens", 0) > limit:
            config["maxOutputTokens"] = limit
        if system := request.get("systemInstruction"):
            request["systemInstruction"] = {"parts": system["parts"]}
        session_id, request_id = _sessions.begin(str(extra.get("antigravity_session") or f"call-{id(request)}-{time.time()}"))
        request["sessionId"] = session_id
        try:
            result = ag.generate(self.api_key, model, request, stream=stream, request_id=request_id,
                                 timeout=_seconds(timeout), on_connection=self._track)
        except ag.AntigravityError as exc:
            raise _api_error(exc) from exc
        if not stream:
            return translate_gemini_response(result, model=model)
        return self._chunks(model, result)

    def _chunks(self, model: str, events: Iterator[dict]) -> Iterator[Any]:
        indices: dict[str, dict[str, Any]] = {}
        try:
            for event in events:
                yield from translate_stream_event(event, model, indices)
        except OSError as exc:
            raise GeminiAPIError(f"Antigravity stream interrupted: {exc}", code="antigravity_stream_error") from exc


def _seconds(timeout: Any) -> float:
    if isinstance(timeout, (int, float)) and timeout > 0:
        return float(timeout)
    read = getattr(timeout, "read", None)
    return float(read) if isinstance(read, (int, float)) and read > 0 else 1200.0
