"""Base class for browser-automation providers.

A provider drives one web chat interface through a real browser session and
exposes it to the router through a uniform interface: start/close/health,
new_chat, and prompt sending (non-streaming + streaming).

Concrete providers subclass this and define the *site-specific* parts:
- target URL and login detection
- DOM selectors for input/send/response capture
- response parsing (SSE shape, cleanup of UI chrome text)

All shared browser plumbing lives here: persistent-context launch, CDP stream
interception, request serialization (one browser = one request at a time),
inactivity auto-refresh/restart, and graceful close.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncGenerator
from pathlib import Path

from playwright.async_api import BrowserContext, CDPSession, Page, async_playwright

logger = logging.getLogger("browser_api.providers")


class ResponseStatus:
    OK = "ok"
    RATE_LIMITED = "rate_limited"
    AUTH_REQUIRED = "auth_required"
    ERROR = "error"
    TIMEOUT = "timeout"


class BrowserProvider:
    """Base class. Subclasses set the class attributes and override parsing hooks."""

    # --- Site-specific configuration (set in subclasses) ---
    name: str = "base"
    target_url: str = "about:blank"
    login_url_fragment: str = ""          # e.g. "login.example.com"
    ready_url_fragment: str = ""          # substring of target URL used for health page_loaded
    stream_url_fragment: str = ""         # substring identifying the response stream URL
    input_selectors: list[str] = []
    send_selectors: list[str] = []
    response_selectors: list[str] = []
    dom_fallback_wait_selector: str = ""
    stop_button_selector: str = ""
    drop_lines: set[str] = set()          # UI chrome lines stripped from captured text
    strip_prefixes: list[str] = []        # leading prefixes stripped from captured text

    def __init__(self, profile_dir: Path, headless: bool = True):
        self.profile_dir = Path(profile_dir)
        self.headless = headless
        self._playwright = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._cdp: CDPSession | None = None
        self._stream_request_id: str | None = None
        self._stream_done = asyncio.Event()
        self._stream_error: str | None = None
        self._last_activity: float = time.monotonic()
        self._auto_refresh_interval: float = 180.0
        self._auto_restart_interval: float = 900.0
        # One browser session, one request at a time: CDP stream interception
        # state is shared instance state, so concurrent calls would corrupt it.
        self._request_lock = asyncio.Lock()

    def _mark_activity(self):
        self._last_activity = time.monotonic()

    def _reset_stream_state(self):
        """Clear per-request CDP stream state.

        A stale event from a previous request (e.g. a leftover finished
        ``_stream_done`` or an old ``_stream_error``) must not be mistaken
        for this request's stream, otherwise the caller may break early or
        report a bogus error before the new response starts arriving.
        """
        self._stream_request_id = None
        self._stream_done.clear()
        self._stream_error = None

    def _is_dead_page_error(self, e: Exception) -> bool:
        """True when the browser/page/context is gone and the session must be rebuilt."""
        msg = str(e).lower()
        dead_markers = (
            "target closed", "browser closed", "context closed",
            "browser context already closed", "target crashed",
            "connection closed", "page closed",
        )
        return any(m in msg for m in dead_markers)

    async def _mark_dead(self):
        """Rebuild path for a provider whose browser session died at runtime."""
        logger.info(f"[{self.name}] Marking provider dead — next request will rebuild")
        self._stream_request_id = None
        await self.close()

    async def _check_inactivity(self):
        # Called from a background task (not from inside the request lock),
        # so the lock check here is meaningful: never restart mid-request.
        if self._request_lock.locked():
            return
        if not self._page or self._page.is_closed():
            return
        elapsed = time.monotonic() - self._last_activity
        if elapsed >= self._auto_restart_interval:
            logger.info(f"[{self.name}] Auto-restarting after {elapsed:.0f}s inactivity")
            await self.close()
            await self.start()
        elif elapsed >= self._auto_refresh_interval:
            logger.info(f"[{self.name}] Auto-refreshing to new chat after {elapsed:.0f}s inactivity")
            await self.new_chat()

    async def start(self) -> BrowserProvider:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # The profile dir holds live session cookies — restrict access.
        try:
            os.chmod(self.profile_dir, 0o700)
        except OSError:
            pass
        # Do not log the full profile path (it contains the OS username).
        logger.info(f"[{self.name}] Launching Chrome (headless={self.headless})")

        self._playwright = await async_playwright().start()
        launch_options = {
            "user_data_dir": str(self.profile_dir),
            "headless": self.headless,
            "args": [
                "--disable-features=ChromeWhatsNewUI,TranslateUI",
                "--no-default-browser-check",
                "--no-first-run",
            ],
            "viewport": {"width": 1280, "height": 900},
        }
        chrome_executable = os.environ.get("BROWSER_API_CHROME_EXECUTABLE", "").strip()
        if chrome_executable:
            launch_options["executable_path"] = chrome_executable
            logger.debug(f"[{self.name}] Using dedicated Chrome executable: {chrome_executable}")
        else:
            launch_options["channel"] = "chrome"

        self._context = await self._playwright.chromium.launch_persistent_context(
            **launch_options,
        )

        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

        await self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=30000)
        logger.info(f"[{self.name}] Navigated to target")

        await self._wait_for_page_ready()
        await self._setup_cdp_interception()

        return self

    async def new_chat(self):
        async with self._request_lock:
            await self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=30000)
            await self._wait_for_page_ready()
            logger.info(f"[{self.name}] New chat started")

    async def _wait_for_page_ready(self, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                await self._page.wait_for_selector(
                    'div[contenteditable="true"], [role="textbox"], textarea',
                    timeout=5000,
                )
                logger.info(f"[{self.name}] Input area found")
                return
            except Exception:
                pass

            if self.login_url_fragment and self.login_url_fragment in self._page.url:
                logger.info(f"[{self.name}] Login page detected — waiting for manual login...")
                await self._page.wait_for_url(
                    f"{self.target_url}/**", timeout=120000
                )
                continue

            await asyncio.sleep(1)

        raise TimeoutError("Page did not load input area within timeout")

    async def _setup_cdp_interception(self):
        if self._cdp:
            try:
                self._cdp.remove_listener("Network.responseReceived", self._on_response_received)
                self._cdp.remove_listener("Network.loadingFinished", self._on_loading_finished)
                self._cdp.remove_listener("Network.loadingFailed", self._on_loading_failed)
            except Exception:
                pass
        else:
            self._cdp = await self._page.context.new_cdp_session(self._page)
            await self._cdp.send("Network.enable")

        self._cdp.on("Network.responseReceived", self._on_response_received)
        self._cdp.on("Network.loadingFinished", self._on_loading_finished)
        self._cdp.on("Network.loadingFailed", self._on_loading_failed)

    def _is_target_stream(self, params: dict) -> bool:
        response = params.get("response", {})
        url = response.get("url", "")
        mime = response.get("mimeType", "")
        status = response.get("status", 0)

        if status >= 400:
            return False
        if mime == "text/event-stream":
            return True
        if self.stream_url_fragment and self.stream_url_fragment in url:
            return True
        return False

    def _on_response_received(self, params: dict):
        if self._is_target_stream(params):
            rid = params.get("requestId", "")
            logger.info(f"[{self.name}] Intercepted stream: requestId={rid}")
            self._stream_request_id = rid
            self._stream_done.clear()
            self._stream_error = None

    def _on_loading_finished(self, params: dict):
        if params.get("requestId") == self._stream_request_id:
            logger.info(f"[{self.name}] Stream finished")
            self._stream_done.set()

    def _on_loading_failed(self, params: dict):
        if params.get("requestId") == self._stream_request_id:
            error = params.get("errorText", "Unknown error")
            logger.error(f"[{self.name}] Stream failed: {error}")
            self._stream_error = error
            self._stream_done.set()

    async def _capture_response_body(self) -> tuple[str, str]:
        if not self._stream_request_id or not self._cdp:
            logger.info(f"[{self.name}] No CDP stream captured, using DOM fallback")
            return await self._dom_fallback()

        try:
            await asyncio.wait_for(self._stream_done.wait(), timeout=120.0)
        except asyncio.TimeoutError:
            return ResponseStatus.TIMEOUT, "Response timed out"

        if self._stream_error:
            return ResponseStatus.ERROR, self._stream_error

        try:
            result = await self._cdp.send(
                "Network.getResponseBody",
                {"requestId": self._stream_request_id},
            )
        except Exception as e:
            logger.warning(f"[{self.name}] getResponseBody failed, using DOM fallback: {e}")
            return await self._dom_fallback()

        body = result.get("body", "")
        if not body:
            logger.warning(f"[{self.name}] Empty response body, using DOM fallback")
            return await self._dom_fallback()

        return self.parse_sse_body(body)

    def parse_sse_body(self, body: str) -> tuple[str, str]:
        """Parse a provider SSE body into (status, content). Override per provider."""
        raise NotImplementedError

    async def _dom_fallback(self) -> tuple[str, str]:
        # Wait for the newest response node to appear (count-based, avoids stale reads)
        try:
            await self._page.wait_for_selector(
                self.dom_fallback_wait_selector,
                state="attached",
                timeout=5000,
            )
        except Exception:
            pass
        # Wait for the stop button to detach = generation complete (short prompts finish fast)
        try:
            await self._page.wait_for_selector(
                self.stop_button_selector,
                state="detached",
                timeout=90000,
            )
        except Exception:
            pass
        # Give the markdown node time to render full text after completion; longer prompts need more time.
        for _ in range(60):
            await asyncio.sleep(0.5)
            try:
                current = await self._extract_response_text()
                if current:
                    return ResponseStatus.OK, current
            except Exception:
                pass
        return ResponseStatus.TIMEOUT, "No response captured via DOM"

    async def _find_and_fill_input(self, text: str):
        matched_selector: str | None = None
        for sel in self.input_selectors:
            try:
                element = await self._page.wait_for_selector(
                    sel, state="visible", timeout=3000
                )
                if element:
                    matched_selector = sel
                    break
            except Exception:
                continue

        if matched_selector is None:
            raise RuntimeError("Could not find input element on the page")

        for attempt in range(3):
            try:
                await element.click(timeout=3000)
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5)
                element = await self._page.wait_for_selector(
                    matched_selector,
                    state="visible", timeout=3000
                )
                if not element:
                    raise RuntimeError("Input element detached and could not be re-queried")

        await asyncio.sleep(0.1)
        # Re-query before fill: the web app re-renders the DOM after click, which detaches
        # the old element handle ("Element is not attached to the DOM").
        element = await self._page.wait_for_selector(
            matched_selector,
            state="visible", timeout=3000
        )
        if not element:
            raise RuntimeError("Input element detached and could not be re-queried")
        await element.fill(text)

    async def _find_and_click_send(self):
        for attempt in range(3):
            for sel in self.send_selectors:
                try:
                    btn = await self._page.wait_for_selector(
                        sel, state="visible", timeout=3000
                    )
                    if btn:
                        await btn.click(timeout=5000)
                        return
                except Exception:
                    continue
            await asyncio.sleep(0.5)

        logger.warning(f"[{self.name}] Send button selectors all failed, pressing Enter")
        await self._page.keyboard.press("Enter")
        await asyncio.sleep(1)

    async def send_prompt(self, prompt: str, new_chat: bool = False) -> dict:
        async with self._request_lock:
            try:
                self._reset_stream_state()
                await self._check_inactivity()
                self._mark_activity()
                if new_chat:
                    await self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=30000)
                    await self._wait_for_page_ready()
                    logger.info(f"[{self.name}] New chat started")
                await self._setup_cdp_interception()

                await self._find_and_fill_input(prompt)
                await asyncio.sleep(0.3)
                await self._find_and_click_send()

                status, content = await self._capture_response_body()

                return {
                    "status": status,
                    "content": content,
                    "prompt": prompt,
                }
            except Exception as e:
                logger.exception(f"[{self.name}] send_prompt failed")
                if self._is_dead_page_error(e):
                    await self._mark_dead()
                return {
                    "status": ResponseStatus.ERROR,
                    "content": str(e),
                    "prompt": prompt,
                }
            finally:
                self._stream_request_id = None

    async def send_prompt_streaming(self, prompt: str, new_chat: bool = False) -> AsyncGenerator[dict, None]:
        async with self._request_lock:
            async for chunk in self._send_prompt_streaming_locked(prompt, new_chat):
                yield chunk

    async def _send_prompt_streaming_locked(self, prompt: str, new_chat: bool = False) -> AsyncGenerator[dict, None]:
        try:
            self._reset_stream_state()
            await self._check_inactivity()
            self._mark_activity()
            if new_chat:
                await self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=30000)
                await self._wait_for_page_ready()
                logger.info(f"[{self.name}] New chat started")
            await self._setup_cdp_interception()

            baseline = await self._extract_response_text()

            await self._find_and_fill_input(prompt)
            await asyncio.sleep(0.3)
            await self._find_and_click_send()

            previous_text = ""
            stable_count = 0
            start_time = time.monotonic()

            for _ in range(400):
                await asyncio.sleep(0.3)
                try:
                    current = await self._extract_response_text()
                    current_html = await self._extract_response_html()
                    if baseline and current.startswith(baseline):
                        current = current[len(baseline):].strip()
                    if current and current != previous_text:
                        new_text = current[len(previous_text):] if previous_text and current.startswith(previous_text) else current
                        yield {"delta": new_text, "status": ResponseStatus.OK, "html": current_html}
                        previous_text = current
                        stable_count = 0
                    elif current and current == previous_text:
                        stable_count += 1
                        if stable_count >= 30 and await self._is_response_complete():
                            yield {"delta": "", "status": ResponseStatus.OK, "html": current_html}
                            break
                except Exception:
                    pass

                if not previous_text and time.monotonic() - start_time > 25:
                    logger.info(f"[{self.name}] No response after 25s, refreshing page")
                    await self._page.goto(self.target_url, wait_until="domcontentloaded", timeout=30000)
                    await self._wait_for_page_ready()
                    break

                if self._stream_done.is_set():
                    break

            if not previous_text:
                status, content = await self._dom_fallback()
                yield {"delta": content, "status": status}

        except Exception as e:
            logger.exception(f"[{self.name}] send_prompt_streaming failed")
            if self._is_dead_page_error(e):
                await self._mark_dead()
            yield {"delta": str(e), "status": ResponseStatus.ERROR}
        finally:
            self._stream_request_id = None

    async def _extract_response_text(self) -> str:
        for sel in self.response_selectors:
            try:
                elements = await self._page.query_selector_all(sel)
                if elements:
                    text = await elements[-1].evaluate(
                        """(node) => {
                            const clone = node.cloneNode(true);
                            ['model-response-label-announcer',
                             '.cdk-visually-hidden',
                             '.visually-hidden',
                             'thought-disclosure',
                             'collapsible-thought',
                             '.thought-container',
                             '[aria-live]',
                             '.processing-state-visible',
                             '.stop-generation-button'
                            ].forEach(s => { try { clone.querySelectorAll(s).forEach(el => el.remove()); } catch(e){} });
                            return clone.innerText || clone.textContent || '';
                        }"""
                    )
                    if text:
                        cleaned = self._clean_response(text)
                        if cleaned:
                            return cleaned
            except Exception:
                continue
        return ""

    async def _extract_response_html(self) -> str:
        for sel in self.response_selectors:
            try:
                elements = await self._page.query_selector_all(sel)
                if elements:
                    html = await elements[-1].evaluate(
                        """(node) => {
                            const clone = node.cloneNode(true);
                            ['model-response-label-announcer',
                             '.cdk-visually-hidden',
                             '.visually-hidden',
                             'thought-disclosure',
                             'collapsible-thought',
                             '.thought-container',
                             '[aria-live]',
                             '.processing-state-visible'
                            ].forEach(s => { try { clone.querySelectorAll(s).forEach(el => el.remove()); } catch(e){} });
                            return clone.innerHTML || '';
                        }"""
                    )
                    if html and html.strip():
                        cleaned = self._clean_html(html)
                        if cleaned:
                            return cleaned
            except Exception:
                continue
        return ""

    @staticmethod
    def _clean_html(html: str) -> str:
        import re
        html = re.sub(r'\s*_ngcontent-[a-z0-9-]+="?"?', '', html)
        html = re.sub(r'\s*_nghost-[a-z0-9-]+="?"?', '', html)
        html = html.replace('<!---->', '')
        html = re.sub(r'>\s+<', '>\n<', html)
        return html.strip()

    async def _is_response_complete(self) -> bool:
        try:
            input_el = await self._page.query_selector(
                '[contenteditable], [role="textbox"], rich-textarea, textarea'
            )
            if input_el:
                text = (await input_el.text_content() or "").strip()
                return text == ""
        except Exception:
            pass
        return False

    @staticmethod
    def _clean_response(text: str) -> str:
        text = text.strip()
        # Drop residual screen-reader / announcer lines anywhere in the text
        drop_lines = {
            "initiating initial assessment", "initial assessment",
            "searching the web", "searching...", "working...",
            "thinking...", "generating...", "analyzing...",
            "stop generating",
        }
        lines = text.split("\n")
        kept = []
        for line in lines:
            stripped = " ".join(line.split())
            if stripped.lower() in drop_lines:
                continue
            kept.append(stripped)
        text = "\n".join(kept)
        # Also strip leading provider-brand prefixes if on the first line
        prefixes = ["says: "]
        for p in prefixes:
            if text.startswith(p):
                text = text[len(p):]
                break
        stripped = text.strip()
        if not stripped:
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

    async def health_check(self) -> dict:
        try:
            if not self._page:
                return {"browser_alive": False, "page_loaded": False}
            url = self._page.url
            self._mark_activity()
            return {
                "browser_alive": True,
                "page_loaded": self.ready_url_fragment in url,
                "url": url,
            }
        except Exception:
            return {"browser_alive": False, "page_loaded": False}

    async def close(self):
        if self._cdp:
            try:
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
        if self._context:
            try:
                await self._context.close()
            except Exception:
                pass
            self._context = None
        self._page = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._stream_request_id = None
        logger.info(f"[{self.name}] Provider closed")
