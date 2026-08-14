"""Provider registry.

Add a new provider by dropping a module in this package that subclasses
browser_api.providers.base.BrowserProvider and registering it below. The
router exposes it automatically at /v1/<name>/chat/completions (and the
default route /v1/chat/completions when BROWSER_API_PROVIDER=<name>).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from browser_api.providers.base import BrowserProvider, ResponseStatus

__all__ = ["BrowserProvider", "ResponseStatus", "PROVIDERS", "register", "get_provider_class", "default_provider_name", "provider_model_ids", "cli"]

# Registered providers: name -> provider class.
PROVIDERS: dict[str, type[BrowserProvider]] = {}


def register(cls: type[BrowserProvider]) -> type[BrowserProvider]:
    PROVIDERS[cls.name] = cls
    return cls


def get_provider_class(name: str) -> type[BrowserProvider]:
    try:
        return PROVIDERS[name]
    except KeyError:
        raise KeyError(f"Unknown provider: {name!r} (available: {', '.join(sorted(PROVIDERS))})")


def default_provider_name() -> str:
    return os.environ.get("BROWSER_API_PROVIDER", "gemini")


# Import providers last so register() is defined before they run.
from browser_api.providers.gemini import GeminiProvider  # noqa: E402

register(GeminiProvider)


def provider_model_ids(name: str) -> tuple[str, list[str]]:
    """(default_model, aliases) for a provider, env-overridable per provider.

    Backward compat: BROWSER_API_MODEL / BROWSER_API_MODEL_ALIASES (0.2.0
    names) still apply to the gemini provider when the per-provider names
    are not set.
    """
    env_key = f"BROWSER_API_{name.upper()}_MODEL"
    env_aliases_key = f"BROWSER_API_{name.upper()}_MODEL_ALIASES"
    legacy_default = os.environ.get("BROWSER_API_MODEL", "") if name == "gemini" else ""
    legacy_aliases = os.environ.get("BROWSER_API_MODEL_ALIASES", "") if name == "gemini" else ""
    default_model = os.environ.get(env_key, "") or legacy_default or (
        "gemini-3.6" if name == "gemini" else "model"
    )
    aliases = [m.strip() for m in (
        os.environ.get(env_aliases_key, "") or legacy_aliases or (
            "gemini-3.6,gemini-3.5-flash-lite" if name == "gemini" else default_model
        )
    ).split(",") if m.strip()] or [default_model]
    # When only the default-model override is set (not the aliases list), seed
    # the served model list with it so the override is visible via /v1/models.
    if not (os.environ.get(env_aliases_key) or legacy_aliases) and default_model not in aliases:
        aliases.insert(0, default_model)
    # Deduplicate while preserving order (env may contain dupes).
    aliases = list(dict.fromkeys(aliases))
    return default_model, aliases


async def _main():
    import argparse

    from browser_api.config import get_profile_dir

    parser = argparse.ArgumentParser(description="browser-api bridge")
    parser.add_argument("--prompt", help="Send a prompt to the web app")
    parser.add_argument("--provider", help="Provider name (default: BROWSER_API_PROVIDER or gemini)")
    parser.add_argument("--profile", help="Chrome profile directory")
    parser.add_argument("--headed", action="store_true", help="Run with a visible browser window (default: headless)")
    parser.add_argument("--stream", action="store_true", help="Stream response")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    name = args.provider or default_provider_name()
    provider_cls = get_provider_class(name)
    profile = Path(args.profile) if args.profile else get_profile_dir(name)

    bridge = provider_cls(profile_dir=profile, headless=not args.headed)
    await bridge.start()

    try:
        if args.prompt:
            if args.stream:
                print("Streaming response:\n")
                async for chunk in bridge.send_prompt_streaming(args.prompt):
                    print(chunk.get("delta", ""), end="", flush=True)
                print()
            else:
                print("Sending prompt...\n")
                result = await bridge.send_prompt(args.prompt)
                print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            health = await bridge.health_check()
            print(json.dumps(health, indent=2))
    finally:
        await bridge.close()


def cli():
    """Sync console-script entry point (setuptools cannot call async main)."""
    asyncio.run(_main())


if __name__ == "__main__":
    cli()
