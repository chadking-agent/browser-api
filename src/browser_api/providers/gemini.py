"""Gemini provider: drives gemini.google.com through a signed-in Chrome profile."""
from __future__ import annotations

import json

from browser_api.providers.base import BrowserProvider, ResponseStatus


class GeminiProvider(BrowserProvider):
    name = "gemini"
    target_url = "https://gemini.google.com/app"
    login_url_fragment = "accounts.google.com"
    ready_url_fragment = "gemini.google.com"
    stream_url_fragment = "streamGenerateContent"
    input_selectors = [
        'div[contenteditable="true"]',
        '[role="textbox"]',
        'rich-textarea',
        'textarea',
    ]
    send_selectors = [
        'button[aria-label="Send message"]',
        'button[aria-label="Send"]',
        '[data-testid="send-button"]',
        'button.send-button',
    ]
    response_selectors = [
        'message-content .markdown',
        'message-content',
        '.response-content',
        '[data-message-content]',
        'model-response',
        '[data-response]',
    ]
    dom_fallback_wait_selector = 'model-response, [data-message-id]'
    stop_button_selector = 'button[aria-label*="Stop"], button[aria-label*="Stop generating"]'
    drop_lines = {
        "gemini said", "gemini",
        "initiating initial assessment", "initial assessment",
        "searching the web", "searching...", "working...",
        "thinking...", "generating...", "analyzing...",
        "stop generating",
    }
    strip_prefixes = ["Gemini said ", "Gemini said\n", "Gemini\nsaid\n", "says: "]

    def parse_sse_body(self, body: str) -> tuple[str, str]:
        content_parts = []
        sse_status = ResponseStatus.OK

        for line in body.split("\n"):
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue

            try:
                data = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        text = self._extract_text_from_chunk(item)
                        if text:
                            content_parts.append(text)
            elif isinstance(data, dict):
                if self._is_rate_limited(data):
                    sse_status = ResponseStatus.RATE_LIMITED
                    content_parts.append("Rate limited. Please wait before trying again.")
                elif self._is_error_response(data):
                    sse_status = ResponseStatus.ERROR
                    content_parts.append(data.get("error", {}).get("message", "Unknown API error"))
                else:
                    text = self._extract_text_from_chunk(data)
                    if text:
                        content_parts.append(text)

        if not content_parts:
            return ResponseStatus.ERROR, "No content extracted from response"

        return sse_status, "".join(content_parts)

    @staticmethod
    def _extract_text_from_chunk(data: dict) -> str:
        try:
            candidates = data.get("candidates", [])
            for c in candidates:
                c = c if isinstance(c, dict) else {}
                content = c.get("content", {}) if isinstance(c, dict) else {}
                parts = content.get("parts", [])
                for p in parts:
                    if isinstance(p, dict) and "text" in p:
                        return p["text"]
            return data.get("text", "")
        except Exception:
            return ""

    @staticmethod
    def _is_error_response(data: dict) -> bool:
        if "error" in data:
            return True
        finish_reason = ""
        try:
            finish_reason = data.get("candidates", [{}])[0].get("finishReason", "")
        except (IndexError, KeyError, TypeError):
            pass
        return finish_reason in ("SAFETY", "RECITATION", "OTHER")

    @staticmethod
    def _is_rate_limited(data: dict) -> bool:
        err = data.get("error", {})
        if isinstance(err, dict):
            code = err.get("code", 0)
            if code in (429, 503):
                return True
        return False

    @staticmethod
    def _clean_response(text: str) -> str:
        # Gemini-specific override: strips the "Gemini said" UI chrome that
        # sometimes appears at the start of the captured response node.
        text = text.strip()
        lines = text.split("\n")
        kept = []
        for line in lines:
            stripped = " ".join(line.split())
            if stripped.lower() in GeminiProvider.drop_lines:
                continue
            kept.append(stripped)
        text = "\n".join(kept)
        for p in GeminiProvider.strip_prefixes:
            if text.startswith(p):
                text = text[len(p):]
                break
        stripped = text.strip()
        if stripped in {"", "Gemini", "Gemini said"}:
            return ""
        lines = text.split("\n")
        cleaned = []
        blank_count = 0
        for line in lines:
            stripped = " ".join(line.split())
            if stripped == "":
                blank_count += 1
                if blank_count <= 1:
                    cleaned.append("")
            else:
                blank_count = 0
                cleaned.append(stripped)
        return "\n".join(cleaned).strip()
