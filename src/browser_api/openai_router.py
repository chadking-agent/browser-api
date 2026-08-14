from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from browser_api import __version__
from browser_api.config import (
    ensure_dirs,
    get_log_dir,
    get_profile_dir,
)
from browser_api.providers import (
    PROVIDERS,
    default_provider_name,
    get_provider_class,
    provider_model_ids,
)
from browser_api.providers.base import ResponseStatus

logger = logging.getLogger("openai_router")

# Loopback-only by default. Override with BROWSER_API_HOST if you
# deliberately want LAN access (see README warning: the server has no
# authentication unless BROWSER_API_API_KEY is set).
HOST = os.environ.get("BROWSER_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("BROWSER_API_PORT", "54706"))

# Optional bearer token. If set, every route requires
# `Authorization: Bearer <key>` (constant-time compare). Unset = no auth,
# which is only safe while bound to loopback.
API_KEY = os.environ.get("BROWSER_API_API_KEY", "")

# The default provider (used by the un-namespaced /v1/chat/completions route).
DEFAULT_PROVIDER = default_provider_name()


def _validate_default_provider():
    if DEFAULT_PROVIDER not in PROVIDERS:
        raise RuntimeError(
            f"BROWSER_API_PROVIDER={DEFAULT_PROVIDER!r} is not a registered provider. "
            f"Available: {', '.join(sorted(PROVIDERS))}"
        )


_validate_default_provider()


class ProviderState:
    """Lifecycle state for one provider's browser session."""

    def __init__(self, name: str):
        self.name = name
        self.bridge = None
        self.ready = asyncio.Event()
        self.start_task: asyncio.Task | None = None
        self.failed = False

    @property
    def default_model(self) -> str:
        return provider_model_ids(self.name)[0]

    @property
    def model_aliases(self) -> list[str]:
        return provider_model_ids(self.name)[1]


# One provider state per registered provider, created lazily on first use.
_state: dict[str, ProviderState] = {}


def _provider_state(name: str) -> ProviderState:
    if name not in _state:
        _state[name] = ProviderState(name)
    return _state[name]


def _is_headless() -> bool:
    val = os.environ.get("BROWSER_API_HEADLESS", "true").lower()
    return val not in ("false", "0", "no")


async def _start_bridge(state: ProviderState):
    profile_dir = get_profile_dir(state.name)
    headless = _is_headless()
    try:
        cls = get_provider_class(state.name)
        state.bridge = cls(profile_dir=profile_dir, headless=headless)
        await state.bridge.start()
        state.ready.set()
        logger.info(f"Bridge ready ({state.name}, headless={headless})")
    except Exception as e:
        logger.error(f"Bridge start failed ({state.name}): {e}")
        state.ready.clear()
        state.failed = True
        # Release any partially-acquired browser/profile resources so a retry
        # doesn't collide with a stale Chrome or profile lock.
        try:
            if state.bridge:
                await state.bridge.close()
        except Exception:
            pass
        state.bridge = None


async def _ensure_bridge(name: str):
    state = _provider_state(name)
    if state.ready.is_set():
        # Verify the session is actually alive (auto-restart/rebuild may have
        # closed it) before returning it as ready.
        if state.bridge and state.bridge._page and not state.bridge._page.is_closed():
            return state
        # Bridge died at runtime — rebuild on the next request.
        logger.info(f"Bridge {name} session gone — rebuilding")
        state.ready.clear()
        state.failed = False
        state.start_task = None
    if state.failed:
        # Start already failed — fail fast instead of burning 120s per request.
        raise HTTPException(503, f"Bridge failed to start ({name}; check Chrome/Playwright install)")
    if state.start_task is None or state.start_task.done():
        state.start_task = asyncio.create_task(_start_bridge(state))
    try:
        await asyncio.wait_for(state.ready.wait(), timeout=120.0)
    except asyncio.TimeoutError:
        logger.error(f"Bridge failed to become ready within 120s ({name})")
        state.failed = True
        raise HTTPException(503, f"Bridge failed to start ({name}; check Chrome/Playwright install)")
    return state


def _require_provider(name: str) -> str:
    if name not in PROVIDERS:
        raise HTTPException(
            404,
            f"Unknown provider: {name!r} (available: {', '.join(sorted(PROVIDERS))})",
        )
    return name


@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_dirs()

    log_dir = get_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "api_server.log"

    fh = logging.FileHandler(log_file)
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    ))
    try:
        os.chmod(log_file, 0o600)
        os.chmod(log_dir, 0o700)
    except OSError:
        pass
    logging.getLogger().addHandler(fh)

    logger.info(f"Starting browser-api server (default provider: {DEFAULT_PROVIDER})")
    # Warm the default provider so /health is meaningful immediately.
    _provider_state(DEFAULT_PROVIDER).start_task = asyncio.create_task(
        _start_bridge(_provider_state(DEFAULT_PROVIDER))
    )

    # Background inactivity watchdog: refresh/restart wedged provider sessions.
    async def _inactivity_watchdog():
        while True:
            await asyncio.sleep(30)
            for st in list(_state.values()):
                if st.ready.is_set() and st.bridge:
                    try:
                        await st.bridge._check_inactivity()
                    except Exception:
                        logger.exception(f"Inactivity check failed ({st.name})")

    watchdog = asyncio.create_task(_inactivity_watchdog())

    yield

    logger.info("Shutting down")
    watchdog.cancel()
    for st in _state.values():
        if st.start_task and not st.start_task.done():
            st.start_task.cancel()
            try:
                await st.start_task
            except (asyncio.CancelledError, Exception):
                pass
        if st.bridge:
            try:
                await st.bridge.close()
            except Exception:
                pass


app = FastAPI(title="browser-api Router", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

MAX_BODY_BYTES = int(os.environ.get("BROWSER_API_MAX_BODY_BYTES", str(8 * 1024 * 1024)))


def _openai_error(status: int, message: str) -> JSONResponse:
    """OpenAI-style error envelope: {"error": {"message": ..., "type": ...}}."""
    etype = {
        400: "invalid_request_error",
        401: "authentication_error",
        404: "not_found_error",
        413: "invalid_request_error",
        422: "invalid_request_error",
        429: "rate_limit_error",
        500: "api_error",
        503: "api_error",
        504: "api_error",
    }.get(status, "api_error")
    return JSONResponse(
        {"error": {"message": message, "type": etype}},
        status_code=status,
    )


@app.exception_handler(HTTPException)
async def _http_exc_handler(request: Request, exc: HTTPException):
    return _openai_error(exc.status_code, str(exc.detail))


@app.exception_handler(RequestValidationError)
async def _validation_exc_handler(request: Request, exc: RequestValidationError):
    # Do not echo pydantic's input payload back to the client (it can be
    # large and may contain the caller's own data).
    return _openai_error(422, "Invalid request: check the request schema (see /help)")


@app.exception_handler(Exception)
async def _unhandled_exc_handler(request: Request, exc: Exception):
    logger.exception("Unhandled error serving %s", request.url.path)
    return _openai_error(500, "Internal server error")


@app.middleware("http")
async def _body_size_limit(request: Request, call_next):
    """Reject oversized request bodies (simple DoS guard)."""
    if request.method in ("POST", "PUT", "PATCH"):
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
            return _openai_error(413, "Request body too large")
    return await call_next(request)


def _require_auth(request: Request):
    """Optional bearer-token gate. No-op when BROWSER_API_API_KEY is unset."""
    if not API_KEY:
        return
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization: Bearer <key> header")
    provided = header[len("Bearer "):].strip()
    if not secrets.compare_digest(provided, API_KEY):
        raise HTTPException(401, "Invalid API key")


class Message(BaseModel):
    role: str
    content: str = Field(..., max_length=100_000)


class ChatRequest(BaseModel):
    messages: list[Message] = Field(..., max_length=64)
    model: str = Field("", max_length=200)
    stream: bool = False
    temperature: float | None = Field(None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(None, ge=1, le=1_000_000)
    new_chat: bool = True


def _safe_url(url: str) -> str:
    """Only host + scheme, never the full path (chat thread IDs leak info)."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
    except Exception:
        return ""


@app.get("/health", dependencies=[Depends(_require_auth)])
async def health():
    state = _provider_state(DEFAULT_PROVIDER)
    if state.bridge and state.ready.is_set():
        hc = await state.bridge.health_check()
        return {
            "status": "ok",
            "provider": DEFAULT_PROVIDER,
            "browser_alive": hc.get("browser_alive", False),
            "page_loaded": hc.get("page_loaded", False),
            "url": _safe_url(hc.get("url", "")),
        }
    elif state.start_task and not state.start_task.done():
        return {"status": "starting", "provider": DEFAULT_PROVIDER, "browser_alive": False}
    elif state.start_task and state.start_task.done():
        return {"status": "error", "provider": DEFAULT_PROVIDER, "browser_alive": False,
                "detail": "Bridge failed to start (check Chrome/Playwright install)"}
    return {"status": "ok", "provider": DEFAULT_PROVIDER, "browser_alive": False}


@app.get("/v1/models", dependencies=[Depends(_require_auth)])
async def list_models():
    state = _provider_state(DEFAULT_PROVIDER)
    return _models_response(state)


@app.get("/v1/{provider}/models", dependencies=[Depends(_require_auth)])
async def list_provider_models(provider: str):
    _require_provider(provider)
    return _models_response(_provider_state(provider))


def _models_response(state: ProviderState) -> dict:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": now, "owned_by": "browser-api"}
            for m in state.model_aliases
        ],
    }


@app.get("/v1/providers", dependencies=[Depends(_require_auth)])
async def list_providers():
    """List registered providers and the active default."""
    return {
        "object": "list",
        "default": DEFAULT_PROVIDER,
        "data": [
            {
                "id": name,
                "object": "provider",
                "models": _provider_state(name).model_aliases,
            }
            for name in sorted(PROVIDERS)
        ],
    }


@app.get("/help", dependencies=[Depends(_require_auth)])
@app.get("/v1/help", dependencies=[Depends(_require_auth)])
async def help_doc():
    """Human-readable API guide: endpoints, request/response shapes, usage examples."""
    return {
        "server": "browser-api (OpenAI-compatible proxy over a web chat interface via Playwright)",
        "version": __version__,
        "base_url": f"http://<host>:{PORT}",
        "default_provider": DEFAULT_PROVIDER,
        "providers": sorted(PROVIDERS),
        "summary": (
            "OpenAI-compatible proxy over real signed-in web chat sessions. "
            f"Default provider: {DEFAULT_PROVIDER}. Each provider runs its own "
            "browser profile. Each request is a real browser round-trip (5-30s latency)."
        ),
        "endpoints": {
            "GET /health": (
                "Liveness for the default provider. Returns {status, provider, browser_alive, page_loaded, url}. "
                "page_loaded=false means the browser is not logged in yet — run once "
                "with BROWSER_API_HEADLESS=false and complete login."
            ),
            "GET /v1/models": f"Model ids for the default provider ({DEFAULT_PROVIDER}).",
            "GET /v1/{provider}/models": "Model ids for a specific provider (e.g. /v1/gemini/models).",
            "GET /v1/providers": "List registered providers and their model ids.",
            "GET /help": "This page.",
            "POST /v1/chat/completions": f"OpenAI-compatible chat for the default provider ({DEFAULT_PROVIDER}).",
            "POST /v1/{provider}/chat/completions": "OpenAI-compatible chat for a specific provider (e.g. /v1/gemini/chat/completions).",
        },
        "chat_completions_schema": {
            "method": "POST /v1/chat/completions or POST /v1/{provider}/chat/completions",
            "body": {
                "model": "any string; informational",
                "messages": [
                    {"role": "user", "content": "your prompt here"},
                    {"role": "assistant", "content": "optional prior assistant turn"},
                ],
                "stream": False,
                "new_chat": True,
                "temperature": None,
                "max_tokens": None,
            },
            "new_chat_flag_explained": (
                "new_chat=true (DEFAULT): the provider opens a brand-new chat "
                "(navigates to a fresh page) before sending. "
                "Use this to start a fresh conversation with no history.\n"
                "new_chat=false: the provider continues in the CURRENT chat thread, so "
                "the web app sees the prior conversation context from the browser session.\n"
                "Multi-turn pattern: send new_chat=true on the first message, then "
                "new_chat=false on follow-ups. NOTE: when new_chat=false, the browser's "
                "actual prior turns are what the web app sees — your 'messages' array is only "
                "used to extract the last user message, not to reconstruct history."
            ),
            "stream_explained": (
                "stream=true returns Server-Sent Events: one 'data:' chunk per delta, "
                "a final chunk with finish_reason=stop, then [DONE]."
            ),
            "examples": {
                "curl_non_streaming": (
                    "curl -s http://localhost:54706/v1/chat/completions \\n"
                    "  -H 'Content-Type: application/json' \\n"
                    "  -d '{\"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]}'"
                ),
                "curl_provider_route": (
                    "curl -s http://localhost:54706/v1/gemini/chat/completions \\n"
                    "  -H 'Content-Type: application/json' \\n"
                    "  -d '{\"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]}'"
                ),
                "curl_streaming": (
                    "curl -N -s http://localhost:54706/v1/chat/completions \\n"
                    "  -H 'Content-Type: application/json' \\n"
                    "  -d '{\"stream\": true, \"messages\": [{\"role\": \"user\", \"content\": \"Hello\"}]}'"
                ),
                "curl_multi_turn": (
                    "# turn 1: new chat\n"
                    "curl -s http://localhost:54706/v1/chat/completions -H 'Content-Type: application/json' \\n"
                    "  -d '{\"messages\": [{\"role\": \"user\", \"content\": \"My name is Alex\"}]}'\n"
                    "# turn 2: same thread\n"
                    "curl -s http://localhost:54706/v1/chat/completions -H 'Content-Type: application/json' \\n"
                    "  -d '{\"new_chat\": false, \"messages\": [{\"role\": \"user\", \"content\": \"What is my name?\"}]}'"
                ),
                "openai_sdk": (
                    "from openai import OpenAI\n"
                    "client = OpenAI(base_url=\"http://localhost:54706/v1\", api_key=\"not-needed\")\n"
                    "resp = client.chat.completions.create(\n"
                    "    messages=[{\"role\": \"user\", \"content\": \"Hello\"}],\n"
                    ")\n"
                    "print(resp.choices[0].message.content)"
                ),
                "with_auth": (
                    "If BROWSER_API_API_KEY is set, add the header to every request:\n"
                    "  -H 'Authorization: Bearer <key>'"
                ),
            },
        },
        "config": {
            "BROWSER_API_PROVIDER": "default provider name (default: gemini)",
            "BROWSER_API_HOST": "127.0.0.1 (loopback default; keep it that way unless you set an API key)",
            "BROWSER_API_PORT": "54706",
            "BROWSER_API_API_KEY": "unset by default. If set, ALL routes require Authorization: Bearer <key>",
            "BROWSER_API_HEADLESS": "true. Set false for first-run login.",
            "BROWSER_API_CHROME_EXECUTABLE": "path to a specific Chrome/Chromium binary; default channel 'chrome'",
            "BROWSER_API_<PROVIDER>_MODEL": "per-provider default model id override (e.g. BROWSER_API_GEMINI_MODEL)",
        },
        "notes": [
            "This is a browser-automation bridge to web chat apps, not an official API.",
            f"Registered providers: {', '.join(sorted(PROVIDERS))}. Each runs its own browser profile.",
            "Each request does a real browser round-trip: ~5-30s latency.",
            "The provider uses the model currently selected in the signed-in browser profile.",
            "temperature/max_tokens are accepted for OpenAI-client compatibility but not honored (the browser uses the web app's own generation settings).",
            "No file upload support: the web apps only accept files via real user interaction.",
            "Read the WARNING at the top of the project README before use (provider ToS / account risk).",
        ],
    }


async def _chat_completions_for(state: ProviderState, req: ChatRequest):
    last_msg = ""
    for m in reversed(req.messages):
        if m.role == "user":
            last_msg = m.content
            break

    if not last_msg:
        raise HTTPException(400, "No user message found")

    # Echo the client's model id, or the provider's configured default when omitted.
    model = req.model or state.default_model

    # Wait for the provider's bridge (bounded 120s) before checking readiness.
    await _ensure_bridge(state.name)
    if not state.bridge:
        raise HTTPException(503, "Bridge failed to start")
    # Redacted logging: never log prompt content (may contain secrets/PII).
    logger.debug(f"Chat request: provider={state.name} model={req.model} stream={req.stream}")

    if req.stream:
        async def event_generator():
            request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
            created = int(time.time())
            errored = False
            async for chunk in state.bridge.send_prompt_streaming(last_msg, new_chat=req.new_chat):
                delta = chunk.get("delta", "")
                status = chunk.get("status", ResponseStatus.OK)
                if status != ResponseStatus.OK:
                    errored = True
                    yield {
                        "data": json.dumps({
                            "id": request_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": f"[{status}] {delta}"},
                                "finish_reason": None,
                            }],
                        })
                    }
                    break
                if delta:
                    yield {
                        "data": json.dumps({
                            "id": request_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {
                                    "content": delta,
                                    "html": chunk.get("html", ""),
                                },
                                "finish_reason": None,
                            }],
                        })
                    }
            if not errored:
                yield {
                    "data": json.dumps({
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }],
                    })
                }
            # Always terminate with [DONE] so strict SSE clients don't hang.
            yield {"data": "[DONE]"}

        return EventSourceResponse(event_generator())
    else:
        result = await state.bridge.send_prompt(last_msg, new_chat=req.new_chat)
        content = result.get("content", "")
        status = result.get("status", ResponseStatus.ERROR)

        if status == ResponseStatus.RATE_LIMITED:
            raise HTTPException(429, "Rate limited. Please wait before retrying.")
        if status == ResponseStatus.AUTH_REQUIRED:
            raise HTTPException(401, "Login session expired. Run once with headed mode to re-authenticate.")
        if status == ResponseStatus.TIMEOUT:
            raise HTTPException(504, "The provider did not respond in time.")
        if status == ResponseStatus.ERROR:
            raise HTTPException(500, f"Provider error: {content}")

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop" if status == ResponseStatus.OK else "error",
            }],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }


@app.post("/v1/chat/completions", dependencies=[Depends(_require_auth)])
async def chat_completions(req: ChatRequest):
    return await _chat_completions_for(_provider_state(DEFAULT_PROVIDER), req)


@app.post("/v1/{provider}/chat/completions", dependencies=[Depends(_require_auth)])
async def provider_chat_completions(provider: str, req: ChatRequest):
    _require_provider(provider)
    return await _chat_completions_for(_provider_state(provider), req)


def main():
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="browser-api Router")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if args.reload:
        # --reload requires the string import form; only meaningful from a
        # source checkout where the package is importable.
        uvicorn.run(
            "browser_api.openai_router:app",
            host=args.host,
            port=args.port,
            reload=True,
            log_level="info",
        )
    else:
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            reload=False,
            log_level="info",
        )


if __name__ == "__main__":
    main()
