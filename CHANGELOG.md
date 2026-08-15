# Changelog

## [0.3.1] - 2026-08-14

### Added
- `_reset_stream_state()` in the provider base: clears per-request CDP stream
  state so a stale `finished`/`error` event from a previous request cannot
  break or mis-report the current stream.

### Fixed
- `_env_int()` now bound-checks parsed values (ports 1-65535, body caps
  1 KiB-1 GiB) and falls back to the default on out-of-range input — a
  negative or zero `BROWSER_API_PORT` / `BROWSER_API_MAX_BODY_BYTES` no
  longer passes through raw.
- README version example corrected to 0.3.0.

### Notes
- ddd + secharden passed (Gemini adversarial review: stream-state race
  mitigated by the existing requestId guard; env bounds hardened).

## [0.3.0] - 2026-08-13

### Added
- **Multi-provider architecture**: provider registry (`providers/` package)
  with a base `BrowserProvider` class. Each provider drives one web chat app
  through its own browser profile and is exposed at `/v1/<provider>/...`
  routes (`/v1/gemini/chat/completions`, `/v1/gemini/models`, ...).
- `GET /v1/providers` — list registered providers and their model ids.
- `BROWSER_API_PROVIDER` env var selects the default provider used by the
  un-namespaced `/v1/chat/completions` and `/v1/models` routes.
- Per-provider model-id env overrides (`BROWSER_API_<PROVIDER>_MODEL` /
  `BROWSER_API_<PROVIDER>_MODEL_ALIASES`).
- Per-provider profile directories under the profiles base dir, so provider
  sessions never share cookies.

### Changed
- Gemini automation moved from `browser_bridge.py` into
  `providers/gemini.py` (`GeminiProvider`). The un-namespaced routes remain
  fully backward compatible (same shapes, same behavior for the default
  provider).

## [0.2.0] - 2026-08-13

First public release.

### Added
- Optional bearer-token auth gate (`BROWSER_API_API_KEY`, constant-time
  compare) on all routes including `/health` and `/help`; loopback-only bind by
  default (`BROWSER_API_HOST`, default `127.0.0.1`).
- XDG-style path configuration with per-directory env overrides
  (`BROWSER_API_BASE_DIR` / `PROFILE_DIR` / `LOGS_DIR` / `CACHE_DIR` /
  `CONFIG_DIR`), defaulting under `~/.local/share/browser-api/`.
- Requirements split: `requirements.txt` (core runtime), `requirements-dev.txt`
  (ruff + pytest).
- Request body-size limit (`BROWSER_API_MAX_BODY_BYTES`, 8 MiB default).
- Prompt content no longer logged; log file + dir permission tightened
  (0600/0700).

### Removed
- Host-control tools — filesystem read/write, git ops, system info, opencode
  launchers (15 registry entries) — archived out of the public package.
- Local-model tool suite (11 tools: chat/embed/rerank/vision/whisper/tts/rag)
  and its `/v1/tools*` endpoints — this is a pure proxy over
  the target web app; the local-server integration was private-infrastructure
  scope creep and does not ship. `requirements-local.txt` and the
  `[local-tools]` extra are gone.
- `/v1/self-improve` endpoint (git stash / opencode integration) removed
  entirely; dead config helpers deleted.
- File upload (`files` request field) removed — the web app only accepts
  attachments via real user interaction, which browser automation cannot
  emulate; use the provider's official API for file uploads.

### Changed
- Profile, logs, cache, and config use XDG-style defaults; `config.py` is
  import-safe (no filesystem access at import time) and enforces `0o700` on
  the profile dir.
- Server no longer binds `0.0.0.0` by default.
- Model ids (`/v1/models`, request default) are now env-configurable
  (`BROWSER_API_MODEL` / `BROWSER_API_MODEL_ALIASES`) and documented as
  informational.

### Known limitations
- Uses whichever model is active in the signed-in browser profile; the `model`
  field in requests is informational.
- One server instance per profile directory (persistent-context lock).
- Per-request latency 5–30 s; selectors may break when the web app's UI changes.
- No authentication by default — loopback bind is the protection; set
  `BROWSER_API_API_KEY` before exposing the server anywhere.
- File upload is not supported: the web app only accepts attachments via
  real user interaction, which browser automation cannot emulate; use the
  provider's official API for file uploads.
