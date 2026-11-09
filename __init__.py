"""Gemini Antigravity: Hermes on a Google Antigravity subscription, straight to Google with OAuth 2.0.

No agy CLI and no third-party package: sign-in, token refresh, model catalog, quotas and inference go to
Google's own endpoints (antigravity.py, standard library only). Accounts live in Hermes' credential pool.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

from . import antigravity as ag

NAME = "gemini-antigravity"
# Offered when the live catalog is unreachable; the picker otherwise shows what Google offers the account
FALLBACK_MODELS = ("gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low", "claude-sonnet-4-6",
                   "claude-opus-4-6-thinking", "gpt-oss-120b-medium")
_GRANT_DEAD = ("invalid_grant", "invalid_token", "unauthorized_client")


def _token_request(data: dict[str, str]) -> dict[str, Any]:
    """Token endpoint for Hermes' pooled refresh; a revoked grant asks for a new sign-in instead of retrying."""
    from hermes_cli.auth_constants import AuthError
    try:
        return ag.token_request(data)
    except ag.AntigravityError as exc:
        if exc.status in (400, 401) and any(code in str(exc) for code in _GRANT_DEAD):
            raise AuthError(f"{NAME}: Google revoked this sign-in. Run `hermes auth add {NAME}`.", provider=NAME,
                            code="invalid_grant", relogin_required=True) from exc
        raise


def _refresh(entry: Any) -> Any:
    from hermes_cli.auth_oauth_pkce_plugin import OAuthPKCEConfig, pkce_refresh_credential
    # Hermes' rotation (auth lock, adoption of a peer's refresh) with Google's token endpoint behind it
    cfg = OAuthPKCEConfig(client_id="", authorize_url="", token_url="", label="Antigravity", token_request=_token_request)
    return pkce_refresh_credential(cfg)(entry)


def _auth(action: str, args: Any) -> bool:
    from agent.credential_pool import AUTH_TYPE_OAUTH, PooledCredential, load_pool
    if action == "add":
        token = ag.login(open_browser=not getattr(args, "no_browser", False))
        expires_in = int(token.get("expires_in") or 3600)
        email = token.get("email") or "Google account"
        pool = load_pool(NAME)
        # signing in again with the same account replaces its row instead of adding a twin
        for index in range(len(pool.entries()), 0, -1):
            if pool.entries()[index - 1].label == email:
                pool.remove_index(index)
        entry = pool.add_entry(PooledCredential(
            provider=NAME, id=uuid.uuid4().hex[:6], label=email, auth_type=AUTH_TYPE_OAUTH, priority=0,
            source="manual:antigravity_oauth", access_token=token["access_token"], refresh_token=token["refresh_token"],
            expires_at_ms=int(time.time() * 1000) + expires_in * 1000))
        print(f"Signed in to Antigravity as {email} (credential {entry.id}).")
        print(f"Pick a model with `hermes model` (provider: {NAME}).")
        return True
    if action == "status":
        entries = load_pool(NAME).entries()
        print(f"{NAME}: " + (", ".join(e.label for e in entries) if entries else "logged out"))
        return True
    if action == "logout":
        pool = load_pool(NAME)
        count = len(pool.entries())
        for index in range(count, 0, -1):
            pool.remove_index(index)
        print(f"Signed out of Antigravity ({count} account(s) removed).")
        return True
    return False  # refresh: Hermes' pool calls _refresh


class AntigravityProfile(ProviderProfile):
    def create_client(self, **client_kwargs: Any) -> Any:
        from .client import AntigravityClient
        allowed = {"api_key", "base_url", "default_headers", "timeout"}
        return AntigravityClient(**{k: v for k, v in client_kwargs.items() if k in allowed})

    def build_extra_body(self, *, session_id: str | None = None, **context: Any) -> dict[str, Any]:
        # same Antigravity session for a whole Hermes session, so Google can reuse its cache
        return {"antigravity_session": session_id} if session_id else {}

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0) -> list[str] | None:
        if not api_key:
            return None
        from .client import _catalog, catalog_models
        try:
            catalog_models(api_key, force=True)
        except ag.AntigravityError:
            return None
        return list(_catalog.get("offered") or []) or None

    def supported_reasoning_efforts(self, model: str | None = None) -> tuple[str, ...] | None:
        return ()  # Google publishes one id per level (…-low, …-medium, …-high): the level is in the model

    def get_max_tokens(self, model: str | None) -> int | None:
        return None  # the client caps at the catalog's limit; a Hermes default would cut long answers

    def get_model_context_length(self, model: str | None = None, **_: Any) -> int | None:
        from .client import _catalog
        return (_catalog["models"].get(model or "") or {}).get("maxTokens") or None

    def fetch_account_usage(self, *, api_key: str | None = None, base_url: str | None = None) -> Any:
        if not api_key:
            # `hermes usage` passes no key: read with a live pooled token (never refreshes from here)
            from agent.credential_pool import load_pool
            now_ms = time.time() * 1000
            api_key = next((e.access_token for e in load_pool(NAME).entries() if (e.expires_at_ms or 0) > now_ms), None)
        if not api_key:
            return None
        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
        windows = []
        for pool in ag.quota_pools(ag.fetch_quota_summary(api_key)):
            for w in pool["windows"]:
                reset = None
                if w.get("resetTime"):
                    try:
                        reset = datetime.fromisoformat(str(w["resetTime"]).replace("Z", "+00:00"))
                    except ValueError:
                        pass
                label = {"5h": "5-hour", "weekly": "Weekly"}.get(w["window"], w["window"] or "")
                windows.append(AccountUsageWindow(label=f"{pool['name']} · {label}", used_percent=round((1 - w["remainingFraction"]) * 100, 1),
                                                  reset_at=reset, scope="model"))
        return AccountUsageSnapshot(provider=NAME, source="antigravity_quota", fetched_at=datetime.now(timezone.utc),
                                    title="Antigravity quota", windows=tuple(windows))


register_provider(AntigravityProfile(
    name=NAME, aliases=("antigravity", "antigravity-direct"), display_name="Gemini Antigravity",
    description="Google Antigravity subscription (Gemini, Claude, GPT-OSS), direct OAuth sign-in",
    signup_url="https://antigravity.google", auth_type="oauth_external", base_url="https://cloudcode-pa.googleapis.com/v1internal",
    supports_health_check=False, supports_vision=True, fallback_models=FALLBACK_MODELS,
    default_aux_model="gemini-3.8-flash-low", auth_handler=_auth, refresh_credential=_refresh,
    hostnames=("cloudcode-pa.googleapis.com", "daily-cloudcode-pa.googleapis.com"),
))
