"""API-only ASC/Google authentication adapted from ops_api.py; signing stays in memory."""
from __future__ import annotations

import base64
import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from .common import Failure, canonical, require

ASC = "https://api.appstoreconnect.apple.com"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTP:
    def request(self, url, method="GET", headers=None, payload=None, raw=None):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                and parsed.port in (None, 443), "UNSAFE_URL", "API 地址必须使用 HTTPS")
        data = canonical(payload) if payload is not None else raw
        headers = dict(headers or {})
        if payload is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        for attempt in range(3):
            try:
                with urllib.request.build_opener(NoRedirect).open(req, timeout=30) as response:
                    body = response.read(64 * 1024 * 1024 + 1)
                    require(len(body) <= 64 * 1024 * 1024, "API_LIMIT", "API 响应超出大小限制")
                    return body, {**dict(response.headers), "_workflow_status": response.status}
            except urllib.error.HTTPError as e:
                status = e.code
                e.close()
                if method == "GET" and status in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(0.25 * 2**attempt)
                    continue
                code = {401: "AUTH_EXPIRED_OR_REVOKED", 403: "AUTH_PERMISSION_DENIED", 404: "REMOTE_NOT_FOUND"}.get(status, "API_REJECTED")
                raise Failure(code, "远端 API 拒绝请求，错误正文已隐藏", http_status=status) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                if method == "GET" and attempt < 2:
                    time.sleep(0.25 * 2**attempt)
                    continue
                raise Failure("API_UNAVAILABLE" if method == "GET" else "OUTCOME_UNKNOWN",
                              "远端请求不可达或结果未知") from None

    def json(self, *args, **kwargs):
        raw, _ = self.request(*args, **kwargs)
        try:
            return json.loads(raw)
        except Exception:
            raise Failure("API_INVALID_RESPONSE", "远端返回了无效 JSON") from None


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwt(private_key, header, payload):
    key = load_pem_private_key(private_key, password=None)
    message = (b64(canonical(header)) + "." + b64(canonical(payload))).encode()
    if header["alg"] == "ES256":
        signature = key.sign(message, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(signature)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    else:
        signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return message.decode() + "." + b64(signature)


class Apple:
    def __init__(self, project, vault, http=None):
        self.project, self.vault, self.http = project, vault, http or HTTP()
        self.ios = project.section("ios")
        self.config = self.ios.get("apple", {})
        self._token, self._expires = "", 0

    def headers(self):
        if time.time() >= self._expires - 60:
            now = int(time.time())
            self._token = jwt(self.vault.read(self.config["p8_ref"], "apple_p8"),
                {"alg": "ES256", "kid": self.config["key_id"], "typ": "JWT"},
                {"iss": self.config["issuer_id"], "iat": now, "exp": now + 900, "aud": "appstoreconnect-v1"})
            self.vault._known.append(self._token)
            self._expires = now + 900
        return {"Authorization": "Bearer " + self._token}

    def get(self, path):
        require(path.startswith("/v1/") and not urllib.parse.urlsplit(path).netloc,
                "UNSAFE_URL", "ASC 只允许固定 API 的读取")
        return self.http.json(ASC + path, headers=self.headers())

    def items(self, path):
        rows = []
        for _ in range(100):
            d = self.get(path)
            require(isinstance(d.get("data"), list), "API_INVALID_RESPONSE", "ASC 列表格式无效")
            rows.extend(d["data"])
            nxt = d.get("links", {}).get("next")
            if not nxt:
                return rows
            require(nxt.startswith(ASC + "/v1/"), "UNSAFE_URL", "ASC 分页地址不受信任")
            path = nxt[len(ASC):]
        raise Failure("API_LIMIT", "ASC 分页超过限制")

    def identity(self):
        app_id = str(self.ios.get("app_store_id", ""))
        require(app_id.isdigit(), "CONFIG_MISSING", "请填写 ASC App ID")
        app = self.get("/v1/apps/" + app_id).get("data", {})
        require(str(app.get("id")) == app_id and app.get("attributes", {}).get("bundleId") == self.ios.get("bundle_id"),
                "TARGET_MISMATCH", "ASC App ID 与 Bundle ID 不匹配")
        return app

    def builds(self, version=None, build=None):
        q = {"filter[app]": str(self.ios["app_store_id"]), "limit": "200", "include": "preReleaseVersion"}
        if build is not None:
            q["filter[version]"] = str(build)
        if version:
            q["filter[preReleaseVersion.version]"] = str(version)
        return self.items("/v1/builds?" + urllib.parse.urlencode(q))


class Google:
    def __init__(self, project, vault, http=None):
        self.project, self.vault, self.http = project, vault, http or HTTP()
        self.config = project.section("analytics").get("google", {})
        self._token, self._expires = "", 0

    def headers(self, admin=False):
        if time.time() >= self._expires - 60:
            service = json.loads(self.vault.read(self.config["service_account_ref"], "google_service_account"))
            now = int(time.time())
            assertion = jwt(service["private_key"].encode(), {"alg": "RS256", "typ": "JWT"},
                {"iss": service["client_email"], "scope": "https://www.googleapis.com/auth/analytics.readonly",
                 "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600})
            raw = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}).encode()
            d = self.http.json("https://oauth2.googleapis.com/token", method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"}, raw=raw)
            require(isinstance(d.get("access_token"), str) and d.get("expires_in", 0) > 60,
                    "API_INVALID_RESPONSE", "Google token 返回无效")
            self._token, self._expires = d["access_token"], now + int(d["expires_in"])
            self.vault._known.extend([assertion, self._token])
        return {"Authorization": "Bearer " + self._token}

    def stream(self):
        prop = str(self.config.get("property_id", ""))
        require(prop.isdigit(), "CONFIG_MISSING", "请填写 GA4 Property ID")
        items, token = [], None
        for _ in range(20):
            query = "?" + urllib.parse.urlencode({"pageToken": token}) if token else ""
            d = self.http.json(f"https://analyticsadmin.googleapis.com/v1beta/properties/{prop}/dataStreams" + query, headers=self.headers())
            items.extend(d.get("dataStreams", []))
            token = d.get("nextPageToken")
            if not token:
                break
        require(not token, "API_LIMIT", "GA4 stream 分页超过限制")
        bundle = self.project.section("ios").get("bundle_id")
        matches = [x for x in items if x.get("iosAppStreamData", {}).get("bundleId") == bundle]
        require(bundle and len(matches) == 1, "TARGET_MISMATCH", "GA4 中目标 iOS stream 缺失或不唯一")
        return matches[0]["name"].split("/")[-1]
