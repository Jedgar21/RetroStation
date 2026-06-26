"""
app/services/auth_flows.py — browser-based authentication for Plex & Jellyfin.

PLEX (hosted PIN/OAuth flow — the "Sign in with Plex" button):
  1. POST https://plex.tv/api/v2/pins   -> {id, code}
  2. Open https://app.plex.tv/auth#?clientID=..&code=..&context...   in browser
  3. User logs in & approves on plex.tv
  4. Poll GET https://plex.tv/api/v2/pins/{id} until authToken is non-null
  5. Use the token to GET https://plex.tv/api/v2/resources to discover servers
  No redirect URL needed -> works for self-hosted apps on a LAN.

JELLYFIN (Quick Connect — per-server, no hosted login):
  Requires the server URL up front (no central directory exists).
  1. GET  {server}/QuickConnect/Initiate           -> {Code, Secret}
  2. User opens THEIR Jellyfin web UI, enters the Code to authorize
  3. Poll GET {server}/QuickConnect/Connect?Secret=..  until Authenticated=true
  4. POST {server}/Users/AuthenticateWithQuickConnect {Secret}
       -> {AccessToken, User:{Id}}
  Quick Connect must be enabled on the Jellyfin server.

Each flow exposes start() -> a pending-auth dict the UI shows, and poll() ->
either still-pending, or a finished result with the token (+ server info).
"""

import json
import time
import uuid
import urllib.parse
import urllib.request
from typing import Optional


# A stable client identity FieldStation presents to Plex.
CLIENT_ID = "fieldstation-" + uuid.uuid5(uuid.NAMESPACE_DNS, "fieldstation.local").hex[:16]
PRODUCT = "FieldStation"
PLEX_HEADERS = {
    "X-Plex-Product": PRODUCT,
    "X-Plex-Client-Identifier": CLIENT_ID,
    "Accept": "application/json",
}


def _req(url, method="GET", headers=None, data=None, timeout=20):
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


# --------------------------------------------------------------------------- #
# Plex
# --------------------------------------------------------------------------- #
class PlexAuth:
    PINS = "https://plex.tv/api/v2/pins"
    AUTH_BASE = "https://app.plex.tv/auth#"
    RESOURCES = "https://plex.tv/api/v2/resources"

    @classmethod
    def start(cls) -> dict:
        """Request a PIN and build the browser auth URL the user opens."""
        data = cls._post_pin()
        pin_id, code = data["id"], data["code"]
        params = {
            "clientID": CLIENT_ID,
            "code": code,
            "context[device][product]": PRODUCT,
        }
        auth_url = cls.AUTH_BASE + "?" + urllib.parse.urlencode(params)
        return {"kind": "plex", "pin_id": pin_id, "code": code,
                "auth_url": auth_url}

    @classmethod
    def _post_pin(cls) -> dict:
        return _req(cls.PINS, method="POST", headers=PLEX_HEADERS,
                    data={"strong": "true"})

    @classmethod
    def poll(cls, pin_id) -> dict:
        """Check whether the user approved. Returns token + servers when done."""
        data = _req(f"{cls.PINS}/{pin_id}", headers=PLEX_HEADERS)
        token = data.get("authToken")
        if not token:
            return {"status": "pending"}
        servers = cls.discover_servers(token)
        return {"status": "authorized", "token": token, "servers": servers}

    @classmethod
    def discover_servers(cls, token: str) -> list:
        """List the user's Plex Media Servers + a connectable base URL each."""
        headers = dict(PLEX_HEADERS)
        headers["X-Plex-Token"] = token
        try:
            res = _req(cls.RESOURCES + "?includeHttps=1", headers=headers)
        except Exception:
            return []
        out = []
        for r in res:
            if r.get("provides") and "server" not in r["provides"]:
                continue
            # prefer a local/https connection
            conns = r.get("connections", [])
            best = None
            for c in conns:
                if c.get("local") and c.get("uri"):
                    best = c["uri"]; break
            if not best and conns:
                best = conns[0].get("uri")
            if best:
                out.append({"name": r.get("name"), "base_url": best,
                            "owned": r.get("owned", True)})
        return out


# --------------------------------------------------------------------------- #
# Jellyfin Quick Connect
# --------------------------------------------------------------------------- #
class JellyfinAuth:
    @staticmethod
    def _hdr():
        # Jellyfin wants an Authorization header identifying the client.
        return {
            "Authorization": (
                f'MediaBrowser Client="{PRODUCT}", Device="{PRODUCT}", '
                f'DeviceId="{CLIENT_ID}", Version="1.0"'),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @classmethod
    def start(cls, base_url: str) -> dict:
        base = base_url.rstrip("/")
        data = _req(f"{base}/QuickConnect/Initiate", headers=cls._hdr())
        return {"kind": "jellyfin", "base_url": base,
                "code": data["Code"], "secret": data["Secret"]}

    @classmethod
    def poll(cls, base_url: str, secret: str) -> dict:
        base = base_url.rstrip("/")
        state = _req(f"{base}/QuickConnect/Connect?secret={secret}",
                     headers=cls._hdr())
        if not state.get("Authenticated"):
            return {"status": "pending"}
        # exchange the approved secret for an access token
        auth = _req_json(f"{base}/Users/AuthenticateWithQuickConnect",
                         method="POST", headers=cls._hdr(),
                         json_body={"Secret": secret})
        token = auth.get("AccessToken")
        user_id = (auth.get("User") or {}).get("Id")
        return {"status": "authorized", "token": token, "user_id": user_id,
                "base_url": base}


def _req_json(url, method="GET", headers=None, json_body=None, timeout=20):
    body = json.dumps(json_body).encode() if json_body is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}
