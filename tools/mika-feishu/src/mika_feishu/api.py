"""Versioned public Mika API; no dependency on upstream Python internals."""
import ipaddress
import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    def __init__(self, code, *, uncertain=False, status=None):
        super().__init__(code)
        self.code, self.uncertain, self.status = code, uncertain, status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_url(url):
    p = urllib.parse.urlsplit(url)
    if (p.scheme not in {"http", "https"} or not p.hostname or p.username or
            p.password or p.query or p.fragment or p.path.rstrip("/") not in {"", "/api/v1"}):
        raise ValueError("Use http://PHONE:PORT without credentials, query parameters or other paths")
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
        addresses = socket.getaddrinfo(p.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot resolve the phone address; check the LAN or Tailscale address") from exc
    for entry in addresses:
        a = ipaddress.ip_address(entry[4][0].split("%")[0])
        if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
            a = a.ipv4_mapped
        networks = ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                    "100.64.0.0/10", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10")
        if not any(a in ipaddress.ip_network(n) for n in networks):
            raise ValueError("Only loopback, LAN and private VPN/Tailscale addresses are allowed")
    return urllib.parse.urlunsplit((p.scheme, p.netloc, "/api/v1", "", ""))


class MikaApi:
    def __init__(self, url, token, timeout=12):
        self.base = validate_url(url)
        self.token = token
        self.timeout = timeout
        self.info = None
        self.checked_at = 0
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path="", payload=None):
        # Resolve again on each request. Private service URLs must remain private.
        validate_url(self.base)
        data = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self.token, "Accept": "application/json",
            "Content-Type": "application/json", "User-Agent": "mika-feishu-connector/0.1"})
        writing = method == "POST" and not path.endswith("/wait")
        try:
            with self.opener.open(req, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise ApiError("response_too_large", uncertain=writing)
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except urllib.error.HTTPError as exc:
            # A 5xx after a write might follow execution. Never resubmit automatically.
            raise ApiError("http_" + str(exc.code), uncertain=writing and exc.code >= 500,
                           status=exc.code) from None
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
            raise ApiError("connection_or_response_error", uncertain=writing) from None

    def check(self, force=False):
        if not force and self.info and time.monotonic() - self.checked_at < 30:
            return self.info
        info = self.request("GET")
        if type(info.get("api_version")) is not int or info["api_version"] != 1:
            self.info = None
            raise ApiError("incompatible_api_version")
        if (not isinstance(info.get("capabilities"), list) or
                not all(isinstance(c, str) for c in info["capabilities"]) or
                not isinstance(info.get("limits", {}), dict)):
            self.info = None
            raise ApiError("incompatible_capability_response")
        if "tasks" not in info["capabilities"]:
            self.info = None
            raise ApiError("missing_tasks_capability")
        self.info, self.checked_at = info, time.monotonic()
        return info

    def task(self, task_id):
        self.check()
        result = self.request("GET", "/tasks/" + self.safe_id(task_id))
        if result.get("task_id") != task_id or not isinstance(result.get("status"), str):
            raise ApiError("incompatible_task_response")
        return result

    @staticmethod
    def safe_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
            raise ApiError("invalid_task_id")
        return value

    def start(self, goal, conversation=None):
        info = self.check(force=True)
        self.limit(info, "max_goal", goal, 4000)
        payload = {"goal": goal}
        if conversation:
            payload["conversation"] = conversation
        return self.request("POST", "/tasks", payload)

    def action(self, task_id, action, text=None):
        if action not in {"answer", "steer", "cancel"}:
            raise ApiError("unsupported_action")
        info = self.check(force=True)
        if action == "steer" and "task_steer" not in info.get("capabilities", []):
            raise ApiError("missing_task_steer_capability")
        if text is not None:
            self.limit(info, "max_" + action, text, 2000 if action == "answer" else 4000)
        return self.request("POST", "/tasks/" + self.safe_id(task_id) + "/" + action,
                            {} if text is None else {"text": text})

    @staticmethod
    def limit(info, name, text, fallback):
        maximum = info.get("limits", {}).get(name, fallback)
        if type(maximum) is not int or maximum < 1:
            raise ApiError("incompatible_limits")
        # Kotlin String.length counts UTF-16 units.
        if not text.strip() or len(text.encode("utf-16-le")) // 2 > min(maximum, fallback):
            raise ApiError("text_empty_or_too_long")
