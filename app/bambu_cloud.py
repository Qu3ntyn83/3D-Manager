"""Minimaler Client für den Bambu Lab Filament Manager (Cloud-Spulenbibliothek).

Inoffizielle API – dieselbe, die Bambu Studio / Handy nutzen:
  GET  https://api.bambulab.com/v1/design-user-service/my/filament/v2?offset=&limit=
Login über /v1/user-service/user/login (Passwort → ggf. E-Mail-Code oder 2FA).
api.bambulab.com sitzt hinter Cloudflare → curl_cffi mit Chrome-Impersonation.

Die App liest nur (kein Schreiben in die Cloud).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from curl_cffi import requests as http
except ImportError:  # pragma: no cover
    http = None

IMPERSONATE = "chrome"
TIMEOUT = 20
PAGE_SIZE = 100
MAX_PAGES = 50


class CloudError(Exception):
    pass


class AuthExpired(CloudError):
    pass


class CodeRequired(CloudError):
    """Bambu hat einen Login-Code per E-Mail/SMS geschickt."""


class TfaRequired(CloudError):
    def __init__(self, tfa_key: str):
        super().__init__("2FA-Code erforderlich")
        self.tfa_key = tfa_key


class BambuCloud:
    def __init__(self, region: str = "global", token: str | None = None):
        self.region = region
        self.token = token
        self.mock = os.environ.get("BAMBU_MOCK")

    @property
    def api(self) -> str:
        return "https://api.bambulab.cn" if self.region == "china" else "https://api.bambulab.com"

    @property
    def web(self) -> str:
        return "https://bambulab.cn" if self.region == "china" else "https://bambulab.com"

    def _req(self, method: str, url: str, body: dict | None = None, auth: bool = True, headers: dict | None = None):
        if http is None:
            raise CloudError("curl_cffi ist nicht installiert")
        h = dict(headers or {})
        if auth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        try:
            r = getattr(http, method)(url, headers=h, json=body, timeout=TIMEOUT, impersonate=IMPERSONATE)
        except Exception as e:  # noqa: BLE001
            raise CloudError(f"Verbindung zur Bambu Cloud fehlgeschlagen: {e}") from e
        if r.status_code in (403, 429) and "cloudflare" in r.text.lower():
            raise CloudError(f"Von Cloudflare blockiert (HTTP {r.status_code}) – später erneut versuchen")
        if r.status_code == 401 and auth and self.token:
            raise AuthExpired("Bambu-Token abgelaufen oder ungültig – bitte neu anmelden")
        return r

    @staticmethod
    def _json(r) -> dict[str, Any]:
        try:
            return r.json()
        except Exception as e:  # noqa: BLE001
            raise CloudError(f"Ungültige Antwort der Bambu Cloud (HTTP {r.status_code})") from e

    @staticmethod
    def _bambu_error(r) -> str:
        """Fehlertext aus der Bambu-Antwort holen ({"code":1,"error":"..."})."""
        try:
            d = r.json()
            msg = d.get("error") or d.get("message") or ""
            return f"{msg} (Code {d.get('code')})" if msg else ""
        except Exception:  # noqa: BLE001
            return r.text[:160]

    # ---------- Login ----------
    def login(self, account: str, password: str) -> str:
        r = self._req("post", f"{self.api}/v1/user-service/user/login",
                      {"account": account, "password": password, "apiError": ""}, auth=False)
        if r.status_code != 200:
            detail = self._bambu_error(r)
            print(f"[bambu] Login HTTP {r.status_code}: {detail}", flush=True)
            if "incorrect account or password" in detail.lower():
                raise CloudError("E-Mail oder Passwort falsch. Hinweis: Wenn du dich bei Bambu mit Google/Apple anmeldest, "
                                 "hat dein Konto kein Passwort – dann „Code per Mail“ verwenden.")
            raise CloudError(f"Login abgelehnt (HTTP {r.status_code}): {detail or 'keine Details'}")
        d = self._json(r)
        if d.get("accessToken"):
            self.token = d["accessToken"]
            return self.token
        if d.get("loginType") == "verifyCode":
            raise CodeRequired("Bambu hat einen Login-Code an deine E-Mail geschickt")
        if d.get("loginType") == "tfa":
            raise TfaRequired(d.get("tfaKey", ""))
        raise CloudError(f"Unerwartete Login-Antwort: {d.get('loginType')}")

    def request_code(self, account: str) -> None:
        if "@" in account:
            url, body = f"{self.api}/v1/user-service/user/sendemail/code", {"email": account, "type": "codeLogin"}
        else:
            url, body = f"{self.api}/v1/user-service/user/sendsmscode", {"phone": account, "type": "codeLogin"}
        r = self._req("post", url, body, auth=False)
        if r.status_code != 200:
            detail = self._bambu_error(r)
            print(f"[bambu] Code anfordern HTTP {r.status_code}: {detail}", flush=True)
            raise CloudError(f"Code konnte nicht angefordert werden (HTTP {r.status_code}): {detail or 'keine Details'}")

    def login_code(self, account: str, code: str) -> str:
        r = self._req("post", f"{self.api}/v1/user-service/user/login", {"account": account, "code": code}, auth=False)
        if r.status_code == 200 and (tok := self._json(r).get("accessToken")):
            self.token = tok
            return tok
        detail = self._bambu_error(r)
        print(f"[bambu] Code-Login HTTP {r.status_code}: {detail}", flush=True)
        try:
            code_nr = r.json().get("code")
        except Exception:  # noqa: BLE001
            code_nr = None
        if code_nr == 1:
            self.request_code(account)
            raise CloudError("Code abgelaufen – ein neuer Code wurde an deine E-Mail geschickt.")
        if code_nr == 2:
            raise CloudError("Code falsch – bitte nochmal eingeben (6-stellig aus der Bambu-Mail).")
        raise CloudError(f"Code abgelehnt (HTTP {r.status_code}): {detail or 'keine Details'}")

    def login_tfa(self, tfa_key: str, code: str) -> str:
        c = self._req("get", f"{self.web}/api/csrf", auth=False)
        csrf = c.cookies.get("bbl_csrf_token")
        if not csrf:
            raise CloudError(f"CSRF-Token nicht erhalten (HTTP {c.status_code})")
        r = self._req("post", f"{self.web}/api/sign-in/tfa", {"tfaKey": tfa_key, "tfaCode": code}, auth=False,
                      headers={"x-bbl-csrf-token": csrf, "Cookie": f"bbl_csrf_token={csrf}"})
        tok = r.cookies.get("token")
        if r.status_code == 200 and tok:
            self.token = tok
            return tok
        detail = self._bambu_error(r)
        print(f"[bambu] 2FA HTTP {r.status_code}: {detail}", flush=True)
        raise CloudError(f"2FA-Code abgelehnt (HTTP {r.status_code}): {detail or 'keine Details'}")

    # ---------- Spulen ----------
    def get_spools(self) -> list[dict[str, Any]]:
        if self.mock:
            return json.loads(Path(self.mock).read_text(encoding="utf-8"))
        if not self.token:
            raise AuthExpired("Nicht mit Bambu verbunden")
        spools, offset = [], 0
        for _ in range(MAX_PAGES):
            r = self._req("get", f"{self.api}/v1/design-user-service/my/filament/v2?offset={offset}&limit={PAGE_SIZE}")
            if r.status_code != 200:
                raise CloudError(f"Spulen konnten nicht geladen werden (HTTP {r.status_code})")
            d = self._json(r)
            hits = [h for h in (d.get("hits") or []) if isinstance(h, dict)]
            spools += hits
            offset += len(hits)
            if not hits or not isinstance(d.get("total"), int) or offset >= d["total"]:
                break
        return spools


def diagnose(region: str = "global") -> list[dict]:
    """Schrittweise prüfen, woran ein Login scheitern würde."""
    import socket
    out = []
    host = "api.bambulab.cn" if region == "china" else "api.bambulab.com"
    out.append({"step": "curl_cffi installiert", "ok": http is not None,
                "detail": getattr(__import__("curl_cffi"), "__version__", "") if http else "fehlt – Image neu bauen"})
    try:
        ip = socket.gethostbyname(host)
        out.append({"step": f"DNS {host}", "ok": True, "detail": ip})
    except OSError as e:
        out.append({"step": f"DNS {host}", "ok": False, "detail": str(e)})
        return out
    if http is None:
        return out
    try:
        r = http.post(f"https://{host}/v1/user-service/user/login", json={"account": "diag@example.invalid", "password": "x", "apiError": ""},
                      impersonate=IMPERSONATE, timeout=TIMEOUT)
        cf = r.status_code in (403, 429) and "cloudflare" in r.text.lower()
        out.append({"step": "HTTPS zur Bambu-API", "ok": not cf and r.status_code in (200, 400, 401),
                    "detail": "von Cloudflare blockiert" if cf else f"HTTP {r.status_code} (400 = API antwortet normal)"})
    except Exception as e:  # noqa: BLE001
        out.append({"step": "HTTPS zur Bambu-API", "ok": False, "detail": f"{type(e).__name__}: {e}"})
    return out
