"""Own engine lifetime, Linux network changes and crash recovery together."""
from contextlib import contextmanager, nullcontext
import base64
import ctypes
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from . import write_private

STATE_DIR = Path("/var/lib/faygo-cli")
STATE_FILE = STATE_DIR / "state.json"
RESOLVER = Path("/etc/resolv.conf")
TUN, TABLE, MARK = "faygo0", "20580", "0xf2080"
PRIORITIES = range(20580, 20585)
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
VPN_DNS = b"nameserver 172.31.255.2\noptions timeout:2 attempts:2\n"
FIREWALL = f'''table inet faygo_cli {{
 chain output {{
  type filter hook output priority -10; policy drop;
  oifname "lo" accept
  meta mark {MARK} accept
  oifname "{TUN}" accept
  tcp sport {{ 22, 2222 }} ct state established accept
  udp sport 68 udp dport 67 accept
  counter reject with icmpx type admin-prohibited
 }}
 chain forward {{
  type filter hook forward priority -10; policy drop;
  oifname "{TUN}" accept
  iifname "{TUN}" accept
 }}
}}
'''


def _command(*args, check=True, input=None):
    result = subprocess.run(args, input=input, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"Команда {' '.join(args)}: {result.stderr.strip()[:300]}")
    return result


def _require_root():
    if os.geteuid() != 0:
        raise RuntimeError("Для полного VPN и восстановления нужны права root")
    for tool in ("ip", "nft"):
        if not shutil.which(tool):
            raise RuntimeError(f"Не найден {tool}; нужны iproute2 и nftables")


@contextmanager
def _network_lock():
    _require_root()
    with open("/run/faygo-cli.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Faygo VPN уже работает; сначала останови его") from None
        yield


@contextmanager
def _signals():
    def interrupt(*_):
        raise KeyboardInterrupt
    previous = {sig: signal.signal(sig, interrupt) for sig in SIGNALS}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _firewall_exists():
    tables = json.loads(_command("nft", "-j", "list", "tables").stdout)["nftables"]
    return any(x.get("table", {}).get("name") == "faygo_cli" and
               x.get("table", {}).get("family") == "inet" for x in tables)


def _preflight():
    if STATE_FILE.exists():
        raise RuntimeError("Остался аварийный сеанс. Сначала выполни: faygo recover")
    if _firewall_exists() or _command("ip", "link", "show", TUN, check=False).returncode == 0:
        raise RuntimeError("Имя faygo0 или firewall faygo_cli занято; ничего не меняю")
    for family in ("-4", "-6"):
        rules = json.loads(_command("ip", family, "-j", "rule").stdout)
        if any(x.get("priority") in PRIORITIES or x.get("oif") == TUN or x.get("iif") == TUN for x in rules):
            raise RuntimeError("Правила маршрутизации Faygo уже заняты")
        if _command("ip", family, "route", "show", "table", TABLE, check=False).stdout.strip():
            raise RuntimeError("Таблица маршрутов 20580 уже занята")
    _command("nft", "--check", "--file", "-", input=FIREWALL)


def _process_token(pid):
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None


def _stop_owned_process(state):
    pid, token = state.get("pid"), state.get("process_token")
    if not pid or not token or _process_token(pid) != token:
        return
    try:
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while _process_token(pid) == token and time.monotonic() < deadline:
            time.sleep(0.1)
        if _process_token(pid) == token:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _write_resolver(data):
    RESOLVER.write_bytes(data)  # Keep the resolver's symlink, inode and permissions.
    with RESOLVER.open("rb") as stream:
        os.fsync(stream.fileno())


def _restore_network(*, prompt=False):
    if not STATE_FILE.exists():
        return
    state = json.loads(STATE_FILE.read_text())
    original = base64.b64decode(state["resolv_conf"], validate=True)
    current = RESOLVER.read_bytes()
    if current not in (VPN_DNS, original):
        accepted = False
        if prompt and sys.stdin.isatty():
            accepted = input("DNS изменён. Вернуть DNS до запуска Faygo? [y/N]: ").strip().lower() in ("y", "yes", "д", "да")
        if not accepted:
            # Recovery may be pending before startup ever installed the firewall.
            if not _firewall_exists():
                _command("nft", "--file", "-", input=FIREWALL)
            raise RuntimeError("DNS изменён: резервная копия и блокировка сохранены. Запусти faygo recover в терминале и подтверди возврат DNS.")
    _stop_owned_process(state)
    if current != original:
        _write_resolver(original)
    for family in ("-4", "-6"):
        rules = json.loads(_command("ip", family, "-j", "rule").stdout)
        for rule in reversed(rules):
            if rule.get("priority") in PRIORITIES and str(rule.get("table")) in (TABLE, "main", "254"):
                _command("ip", family, "rule", "del", "pref", str(rule["priority"]))
            elif rule.get("oif") == TUN or rule.get("iif") == TUN:
                # sing-tun may leave an extra IPv6 oif rule after a crash.
                args = ["ip", family, "rule", "del", "pref", str(rule["priority"]), "table", str(rule["table"])]
                for selector in ("oif", "iif"):
                    if rule.get(selector) == TUN:
                        args += [selector, TUN]
                _command(*args)
        routes = _command("ip", family, "route", "show", "table", TABLE, check=False)
        if routes.stdout.strip():
            _command("ip", family, "route", "flush", "table", TABLE)
    if _command("ip", "link", "show", TUN, check=False).returncode == 0:
        _command("ip", "link", "delete", TUN)
    if _firewall_exists():
        _command("nft", "delete", "table", "inet", "faygo_cli")
    (STATE_DIR / "config.json").unlink(missing_ok=True)
    STATE_FILE.unlink()
    print("Правила Faygo убраны, DNS возвращён.", flush=True)


def recover():
    with _network_lock(), _signals():
        if not STATE_FILE.exists():
            print("Сохранённого сеанса Faygo нет; сеть не изменяю.")
            return
        _restore_network(prompt=True)


def _configure_tun(config):
    proxy = config["outbounds"][0]
    proxy["server"] = socket.getaddrinfo(proxy["server"], proxy["server_port"], socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    proxy["routing_mark"] = int(MARK, 16)
    config["inbounds"].append({
        "type": "tun", "tag": "vpn", "interface_name": TUN,
        "address": ["172.31.255.1/30", "fdfe:dcba:9876::1/126"],
        "mtu": 1500, "auto_route": False, "dns_mode": "disabled", "stack": "mixed",
    })
    config["dns"] = {
        "servers": [{"type": "https", "tag": "remote", "server": "1.1.1.1", "detour": "proxy",
                     "tls": {"enabled": True, "server_name": "cloudflare-dns.com"}}],
        "final": "remote", "strategy": "ipv4_only",
    }
    config["route"]["rules"] = [{"port": 53, "action": "hijack-dns"}]


def _apply_network():
    _command("nft", "--file", "-", input=FIREWALL)
    for family in ("-4", "-6"):
        _command("ip", family, "route", "add", "default", "dev", TUN, "table", TABLE)
        _command("ip", family, "rule", "add", "pref", "20580", "fwmark", MARK, "lookup", "main")
        for priority, port in (("20581", "22"), ("20582", "2222")):
            _command("ip", family, "rule", "add", "pref", priority, "ipproto", "tcp", "sport", port, "lookup", "main")
        if family == "-4":
            _command("ip", family, "rule", "add", "pref", "20583", "ipproto", "udp", "sport", "68", "dport", "67", "lookup", "main")
        _command("ip", family, "rule", "add", "pref", "20584", "lookup", TABLE)
    _write_resolver(VPN_DNS)


def _die_with_parent(parent):
    # The kernel owns the launch-to-journal gap, including controller SIGKILL.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
        os._exit(127)
    signal.pthread_sigmask(signal.SIG_SETMASK, [])


class _Engine:
    def __init__(self, binary, path):
        self.binary, self.path, self.process = binary, path, None

    def start(self, state):
        checked = subprocess.run([self.binary, "check", "-c", str(self.path)], capture_output=True)
        if checked.returncode:
            raise RuntimeError("sing-box отклонил конфигурацию; секретные параметры не выводятся")
        previous = signal.pthread_sigmask(signal.SIG_BLOCK, SIGNALS)
        try:
            parent = os.getpid()
            self.process = subprocess.Popen(
                [self.binary, "run", "-c", str(self.path)], start_new_session=True,
                preexec_fn=lambda: _die_with_parent(parent),
            )
            if state is not None:
                state.update(pid=self.process.pid, process_token=_process_token(self.process.pid))
                write_private(STATE_FILE, json.dumps(state))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous)

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def _wait_ready(process, port, *, proxy):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("sing-box завершился при запуске")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                if proxy or _command("ip", "link", "show", TUN, check=False).returncode == 0:
                    return
        except OSError:
            pass
        time.sleep(0.1)
    raise RuntimeError("sing-box не запустился за 10 секунд")


def _check_connection(port, *, proxy):
    if not shutil.which("curl"):
        raise RuntimeError("Для --check нужен curl")
    result = subprocess.run([
        "curl", "-fsS", "--proxy", f"socks5h://127.0.0.1:{port}" if proxy else "",
        "--noproxy", "" if proxy else "*", "--connect-timeout", "10", "--max-time", "30",
        "https://www.cloudflare.com/cdn-cgi/trace",
    ], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"HTTPS через Faygo не прошёл проверку: {result.stderr.strip()[:300]}")
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    print(f"HTTPS через Faygo работает. Выход: {fields.get('loc', '?')}; IP: {fields.get('ip', '?')}", flush=True)


def run(config, binary, port, *, proxy=False, check=False):
    if sys.platform != "linux":
        raise RuntimeError("Запуск VPN поддерживается только на Linux")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            raise RuntimeError(f"Порт {port} занят. Выбери другой через --port.") from None
    with _signals(), (nullcontext() if proxy else _network_lock()):
        if not proxy:
            _preflight()
            _configure_tun(config)
            STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (tempfile.TemporaryDirectory(prefix="faygo-") if proxy else nullcontext(STATE_DIR)) as directory:
            path = Path(directory) / "config.json"
            engine = _Engine(binary, path)
            try:
                write_private(path, json.dumps(config))
                state = None
                if not proxy:
                    state = {"resolv_conf": base64.b64encode(RESOLVER.read_bytes()).decode()}
                    write_private(STATE_FILE, json.dumps(state))
                engine.start(state)
                process = engine.process
                _wait_ready(process, port, proxy=proxy)
                if proxy:
                    print(f"Faygo: socks5h://127.0.0.1:{port} (также HTTP proxy). Ctrl+C — остановить.", flush=True)
                else:
                    _apply_network()
                    print("Faygo VPN включён: TCP/UDP и DNS через Faygo; обход заблокирован. Ctrl+C — восстановить сеть.", flush=True)
                if check:
                    _check_connection(port, proxy=proxy)
                else:
                    code = process.wait()
                    if proxy:
                        if code:
                            raise RuntimeError(f"sing-box завершился с кодом {code}")
                    else:
                        print(f"Движок остановился ({code}). Обход заблокирован до Ctrl+C.", file=sys.stderr, flush=True)
                        while True:
                            signal.pause()
            finally:
                for sig in SIGNALS:
                    signal.signal(sig, signal.SIG_IGN)
                engine.stop()
                if not proxy:
                    if STATE_FILE.exists():
                        _restore_network()
                    else:
                        path.unlink(missing_ok=True)
