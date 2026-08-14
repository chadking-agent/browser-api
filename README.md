# browser-api

> ## ⚠️ NOTICE — Read this first
>
> **This project is an educational / experimental demonstration** of how browser
> automation (Playwright/CDP) can be exposed as an OpenAI-compatible HTTP API.
> It is **not affiliated with, endorsed by, or authorized by any service
> provider** in any way, and it is **not an official API** for any product.
>
> **Not for real-world production use.** It drives a real signed-in browser
> session against a third-party web service, which **may violate that
> service's Terms of Service** and can result in **suspension of the account**
> used with it. It is provided **as-is, with no warranty and no liability** —
> the authors accept no responsibility for account bans, data loss, ToS
> enforcement, or any other consequence of using or distributing this software.
>
> **If you are evaluating whether to use automation against a service you rely
> on, use that service's official APIs and developer terms instead.**
>
> **For educational purposes only. You are responsible for your own use.**

An **OpenAI-compatible HTTP proxy** that automates a web chat interface through a real, signed-in browser profile via Playwright. Talk to the web UI from any OpenAI client — curl, the OpenAI SDK, LangChain, etc. — without an official API key.

> **v0.3.0** — multi-provider release. Read the warning below before use.

## ⚠️ WARNING — Read this before you run anything

**browser-api automates a real login session on a third-party web service.** It launches a real browser using a persistent profile, signs into the service with *your* account, and types your prompts into the web UI. This is not an official API, and the service does not provide or endorse this interface. (The default target service is identified in [Configuration](#configuration).)

**By using this project you accept these risks:**

- **Terms-of-service exposure.** Automating the service (via Playwright/CDP, or at all) may violate its Terms of Service. Use at your own risk.
- **Account risk.** Abusive, high-volume, or pattern-recognizable automation can lead to rate-limiting, CAPTCHA walls, **temporary or permanent suspension of your account**, and loss of access to other services tied to that account. The proxy does not rate-limit on your behalf.
- **IP exposure.** All traffic goes from **your machine's IP** (or whatever network the server runs on) to the service, in a browser fingerprint that automation flags can detect. If you run the server on a shared or cloud host, requests originate from that host's IP.
- **Credential gravity.** The browser profile directory contains **live session cookies** for your account. Anyone who can read that directory — or the machine it lives on — can act as you. Never commit profiles, never share them, never run the proxy on a machine you don't control.

**Sensible guardrails:**

1. Use a **dedicated, expendable account** — not your primary account.
2. Use a **fresh, isolated browser profile** (the default profile dir is fine; never point it at your daily-driver browser profile).
3. Keep volume modest; treat aggressive concurrency as account endangerment.
4. **Keep the server on loopback** — `127.0.0.1` is the default bind (`BROWSER_API_HOST`). The server has no authentication unless you set `BROWSER_API_API_KEY`; anyone who can reach the port can use your logged-in session.
5. **Never share** the profile directory, cookies, or screenshots of the session.
6. Read and understand the service's Terms of Service before running this software.

**No warranty.** This project is provided as-is (MIT). The maintainers are not responsible for account suspension, data loss, or other consequences of using it.

---

## What it is / how it works

browser-api is a **browser-automation proxy**, not an API client. It launches a browser with a persistent profile (`launch_persistent_context`), opens the target web app, and automates the chat UI: it types your prompt into the composer, clicks send, then captures the response. Capture is two-tier: (1) primary — a CDP session intercepts the web app's own streaming endpoint and reads the body via `Network.getResponseBody`; (2) fallback — a DOM scraper polls the last response node until the stop button detaches. Streaming mode polls the DOM and emits `delta` chunks.

Because it drives the real web app, you get the model and features of your logged-in account — and you take on the risks described in the warning above.

Key implications, stated plainly:

- Per-request latency is **5–30 s** (a real browser round-trip per prompt).
- The model used is **whatever is active in the signed-in profile**; `model` in the request body is informational.
- One server instance per profile directory (persistent-context lock); no multi-user.

## Requirements

- Python **3.10+** (tested on 3.11–3.13).
- **Google Chrome installed**, or `BROWSER_API_CHROME_EXECUTABLE` pointing at a Chrome/Chromium binary. The bridge launches Chrome via Playwright's `channel="chrome"` — `playwright install chromium` on its own is **not** sufficient (see Installation).
- ~1 GB free disk for the browser profile.

## Installation

```bash
# 1. Python 3.10+
python3 --version

# 2. Create and activate a venv
python3 -m venv .venv
source .venv/bin/activate

# 3. Install
pip install browser-api          # from PyPI
# or, from a source checkout:
# pip install -e .               # installs the package into the active venv
# (a bare `pip install -r requirements.txt` installs dependencies only —
#  the package itself must be installed with `pip install -e .`)

# 4. Chrome — read this first!
#    The bridge launches your installed Google Chrome (channel="chrome"),
#    so a real Chrome install is required. No extra step if you already
#    have Chrome. To use a specific binary:
#    export BROWSER_API_CHROME_EXECUTABLE="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
#    Only if you prefer Playwright's bundled Chromium instead of Chrome:
#    playwright install chromium   # then configure the channel/executable to match
```

Verify: `browser-api-server --help` exits 0, and:

```bash
python -c "import browser_api; print(browser_api.__version__)"   # 0.2.0
```

## First-run login

1. `export BROWSER_API_HEADLESS=false` — the default is headless; login is far more likely to succeed / avoid CAPTCHA in a visible window.
2. `browser-api-server` — logs `Launching Chrome (headless=false)`, then waits for the login redirect.
3. A real Chrome window opens at the service's login page. Sign in (2FA as normal). The server blocks up to **120 s** waiting for you to land back on the target web app.
4. On success the log shows `Input area found` / `Bridge ready`; `curl localhost:54706/health` returns `"page_loaded": true`.
5. **The session is persisted** in the profile dir — subsequent runs can be headless (`BROWSER_API_HEADLESS=true`) and you won't need to re-login until the service expires the session.
6. If the service re-challenges login later, re-run once with `BROWSER_API_HEADLESS=false`.

## Usage

Start the server:

```bash
browser-api-server                 # defaults: 127.0.0.1:54706, headless
```

**curl — health and models:**

```bash
curl -s http://localhost:54706/health
curl -s http://localhost:54706/v1/models
```

**The built-in API guide:**

```bash
curl -s http://localhost:54706/help    # full usage guide: endpoints, new_chat
```

`GET /help` (alias `/v1/help`) returns a complete reference — every endpoint, the request schema, the `new_chat` flag semantics, streaming format, and copy-paste examples for curl, the OpenAI SDK, multi-turn conversations, and auth. Always available; start here.

**curl — chat (non-streaming):**

```bash
curl -s http://localhost:54706/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gemini-3.6", "messages": [{"role": "user", "content": "Explain quantum computing in one sentence."}]}'
```

**curl — streaming (SSE):**

```bash
curl -N -s http://localhost:54706/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "gemini-3.6", "stream": true, "messages": [{"role": "user", "content": "Write a haiku about bridges."}]}'
```

**OpenAI SDK:**

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:54706/v1",
    api_key="not-needed",   # unless BROWSER_API_API_KEY is set
)

resp = client.chat.completions.create(
    model="gemini-3.6",     # informational; the logged-in profile's active model is used
    messages=[{"role": "user", "content": "Hello from the OpenAI SDK!"}],
)
print(resp.choices[0].message.content)
```

**curl — multi-turn (new_chat flag):**

```bash
# turn 1 — start a new chat
curl -s http://localhost:54706/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"new_chat": true, "messages": [{"role": "user", "content": "My name is Alex"}]}'
# turn 2 — continue the same thread
curl -s http://localhost:54706/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"new_chat": false, "messages": [{"role": "user", "content": "What is my name?"}]}'
```

**curl — with API key (if `BROWSER_API_API_KEY` is set):**

```bash
curl -s http://localhost:54706/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <key>' \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}'
```

**`new_chat` semantics.** `new_chat: true` (default) opens a fresh chat before sending — use it to start a conversation. Send `new_chat: false` on follow-ups to continue the browser's current thread. The browser's actual history is what the web app sees, not your `messages` array: if you pass a full history with `new_chat: false`, the browser's prior turns (not your array) are the context.

**CLI mode (single-shot, from a source checkout):**

```bash
python -m browser_api.providers --prompt "hello" --stream
# or specify a provider:
python -m browser_api.providers --provider gemini --prompt "hello" --stream
```

## Configuration

All configuration is via environment variables; there is no config file. Defaults are XDG-style under `~/.local/share/browser-api/`.

**Providers.** The proxy is provider-based: each provider drives one web chat app through its own browser profile. The default provider is `gemini` (Google's chat web app at `gemini.google.com` — see `target_url` in `providers/gemini.py`). Each registered provider is exposed at `/v1/<provider>/...` routes; the un-namespaced `/v1/chat/completions` and `/v1/models` routes target the default provider. Future providers (kimi, deepseek, etc.) are added by dropping a provider module into `providers/` — no router changes needed.

| Variable | Default | Effect |
|---|---|---|
| `BROWSER_API_PROVIDER` | `gemini` | Default provider for `/v1/chat/completions` and `/v1/models` |
| `BROWSER_API_PROFILE_DIR` | `~/.local/share/browser-api/profiles` | Base profiles dir; each provider gets its own subdir (holds **live session cookies**) |
| `BROWSER_API_LOGS_DIR` | `~/.local/share/browser-api/logs` | `api_server.log` |
| `BROWSER_API_CACHE_DIR` | `~/.local/share/browser-api/cache` | Playwright cache and misc |
| `BROWSER_API_CONFIG_DIR` | `~/.local/share/browser-api/config` | State dir |
| `BROWSER_API_BASE_DIR` | `~/.local/share/browser-api` | Base for the defaults above |
| `BROWSER_API_HOST` | `127.0.0.1` | Bind address — keep loopback; no auth unless API key is set |
| `BROWSER_API_PORT` | `54706` | Server port |
| `BROWSER_API_API_KEY` | *(unset)* | If set, every route requires `Authorization: Bearer <key>` (constant-time compare) |
| `BROWSER_API_HEADLESS` | `true` | Headless mode; set `false` for first-run login |
| `BROWSER_API_CHROME_EXECUTABLE` | *(unset → channel `"chrome"`)* | Path to a specific Chrome/Chromium binary |
| `BROWSER_API_GEMINI_MODEL` | `gemini-3.6` | Default provider's echo model id (informational) |
| `BROWSER_API_GEMINI_MODEL_ALIASES` | `gemini-3.6,gemini-3.5-flash-lite` | Comma-separated list served by `/v1/models` |
| `BROWSER_API_MAX_BODY_BYTES` | `8388608` | Max request body size (DoS guard) |

Per-provider model ids use `BROWSER_API_<PROVIDER>_MODEL` / `BROWSER_API_<PROVIDER>_MODEL_ALIASES` (e.g. `BROWSER_API_KIMI_MODEL` when a kimi provider exists). The 0.2.0 names `BROWSER_API_MODEL` / `BROWSER_API_MODEL_ALIASES` still work for the gemini provider (backward compatible).

`browser-api-server` also accepts `--host`, `--port`, and `--reload` flags.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Server starts but `/health` says `page_loaded: false` | Not logged in; run once with `BROWSER_API_HEADLESS=false` and complete login (see First-run login) |
| `Executable doesn't exist` / channel error | No Chrome installed and no `BROWSER_API_CHROME_EXECUTABLE`; install Chrome or set the var |
| Login loop / CAPTCHA | Headless automation is fingerprintable; use headed first run; the service may still challenge — a provider-side risk (see warning) |
| `Could not find input element on the page` | The web app UI changed or the page didn't finish loading; check `/health`; known selector-fragility limitation |
| Response never arrives (~120 s timeout) | CDP stream missed and DOM fallback failing; inspect `api_server.log` in the logs dir |
| HTTP 429/503 in the response | Provider-side rate limit; wait and retry |
| `Browser context already closed` / profile lock | Another instance is using the same profile dir; one server per profile |
| Port 54706 in use | `--port` flag or `BROWSER_API_PORT` |
| 401 on `/v1/*` | `BROWSER_API_API_KEY` is set; send the matching `Authorization: Bearer <key>` header |
| `Bridge ready` but curl refused | Server bound elsewhere; check `BROWSER_API_HOST` / `--host` |

## Providers

browser-api is provider-based. Each provider drives one web chat app through
its own browser profile (so sessions never share cookies), and is exposed at
`/v1/<provider>/...` routes:

- `POST /v1/gemini/chat/completions` — chat against the gemini provider
- `GET /v1/gemini/models` — that provider's model ids
- `GET /v1/providers` — list registered providers
- `POST /v1/chat/completions` — the default provider (`BROWSER_API_PROVIDER`)

**Adding a provider** (ships in a future release, or locally): drop a module
in `src/browser_api/providers/` that subclasses `BrowserProvider` and set the
site-specific bits — target URL, login detection, DOM selectors, response
parsing, cleanup. Register it in `providers/__init__.py`; the router exposes
it automatically. No router changes required.

## Limitations & known issues

- Uses whichever model is active in the signed-in browser profile; the `model` field in requests is informational.
- `temperature`, `max_tokens`, and `stream`-related generation controls are **accepted but not honored** — the browser drives the web app's own generation settings; these fields exist for OpenAI-client compatibility only.
- One server instance per profile directory (persistent-context lock).
- Per-request latency 5–30 s; selectors may break when the web app's UI changes.
- No authentication by default — loopback bind is the protection; set `BROWSER_API_API_KEY` before exposing the server anywhere.
- Idle auto-refresh (180 s) / auto-restart (900 s) intervals are hardcoded (not yet env-configurable).
- File attachment/upload is **not supported**: the web app only accepts files via real user interaction, which browser automation cannot emulate. Use the provider's official API if you need file uploads.
- Automating the target web app may violate its provider's ToS — see the warning at the top.

## License

[MIT](LICENSE) — Copyright (c) 2026 browser-api contributors.
