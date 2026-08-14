"""Path configuration for browser-api.

All runtime state defaults to XDG-style locations under
~/.local/share/browser-api/ and can be overridden per-directory via
environment variables:

    BROWSER_API_PROFILE_DIR   base dir for browser profiles (one per provider)
    BROWSER_API_LOGS_DIR      log files
    BROWSER_API_CACHE_DIR     playwright cache + misc
    BROWSER_API_CONFIG_DIR    config/state dir

Each provider gets its own profile directory under PROFILES_DIR/<provider>,
so provider sessions never share cookies.

This module is import-safe: it does not touch the filesystem at import time.
"""
from __future__ import annotations

import os
from pathlib import Path

_BASE_DIR = Path(os.environ.get(
    "BROWSER_API_BASE_DIR",
    str(Path.home() / ".local" / "share" / "browser-api"),
)).expanduser()

PROFILES_DIR = Path(os.environ.get(
    "BROWSER_API_PROFILE_DIR",
    str(_BASE_DIR / "profiles"),
)).expanduser()
LOGS_DIR = Path(os.environ.get(
    "BROWSER_API_LOGS_DIR",
    str(_BASE_DIR / "logs"),
)).expanduser()
CACHE_DIR = Path(os.environ.get(
    "BROWSER_API_CACHE_DIR",
    str(_BASE_DIR / "cache"),
)).expanduser()
CONFIG_DIR = Path(os.environ.get(
    "BROWSER_API_CONFIG_DIR",
    str(_BASE_DIR / "config"),
)).expanduser()


def get_profile_dir(provider: str) -> Path:
    """Per-provider profile directory (holds that provider's live session cookies)."""
    return PROFILES_DIR / provider


def get_data_dir(provider: str | None = None) -> Path:
    """Backward-compatible accessor; returns the default provider's profile dir."""
    if provider is None:
        from browser_api.providers import default_provider_name
        provider = default_provider_name()
    return get_profile_dir(provider)


def get_cache_dir() -> Path:
    return CACHE_DIR


def get_log_dir() -> Path:
    return LOGS_DIR


def get_config_dir() -> Path:
    return CONFIG_DIR


def ensure_dirs() -> None:
    for d in (PROFILES_DIR, CACHE_DIR, LOGS_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
