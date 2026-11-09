"""Google Antigravity protocol, standard library only: OAuth 2.0 (PKCE), Cloud Code project, model catalog,
quotas and the v1internal generation endpoints. Nothing here imports Hermes, so it is testable on its own.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import http.server
import ipaddress
import json
import os
import platform
import re
import secrets
import socket
import socketserver
import ssl
import threading
import time
import urllib.parse
import uuid
import webbrowser
from typing import Any, Callable, Iterator, Optional

# Antigravity's installed-app OAuth client: public identifiers of Google's desktop client, as in the agy CLI.
CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
CLIENT_SECRET = "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"
CALLBACK_PORT = 51121  # the return address Google registered for this client
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/oauth-callback"
SCOPES = (
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
)
ENDPOINTS = ("https://daily-cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com")
DEFAULT_PROJECT = "rising-fact-p41fc"  # shared project Antigravity uses when the account has none
MAX_REQUEST_BYTES = 48_000_000  # above this the server refuses inline data

_OS = {"Windows": "windows", "Darwin": "darwin"}.get(platform.system(), platform.system().lower())
_ARCH = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "i386": "386", "i686": "386"}.get(platform.machine(), platform.machine().lower())
USER_AGENT = f"antigravity/cli/1.3.5 (aidev_client; os_type={_OS}; arch={_ARCH}; cl=998863838; auth_method=consumer)"


class AntigravityError(Exception):
    def __init__(self, message: str, status: Optional[int] = None, *, retryable: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status, self.retryable, self.retry_after = status, retryable, retry_after


# ---------- transport ----------

_ANTIGRAVITY_HOST = re.compile(r"(^|[.-])cloudcode-pa\.googleapis\.com$", re.I)
_resolved: dict[str, tuple[float, str]] = {}


def _is_loopback(address: str) -> bool:
    try:
        return ipaddress.ip_address(address.split("%")[0]).is_loopback
    except ValueError:
        return False


def public_address(host: str, resolve: Callable[[str], list[str]] | None = None, ask_dns: Callable[[str], Optional[str]] | None = None) -> Optional[str]:
    """When the hosts file sends an Antigravity host to this computer (a common leftover of other tools), the
    address public DNS gives for it; None when the host resolves normally. Nothing to change on the machine."""
    if not _ANTIGRAVITY_HOST.search(host):
        return None
    try:
        found = (resolve or (lambda h: [a[4][0] for a in socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)]))(host)
    except OSError:
        return None
    if not found or not all(_is_loopback(a) for a in found):
        return None
    hit = _resolved.get(host)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    address = (ask_dns or _dns_over_https)(host)
    if address:
        _resolved[host] = (time.time(), address)
    return address


def _dns_over_https(host: str) -> Optional[str]:
    # Google's public resolver by address, so the lookup itself cannot be redirected by the hosts file
    conn = http.client.HTTPSConnection("8.8.8.8", timeout=10, context=tls_context())
    try:
        conn.request("GET", f"/resolve?name={urllib.parse.quote(host)}&type=A", headers={"Accept": "application/dns-json"})
        answers = json.loads(conn.getresponse().read()).get("Answer") or []
        return next((a["data"] for a in answers if a.get("type") == 1), None)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    finally:
        conn.close()


def _proxy_for(host: str) -> Optional[urllib.parse.SplitResult]:
    raw = next((os.environ[k] for k in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy") if os.environ.get(k)), "")
    if not raw:
        return None
    skip = [s.strip().lower() for s in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",") if s.strip()]
    h = host.lower()
    if any(s == "*" or h == s.lstrip(".") or h.endswith(s if s.startswith(".") else f".{s}") for s in skip):
        return None
    return urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")


def tls_context() -> ssl.SSLContext:
    """The OS certificate store, as Hermes trusts it (truststore: corporate roots work), else system plus certifi."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        pass
    context = ssl.create_default_context()
    try:
        import certifi
        context.load_verify_locations(certifi.where())
    except (ImportError, OSError):
        pass
    return context


class _Connection(http.client.HTTPSConnection):
    """HTTPS to `host`, through the environment's proxy, or to `address` when the hosts file redirects the host."""

    def __init__(self, host: str, address: Optional[str], timeout: float):
        super().__init__(host, 443, timeout=timeout, context=tls_context())
        self._address = address

    def connect(self) -> None:
        if self._tunnel_host or not self._address:
            return super().connect()
        sock = socket.create_connection((self._address, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def connect(url: str, timeout: float) -> tuple[http.client.HTTPSConnection, str]:
    """(connection, path) for an https URL."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    proxy = _proxy_for(host)
    if proxy:
        # CONNECT tunnel; http.client then names the real host in TLS
        conn = http.client.HTTPSConnection(proxy.hostname or "", proxy.port or 8080, timeout=timeout, context=tls_context())
        headers = {}
        if proxy.username:
            raw = f"{urllib.parse.unquote(proxy.username)}:{urllib.parse.unquote(proxy.password or '')}"
            headers["Proxy-Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
        conn.set_tunnel(host, 443, headers)
    else:
        conn = _Connection(host, public_address(host), timeout)
    return conn, parts.path + (f"?{parts.query}" if parts.query else "")


def http_request(url: str, *, method: str = "POST", headers: dict[str, str] | None = None, body: bytes | None = None,
                 timeout: float = 60) -> tuple[http.client.HTTPSConnection, http.client.HTTPResponse]:
    """Send a request; the caller reads the response and closes the connection."""
    conn, path = connect(url, timeout)
    try:
        conn.request(method, path, body=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
        return conn, conn.getresponse()
    except BaseException:
        conn.close()
        raise


def read_body(res: http.client.HTTPResponse) -> str:
    data = res.read()
    if (res.getheader("Content-Encoding") or "").lower() == "gzip":
        import gzip
        data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


def _json_headers(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}


def post_endpoints(path: str, access: str, payload: dict, *, timeout: float = 15, endpoints: tuple[str, ...] = ENDPOINTS) -> dict:
    """POST JSON to each endpoint until one answers; a client error (4xx other than 404/429) stops there."""
    errors: list[AntigravityError] = []
    for endpoint in endpoints:
        try:
            conn, res = http_request(f"{endpoint}/{path}", headers=_json_headers(access), body=json.dumps(payload).encode(), timeout=timeout)
            try:
                text = read_body(res)
            finally:
                conn.close()
        except (OSError, http.client.HTTPException) as exc:
            errors.append(AntigravityError(f"{path} at {endpoint}: {exc}"))
            continue
        if 200 <= res.status < 300:
            return json.loads(text or "{}")
        errors.append(AntigravityError(f"{path} {res.status} at {endpoint}: {text[:300]}", res.status))
        if res.status < 500 and res.status not in (404, 429):
            break
    raise AntigravityError("; ".join(str(e) for e in errors), errors[-1].status if errors else None)


# ---------- OAuth ----------

def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def authorize_url(verifier: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI, "scope": " ".join(SCOPES),
        "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
        "state": state, "access_type": "offline", "prompt": "consent",
    })
    return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"


def token_request(data: dict[str, str]) -> dict[str, Any]:
    """Google's token endpoint with Antigravity's client: the raw token response (access_token, expires_in, …)."""
    fields = {k: v for k, v in data.items() if k in ("grant_type", "code", "refresh_token", "redirect_uri", "code_verifier")}
    if fields.get("grant_type") == "authorization_code":
        fields["redirect_uri"] = REDIRECT_URI  # the address this login really used
    body = urllib.parse.urlencode({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **fields}).encode()
    conn, res = http_request("https://oauth2.googleapis.com/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body, timeout=30)
    try:
        text = read_body(res)
    finally:
        conn.close()
    payload = json.loads(text or "{}") if text.strip().startswith("{") else {}
    if res.status >= 400:
        error = str(payload.get("error") or "")
        raise AntigravityError(f"Google token request failed ({res.status}{', ' + error if error else ''})", res.status)
    return payload


def account_email(access: str) -> Optional[str]:
    try:
        conn, res = http_request("https://www.googleapis.com/oauth2/v1/userinfo?alt=json", method="GET",
                                 headers={"Authorization": f"Bearer {access}"}, timeout=15)
        try:
            return json.loads(read_body(res)).get("email") if res.status == 200 else None
        finally:
            conn.close()
    except (OSError, ValueError, http.client.HTTPException):
        return None


class _CallbackServer(http.server.HTTPServer):
    def server_bind(self) -> None:
        # HTTPServer would resolve this machine's name first (getfqdn), which can hang for many seconds
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "localhost", self.server_address[1]


_PAGE = ("<!doctype html><meta charset=utf-8><title>Antigravity</title><body style='font-family:system-ui;margin:3rem'>"
         "<h2>{}</h2><p>You can close this tab and return to Hermes.</p>")


def code_from_redirect(text: str, state: str) -> str:
    """Authorization code from the address Google redirected to (pasted, or received by the callback server)."""
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(text.strip()).query)
    if query.get("error"):
        raise AntigravityError(f"Google sign-in failed: {query['error'][0]}")
    if (query.get("state") or [""])[0] != state:
        raise AntigravityError("this address does not belong to this sign-in (state mismatch)")
    code = (query.get("code") or [""])[0]
    if not code:
        raise AntigravityError("no authorization code in this address")
    return code


def login(*, open_browser: bool = True, timeout: float = 600, prompt: Callable[[str], str] = input) -> dict[str, Any]:
    """Browser sign-in. Google returns to localhost:51121 on this computer; when the browser is on another device,
    the user pastes the address it ends on. Returns the token response plus `email`."""
    verifier, state = _b64url(secrets.token_bytes(32)), _b64url(secrets.token_bytes(16))
    url = authorize_url(verifier, state)
    done = threading.Event()
    result: dict[str, Any] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if not self.path.startswith("/oauth-callback"):
                self.send_error(404)
                return
            try:
                result["code"] = code_from_redirect(self.path, state)
                title = "Signed in to Antigravity"
            except AntigravityError as exc:
                result["error"] = exc
                title = str(exc)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_PAGE.format(title).encode())
            done.set()

        def log_message(self, *args: Any) -> None:
            pass

    server = None
    try:
        server = _CallbackServer(("127.0.0.1", CALLBACK_PORT), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except OSError:
        print(f"Port {CALLBACK_PORT} is busy: paste the address after signing in.")
    print(f"\nSign in to Google Antigravity:\n  {url}\n")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print("Browser on another device? After signing in, it ends on a localhost:51121 page that does not load:")
    print("copy that page's address and paste it here.\n")

    def ask() -> None:
        try:
            pasted = prompt("Address (or wait for the browser): ")
        except (EOFError, OSError):
            return
        if pasted.strip() and not done.is_set():
            try:
                result["code"] = code_from_redirect(pasted, state)
            except AntigravityError as exc:
                result["error"] = exc
            done.set()

    threading.Thread(target=ask, daemon=True).start()
    try:
        if not done.wait(timeout):
            raise AntigravityError("sign-in timed out")
    finally:
        if server:
            server.shutdown()
            server.server_close()
    if "error" in result:
        raise result["error"]
    token = token_request({"grant_type": "authorization_code", "code": result["code"], "code_verifier": verifier})
    if not token.get("refresh_token"):
        raise AntigravityError("Google returned no refresh token")
    token["email"] = account_email(token["access_token"])
    return token


# ---------- Cloud Code project ----------

_METADATA = {"ideType": "ANTIGRAVITY"}
_projects: dict[str, tuple[float, str]] = {}


def _load_project(access: str) -> tuple[str, str]:
    data = post_endpoints("v1internal:loadCodeAssist", access, {"metadata": _METADATA}, timeout=10)
    project = data.get("cloudaicompanionProject")
    project_id = project.get("id") if isinstance(project, dict) else project
    tiers = data.get("allowedTiers") or []
    tier = next((t.get("id") for t in tiers if t.get("isDefault")), None) or (tiers[0].get("id") if tiers else "free-tier")
    return project_id or "", tier


def _onboard(access: str, tier: str) -> str:
    for _ in range(10):
        try:
            data = post_endpoints("v1internal:onboardUser", access, {"tierId": tier}, endpoints=(ENDPOINTS[1], ENDPOINTS[0]))
        except AntigravityError:
            return ""
        if data.get("done"):
            return ((data.get("response") or {}).get("cloudaicompanionProject") or {}).get("id") or ""
        time.sleep(5)
    return ""


def project_for(access: str) -> str:
    """The Cloud Code project every call carries (cached 30 min per token)."""
    key = hashlib.sha256(access.encode()).hexdigest()
    hit = _projects.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    project = ""
    try:
        project, tier = _load_project(access)
        project = project or _onboard(access, tier)
    except AntigravityError:
        pass  # Cloud Code unreachable: the call itself will report the real error
    project = project or DEFAULT_PROJECT
    _projects[key] = (time.time(), project)
    return project


# ---------- catalog and quotas ----------

def fetch_catalog(access: str) -> dict:
    project = project_for(access)
    try:
        return post_endpoints("v1internal:fetchAvailableModels", access, {"project": project})
    except AntigravityError as exc:
        if exc.status == 403:
            return post_endpoints("v1internal:fetchAvailableModels", access, {})
        raise


def offered_models(catalog: dict) -> list[str]:
    """Model ids in Antigravity's picker order, without those Google marks unavailable for this plan."""
    available = catalog.get("models") or {}
    sorts = catalog.get("agentModelSorts")
    ids = ([i for s in sorts for g in (s.get("groups") or []) for i in (g.get("modelIds") or [])] if sorts
           else [i for i, m in available.items() if m.get("displayName") and not m.get("isInternal")])
    return [i for i in dict.fromkeys(ids) if i in available and not available[i].get("disabled")]


def fetch_quota_summary(access: str) -> dict:
    return post_endpoints("v1internal:retrieveUserQuotaSummary", access, {"project": project_for(access)})


def quota_pools(summary: dict) -> list[dict]:
    """[{name, windows: [{window, remainingFraction, resetTime}]}] with the 5-hour window first."""
    pools = []
    for group in summary.get("groups") or []:
        windows = [{"window": b.get("window"), "remainingFraction": max(0.0, min(1.0, float(b.get("remainingFraction") or 0))),
                    "resetTime": b.get("resetTime")} for b in group.get("buckets") or [] if b.get("disabled") is not True]
        windows.sort(key=lambda w: w["window"] != "5h")
        if windows:
            pools.append({"name": group.get("displayName") or "Models", "windows": windows})
    return pools


# ---------- generation ----------

def thinking_for(model: str) -> Optional[dict]:
    """Thinking setting for a Google id, as Antigravity sends it: Argon and Claude 5.x get their level,
    Gemini Flash Medium and Low a thinking budget; Flash High keeps Antigravity's own setting."""
    match = re.search(r"-(low|medium|high)$", model)
    if not match:
        return None
    level = match.group(1)
    if model.startswith("gemini-4-argon"):
        return {"includeThoughts": True, "thinkingLevel": level.upper()}
    if re.match(r"^claude-.*-5-\d+-", model):
        return {"includeThoughts": True, "thinkingBudget": 0, "thinkingLevel": level.upper()}
    if re.match(r"^gemini-.*flash", model):
        return {"includeThoughts": True, **({"thinkingBudget": 4000} if level == "medium" else {"thinkingBudget": 1000} if level == "low" else {})}
    return None


def _fnv1a64(text: str) -> str:
    h = 0xCBF29CE484222325
    for byte in text.encode():
        h = ((h ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return str(h - (1 << 64) if h >= 1 << 63 else h)


class Sessions:
    """Session ids the way the Antigravity agent sends them: one conversation keeps the same session, so
    Google can reuse its cache. Forgotten after 24 h unused."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def begin(self, key: str) -> tuple[str, str]:
        now = time.time()
        with self._lock:
            for k in [k for k, v in self._entries.items() if now - v["used"] > 86400]:
                del self._entries[k]
            entry = self._entries.setdefault(key, {"conversation": str(uuid.uuid4()), "trajectory": str(uuid.uuid4()),
                                                   "session": _fnv1a64(f"hermes:{key}"), "step": 0, "used": now})
            entry["used"] = now
            entry["step"] += 1
            return entry["session"], f"agent/{entry['conversation']}/{int(now * 1000)}/{entry['trajectory']}/{entry['step']}"


def failure(status: int, text: str) -> AntigravityError:
    """Readable call error: a spent quota says when it resets; an overloaded server is marked retryable."""
    detail, retry_after, reason = text[:600], None, ""
    try:
        error = json.loads(text).get("error") or {}
        detail = error.get("message") or detail
        details = error.get("details") or []
        reason = next((d.get("reason") for d in details if d.get("reason")), None) or error.get("status") or ""
        delay = next((d.get("retryDelay") for d in details if d.get("retryDelay")), None)
        if delay:
            retry_after = float(str(delay).rstrip("s") or 0)
    except (ValueError, AttributeError, TypeError):
        pass
    capacity = bool(re.search(r"capacity|overloaded", f"{reason} {text}", re.I))
    if status == 429 and not capacity:
        wait = f", retry in about {round(retry_after / 60) or 1} min" if retry_after else ""
        return AntigravityError(f"Antigravity quota reached for this model{wait}. Switch model or account. ({detail})", status, retry_after=retry_after)
    return AntigravityError(f"Antigravity {status}: {detail}", status, retryable=status in (503, 529) or (status == 429 and capacity), retry_after=retry_after)


def generate(access: str, model: str, request: dict, *, stream: bool, request_id: str, timeout: float = 1200,
             on_connection: Callable[[http.client.HTTPSConnection], None] | None = None) -> Any:
    """One v1internal call. Non-streaming: the response (unwrapped). Streaming: an iterator of response events."""
    body = json.dumps({"project": project_for(access), "model": model, "request": request, "requestType": "agent",
                       "userAgent": "antigravity", "requestId": request_id}).encode()
    if len(body) > MAX_REQUEST_BYTES:
        raise AntigravityError(f"request is {len(body) / 1e6:.1f} MB, limit {MAX_REQUEST_BYTES / 1e6:.0f} MB")
    path = "v1internal:streamGenerateContent?alt=sse" if stream else "v1internal:generateContent"
    error: Optional[AntigravityError] = None
    for endpoint in ENDPOINTS:
        try:
            conn, res = http_request(f"{endpoint}/{path}", headers={**_json_headers(access), "Accept": "text/event-stream" if stream else "application/json"},
                                     body=body, timeout=timeout)
        except (OSError, http.client.HTTPException) as exc:
            error = AntigravityError(f"Antigravity unreachable at {endpoint}: {exc}", retryable=True)
            continue
        if on_connection:
            on_connection(conn)
        if res.status == 200:
            if stream:
                return _events(conn, res)
            try:
                data = json.loads(read_body(res))
            finally:
                conn.close()
            return data.get("response", data)
        try:
            error = failure(res.status, read_body(res))
        finally:
            conn.close()
        if res.status != 404 and res.status < 500:
            break  # client error: the other endpoint would answer the same
    raise error or AntigravityError("no Antigravity endpoint answered")


def _events(conn: http.client.HTTPSConnection, res: http.client.HTTPResponse) -> Iterator[dict]:
    try:
        for raw in res:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except ValueError:
                continue
            yield event.get("response", event) if isinstance(event, dict) else event
    finally:
        conn.close()
