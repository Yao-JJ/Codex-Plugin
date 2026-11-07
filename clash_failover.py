"""Local-only Mihomo control and guarded failover; never logs controller secrets."""
from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request


def is_japan_node(name):
    flags = re.findall(r"[\U0001F1E6-\U0001F1FF]{2}", name)
    if flags:
        return flags[0] == "🇯🇵"
    return bool(re.search(r"日本|东京|東京|大阪|名古屋|福冈|福岡|(?<![a-z])(?:jp|jpn|japan|tokyo|osaka)(?![a-z])", name, re.I))

def scalars(path):
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(external-controller|secret|mixed-port|port):\s*(.*?)\s*$", line)
        if match:
            value = match[2]
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1].replace("''", "'")
            else:
                value = value.split(" #", 1)[0].strip()
            result[match[1]] = value
    return result


class ClashClient:
    def __init__(self, address, secret="", proxy_port=0):
        parsed = urllib.parse.urlsplit("http://" + address)
        if parsed.hostname != "localhost" and not ipaddress.ip_address(parsed.hostname).is_loopback:
            raise ValueError("Clash controller must be local")
        if not parsed.port or parsed.username or parsed.path or parsed.query:
            raise ValueError("Invalid local controller address")
        self.base = "http://" + address
        self.secret = secret
        self.proxy_port = int(proxy_port)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def discover(cls):
        folder = Path(os.environ.get("APPDATA", "")) / "io.github.clash-verge-rev.clash-verge-rev"
        for name in ("clash-verge.yaml", "config.yaml"):
            path = folder / name
            if path.is_file():
                values = scalars(path)
                if values.get("external-controller"):
                    return cls(values["external-controller"], values.get("secret", ""), values.get("mixed-port") or values.get("port") or 0)
        raise FileNotFoundError("Clash local controller unavailable")

    def request(self, path, method="GET", body=None, timeout=8):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Bearer " + self.secret, "Content-Type": "application/json"}
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024)
        return json.loads(raw) if raw else None

    def target(self, proxies):
        config = self.request("/configs")
        if config.get("mode", "").lower() != "rule":
            raise ValueError("Only rule-mode AI-specific groups are supported")
        # Stop at the first explicit rule matching our actual probe host.
        for rule in self.request("/rules").get("rules", []):
            payload = rule.get("payload", "").lower()
            kind = rule.get("type")
            match = ((kind == "Domain" and payload == "chatgpt.com") or
                     (kind == "DomainSuffix" and ("chatgpt.com" == payload or "chatgpt.com".endswith("." + payload))) or
                     (kind == "DomainKeyword" and payload and payload in "chatgpt.com"))
            if match:
                group = rule.get("proxy")
                # Do not alter a shared generic selector or GLOBAL automatically.
                if not re.search(r"openai|chatgpt|\bai\b|AI网站|人工智能", group or "", re.I):
                    raise ValueError("No dedicated AI selector found")
                if proxies.get(group, {}).get("type") != "Selector":
                    raise ValueError("AI group is not a manual selector")
                return group
        raise ValueError("No explicit AI route found")

    @staticmethod
    def leaf(proxies, name):
        seen = set()
        while name not in seen:
            seen.add(name)
            proxy = proxies.get(name, {})
            if "all" not in proxy:
                return name
            name = proxy.get("now")
        return None

    def plan(self):
        proxies = self.request("/proxies")["proxies"]
        group = self.target(proxies)
        entry = proxies[group]
        current = entry.get("now")
        current_leaf = self.leaf(proxies, current)
        members = []
        for name in entry.get("all", []):
            p = proxies.get(name, {})
            if "all" in p or p.get("type", "").lower() in ("direct", "reject", "rejectdrop", "pass", "compatible", "dns"):
                continue
            if name != current_leaf and name not in members and p.get("type") and is_japan_node(name):
                members.append(name)
        return {"group": group, "old": current, "current_leaf": current_leaf, "candidates": members}

    def select(self, group, name):
        self.request("/proxies/" + urllib.parse.quote(group, safe=""), "PUT", {"name": name})

    def current(self, group):
        return self.request("/proxies/" + urllib.parse.quote(group, safe="")).get("now")

    def unchanged(self, plan):
        proxies = self.request("/proxies")["proxies"]
        current = proxies.get(plan["group"], {}).get("now")
        return current == plan["old"] and self.leaf(proxies, current) == plan["current_leaf"]

    def choose(self, plan, allowed):
        # Rotate starting point across attempts and bound work to eight nodes.
        candidates = [name for name in plan["candidates"] if is_japan_node(name)]
        if not candidates:
            return None
        start = int(time.time() // 300) % len(candidates)
        candidates = (candidates[start:] + candidates[:start])[:8]
        for name in candidates:
            if not allowed():
                return None
            path = "/proxies/" + urllib.parse.quote(name, safe="") + "/delay?" + urllib.parse.urlencode({"url": "https://chatgpt.com", "timeout": 5000, "expected": "200-499"})
            try:
                delay = self.request(path).get("delay", 0)
                if 0 < delay < 5000:
                    return name
            except Exception:
                continue
        return None


@dataclass
class FailoverGuard:
    state_path: Path
    streak: int = 0
    context: tuple | None = None
    last_probe: float = 0
    attempts: list = field(default_factory=list)

    def __post_init__(self):
        try:
            self.attempts = [float(x) for x in json.loads(self.state_path.read_text(encoding="utf-8")).get("attempts", [])]
        except (OSError, ValueError, TypeError):
            self.attempts = []

    def observe(self, result, context, now):
        # Reset on progress, completion, disabled monitoring, stale/missing samples,
        # HTTP 401/403/429, or certificate configuration errors (not node evidence).
        if context != self.context or now - self.last_probe > 45:
            self.streak = 0
        self.context = context
        self.last_probe = now
        failed = result.get("ok") is False and result.get("kind") != "certificate"
        if not context or not failed or not 0 <= now - result.get("at", 0) < 15 or result.get("at", 0) <= context[2]:
            self.streak = 0
            return False
        self.streak += 1
        recent = [at for at in self.attempts if now - at < 3600]
        return self.streak >= 3 and len(recent) < 3 and (not self.attempts or now - self.attempts[-1] >= 300)

    def reserve(self, now):
        self.attempts = [at for at in self.attempts if now - at < 3600] + [now]
        # Persist before mutation so restart cannot bypass the cooldown/rate limit.
        self.state_path.write_text(json.dumps({"attempts": self.attempts}), encoding="utf-8")
        self.streak = 0
