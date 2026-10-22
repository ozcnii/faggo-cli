"""Own credentials, Faygo requests and validation of provider data."""
import json
import os
from pathlib import Path
import pwd
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import write_private

URL = "https://faygoconnect.com/api/v1/"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Access keys must never follow redirects.


class Account:
    def __init__(self):
        user = pwd.getpwnam(os.environ["SUDO_USER"]) if os.geteuid() == 0 and os.environ.get("SUDO_USER") else None
        self.home = Path(user.pw_dir) if user else Path.home()
        self.directory = self.home / ".config/faygo-cli"
        self._owner = (user.pw_uid, user.pw_gid) if user else None

    def login(self, key):
        self._save("key", str(uuid.UUID(key.strip())))
        self._device_id()

    def _save(self, name, value):
        write_private(self.directory / name, value, owner=self._owner)

    def _device_id(self):
        path = self.directory / "device-id"
        if not path.exists():
            self._save("device-id", str(uuid.uuid4()))
        return str(uuid.UUID(path.read_text().strip()))

    def _request(self, endpoint, **params):
        try:
            key = str(uuid.UUID((self.directory / "key").read_text().strip()))
        except (OSError, ValueError):
            raise RuntimeError("Нет UUID-ключа. Выполни: faygo login") from None
        path = self.directory / "device-name"
        name = path.read_text().strip() if path.exists() else "Faygo CLI Linux"
        if not name or len(name) > 128 or any(not 32 <= ord(char) <= 126 for char in name):
            raise RuntimeError("Имя устройства: 1–128 печатных ASCII-символов")
        url = URL + endpoint + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(url, headers={
            "X-Access-Key": key, "X-HwID": self._device_id(), "X-Device-Name": name,
            "X-App-Version": "1.4.1", "Accept": "application/json",
        })
        try:
            with urllib.request.build_opener(_NoRedirect).open(request, timeout=25) as response:
                data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise RuntimeError("Слишком большой ответ Faygo")
            return json.loads(data)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Faygo: HTTP {error.code}. Проверь ключ, подписку и лимит устройств.") from None
        except (urllib.error.URLError, TimeoutError, ValueError):
            raise RuntimeError("Не удалось получить JSON от Faygo (сеть или формат ответа).") from None

    def profile(self):
        result = self._request("public/subscription/profile")
        if not isinstance(result, dict):
            raise RuntimeError("Faygo вернул некорректный профиль")
        return result

    def servers(self):
        result = self._request("public/connection/list")
        if not isinstance(result, list) or any(
            not isinstance(item, dict) or type(item.get("id")) is not int or not isinstance(item.get("name"), str)
            for item in result
        ):
            raise RuntimeError("Faygo вернул некорректный список серверов")
        return result

    def connection(self, server, port):
        result = self._request("public/connection/connect", gate_id=server)
        if not isinstance(result, dict) or result.get("platform") != "singbox":
            raise RuntimeError("Faygo вернул неподдерживаемую платформу")
        configuration = result.get("configuration")
        outbounds = configuration.get("outbounds") if isinstance(configuration, dict) else None
        if not isinstance(outbounds, list) or any(not isinstance(item, dict) for item in outbounds):
            raise RuntimeError("Faygo вернул некорректную конфигурацию")
        proxies = [item for item in outbounds if item.get("tag") == "proxy" and item.get("type") == "vless"]
        if len(proxies) != 1:
            raise RuntimeError("Ожидался один VLESS outbound с tag=proxy")
        proxy = proxies[0].copy()
        tls = proxy.get("tls")
        if not isinstance(tls, dict) or tls.get("enabled") is not True or tls.get("insecure"):
            raise RuntimeError("Отказ от конфигурации без проверки TLS")
        if not isinstance(proxy.get("server"), str) or not proxy["server"] or type(proxy.get("server_port")) is not int or not 1 <= proxy["server_port"] <= 65535:
            raise RuntimeError("Некорректный адрес шлюза Faygo")
        try:
            uuid.UUID(proxy["uuid"])
        except (KeyError, ValueError, TypeError, AttributeError):
            raise RuntimeError("Некорректные VPN-реквизиты Faygo") from None
        for field in ("transport", "multiplex"):
            if proxy.get(field) == {}:
                del proxy[field]
        return {
            "log": {"level": "warn"},
            "inbounds": [{"type": "mixed", "tag": "local", "listen": "127.0.0.1", "listen_port": port}],
            "outbounds": [proxy], "route": {"final": "proxy"},
        }
