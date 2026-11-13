from __future__ import annotations
import asyncio
import json
import logging
from typing import Optional, Dict, Any
import aiohttp

from config import get_config, LLMConfig

logger = logging.getLogger(__name__)


def _iter_sse_payloads(raw: str):
    """Yield JSON strings from an SSE body, skipping comments and [DONE]."""
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if payload and payload != "[DONE]":
                yield payload


def _extract_completion_content(raw: str) -> str:
    """Return assistant text from a plain OpenAI body or an SSE stream.

    This gateway answers a non-streaming request with an event stream
    (`data: {...}` chunks followed by `data: [DONE]`). Concatenating delta
    chunks is the only way to recover the message; parsing the body as a
    single JSON object silently yields nothing and the judge then looks like
    an infrastructure timeout.
    """
    if not raw or not raw.strip():
        return ""

    text = raw.strip()
    # Fast path: a single JSON object, which is what a compliant server sends.
    if text.startswith("{"):
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = None
        if isinstance(body, dict):
            choice = (body.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content")
            if content is None:
                content = (choice.get("delta") or {}).get("content")
            if content is not None:
                return content if isinstance(content, str) else json.dumps(content)
            if isinstance(body.get("response"), str):
                return body["response"]

    fragments = []
    for payload in _iter_sse_payloads(text):
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, dict):
            continue
        choice = (chunk.get("choices") or [{}])[0]
        piece = (choice.get("delta") or {}).get("content")
        if piece is None:
            piece = (choice.get("message") or {}).get("content")
        if piece is None:
            piece = (choice.get("text") if isinstance(choice.get("text"), str) else None)
        if piece:
            fragments.append(piece)
    return "".join(fragments)

class LLMClient:
    def __init__(self):
        self.config = get_config().llm
        self._session = None
        self._base_url = self.config.base_url or {
            'openrouter': 'https://openrouter.ai/api/v1',
            'anthropic': 'https://api.anthropic.com',
            'openai': 'https://api.openai.com/v1',
        }.get(self.config.provider, 'https://api.openai.com/v1')

    async def _get_session(self):
        """Return a session bound to the currently running event loop.

        The scanner is synchronous and invokes the async client through
        ``asyncio.run``. That creates a new loop for each judge call. Reusing
        an aiohttp session created by the previous loop raises ``Event loop is
        closed`` and turns healthy responses into false judge errors.
        """
        loop = asyncio.get_running_loop()
        session_loop = getattr(self._session, "_loop", None) if self._session else None
        if self._session is not None and (
            self._session.closed or session_loop is None or session_loop.is_closed()
            or session_loop is not loop
        ):
            # A session owned by a closed loop cannot be awaited for cleanup
            # from this loop. Detach it before replacing it; normal calls are
            # still closed by llm_gate in the loop that owns them.
            try:
                if not self._session.closed:
                    self._session.detach()
            except Exception:
                logger.debug("stale LLM session detach failed", exc_info=True)
            self._session = None

        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _request(self, url: str, payload: Dict[str, Any], timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
        started = asyncio.get_running_loop().time()
        try:
            session = await self._get_session()
            req_timeout = aiohttp.ClientTimeout(total=float(timeout_s)) if timeout_s else None
            headers = {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            
            if self.config.provider == 'anthropic':
                headers['x-api-key'] = self.config.api_key
                headers.pop('Authorization')
                payload['model'] = self.config.model
                payload['max_tokens'] = self.config.max_tokens
                payload['temperature'] = self.config.temperature
            elif self.config.provider == 'openrouter':
                headers['HTTP-Referer'] = 'https://vilona-v2.dev'
                headers['X-Title'] = 'Vilona AI v2'
                payload['model'] = self.config.model
                payload['max_tokens'] = self.config.max_tokens
                payload['temperature'] = self.config.temperature
            else:
                payload['model'] = self.config.model
                payload['max_tokens'] = self.config.max_tokens
                payload['temperature'] = self.config.temperature

            async with session.post(url, json=payload, headers=headers, timeout=req_timeout) as resp:
                headers_s = asyncio.get_running_loop().time() - started
                logger.info(
                    "LLM headers received status=%s in %.2fs", resp.status, headers_s
                )
                request_id = resp.headers.get("x-request-id") or resp.headers.get("request-id")
                if resp.status != 200:
                    error_body = await resp.text()
                    logger.error(
                        "LLM API error status=%s request_id=%s body_chars=%d headers_s=%.2f",
                        resp.status, request_id, len(error_body), headers_s,
                    )
                    return None
                raw = await resp.text()
                body_s = asyncio.get_running_loop().time() - started
                logger.info(
                    "LLM body received %d chars in %.2fs (headers took %.2fs)",
                    len(raw), body_s, headers_s,
                )
                try:
                    body = json.loads(raw)
                    if isinstance(body, dict):
                        return body
                    logger.warning("LLM response malformed kind=%s request_id=%s", type(body).__name__, request_id)
                    return None
                except json.JSONDecodeError:
                    content = _extract_completion_content(raw)
                    if not content:
                        logger.warning(
                            "LLM response empty_or_unparseable body_chars=%d request_id=%s",
                            len(raw), request_id,
                        )
                        return None
                    logger.info("LLM SSE content parsed body_chars=%d request_id=%s", len(raw), request_id)
                    # Normalize SSE into the same shape used by generate_signal.
                    return {"choices": [{"message": {"content": content}}]}
        except Exception as e:
            logger.error("LLM request failed: %s: %s", type(e).__name__, e)
            return None

    async def generate_signal(
        self,
        system_prompt: str,
        user_prompt: str,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        retries: int = 1,
    ) -> Optional[str]:
        """
        Generate a trading signal from LLM.
        Returns raw response text.
        """
        model = model or self.config.model
        temperature = temperature or self.config.temperature
        max_tokens = max_tokens or self.config.max_tokens

        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        url = {
            'openrouter': f'{self._base_url}/chat/completions',
            'anthropic': f'{self._base_url}/v1/messages',
            'openai': f'{self._base_url}/chat/completions',
        }.get(self.config.provider, f'{self._base_url}/chat/completions')

        if self.config.provider == 'anthropic':
            payload = {
                "model": model,
                "messages": [
                    {"role": "user", "content": user_prompt},
                ],
                "system": system_prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }

        # Retry one transient gateway failure. Rebuild the payload each time
        # because provider-specific request preparation mutates it in _request.
        # The per-attempt cap is half the total budget so the retry still fits
        # inside the caller's wait_for(timeout_s) in core/llm_gate.py.
        attempts = max(0, int(retries)) + 1
        total_budget = float(self.config.timeout or 30)
        per_attempt = total_budget / attempts * 0.9
        result = None
        for attempt in range(attempts):
            result = await self._request(url, dict(payload), timeout_s=per_attempt)
            if result:
                break
            if attempt + 1 < attempts:
                logger.warning("LLM empty/failed response; retrying attempt %d/%d", attempt + 2, attempts)
                await asyncio.sleep(0.25)
        if not result:
            return None

        if self.config.provider == 'anthropic':
            return result.get('content', [{}])[0].get('text', '')
        elif self.config.provider in ('openai', 'openrouter'):
            return result.get('choices', [{}])[0].get('message', {}).get('content', '')
        else:
            return result.get('response', '')

    async def close(self):
        session = self._session
        self._session = None
        if not session:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        session_loop = getattr(session, "_loop", None)
        if session.closed or session_loop is None or session_loop.is_closed():
            return
        if loop is not None and session_loop is not loop:
            # Owned by a different loop; detaching avoids cross-loop awaits.
            session.detach()
            return
        await session.close()


_llm_client: Optional[LLMClient] = None


def get_llm_client() -> LLMClient:
    global _llm_client
    if _llm_client is None:
        _llm_client = LLMClient()
    return _llm_client
