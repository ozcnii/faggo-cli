"""Publish a complete installed release through one atomic launcher switch."""
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import tarfile
import tempfile
import urllib.request

VERSION = "1.14.3"
PRODUCT = "faggo-cli"


def engine_path(home):
    installed = home / ".local/bin/faygo"
    candidates = [Path(__file__).resolve().parent.parent / "sing-box"]
    if installed.is_symlink() and _manifest(installed.resolve().parent):
        candidates.append(installed.resolve().parent / "sing-box")
    candidates.append(home / ".local/lib/faygo-cli/sing-box")  # Previous installation.
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    system = shutil.which("sing-box")
    if system:
        return system
    raise RuntimeError("Не найден sing-box. Запусти: python3 install.py")


def _manifest(directory):
    try:
        result = json.loads((directory / "release.json").read_text())
        return result if result.get("product") == PRODUCT else {}
    except (OSError, ValueError, AttributeError):
        return {}


def _source_files(directory):
    return [directory / "faygo", *sorted((directory / "faygo_client").glob("*.py"))]


def _source_hash(directory):
    digest = hashlib.sha256()
    for path in _source_files(directory):
        digest.update(str(path.relative_to(directory)).encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def install(*, force=False):
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or not arch:
        raise RuntimeError("Установщик поддерживает только Linux x86_64 / aarch64")
    source = Path(__file__).resolve().parent.parent
    files = _source_files(source)
    source_hash = _source_hash(source)
    library = Path.home() / ".local/lib/faygo-cli"
    target = Path.home() / ".local/bin/faygo"
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (library / ".install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = target.resolve().parent if target.is_symlink() else None
        manifest = _manifest(previous) if previous else {}
        try:
            same = (manifest.get("source_sha256") == source_hash and manifest.get("core_version") == VERSION
                    and _source_hash(previous) == source_hash)
        except OSError:
            same = False
        if (target.exists() or target.is_symlink()) and not force:
            if same and (previous / "sing-box").is_file():
                core_hash = hashlib.sha256((previous / "sing-box").read_bytes()).hexdigest()
                if core_hash == manifest.get("core_sha256"):
                    print("Этот выпуск уже установлен. Запуск: ~/.local/bin/faygo run")
                    return
            raise RuntimeError("Клиент уже установлен и отличается. Для обновления: python3 install.py --force")
        name = f"sing-box-{VERSION}-linux-{arch}.tar.gz"
        request = urllib.request.Request(
            f"https://api.github.com/repos/SagerNet/sing-box/releases/tags/v{VERSION}",
            headers={"User-Agent": "faygo-cli-installer"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
        asset = next((item for item in release["assets"] if item["name"] == name), None)
        expected = asset.get("digest", "") if asset else ""
        if not expected.startswith("sha256:"):
            raise RuntimeError("GitHub не вернул архив с SHA-256; установка остановлена")
        print(f"Скачиваю официальный sing-box {VERSION} ({arch})…", flush=True)
        with urllib.request.urlopen(asset["browser_download_url"], timeout=120) as response:
            data = response.read(100 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != expected.removeprefix("sha256:"):
            raise RuntimeError("SHA-256 не совпадает; установка остановлена")
        releases = library / "releases"
        releases.mkdir(exist_ok=True, mode=0o700)
        staged = Path(tempfile.mkdtemp(prefix="release-", dir=releases))
        link = None
        try:
            (staged / "release.json").write_text(json.dumps({"product": PRODUCT}))
            for path in files:
                destination = staged / path.relative_to(source)
                destination.parent.mkdir(exist_ok=True, mode=0o700)
                shutil.copyfile(path, destination)
            (staged / "faygo").chmod(0o700)
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                member = archive.getmember(f"sing-box-{VERSION}-linux-{arch}/sing-box")
                if not member.isfile():
                    raise RuntimeError("Некорректный архив sing-box")
                with archive.extractfile(member) as stream, (staged / "sing-box").open("wb") as output:
                    shutil.copyfileobj(stream, output)
            (staged / "sing-box").chmod(0o700)
            (staged / "release.json").write_text(json.dumps({
                "product": PRODUCT, "source_sha256": source_hash,
                "core_version": VERSION,
                "core_sha256": hashlib.sha256((staged / "sing-box").read_bytes()).hexdigest(),
            }))
            # Flush the complete release before making any of it visible to callers.
            for path in staged.rglob("*"):
                if path.is_file():
                    with path.open("rb") as stream:
                        os.fsync(stream.fileno())
            _sync_directory(staged / "faygo_client")
            _sync_directory(staged)
            _sync_directory(releases)
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".faygo-", dir=target.parent)
            os.close(descriptor)
            link = Path(temporary)
            link.unlink()
            link.symlink_to(staged / "faygo")
            os.replace(link, target)
            _sync_directory(target.parent)
        finally:
            if link:
                link.unlink(missing_ok=True)
            # A signal after replace() must not delete the now-active release.
            if not (target.is_symlink() and target.resolve() == staged / "faygo"):
                shutil.rmtree(staged)
        # Keep the active and previous release; never remove unrelated directories.
        for directory in releases.iterdir():
            if directory not in (staged, previous) and not directory.is_symlink() and _manifest(directory):
                shutil.rmtree(directory, ignore_errors=True)
        print("Готово: ~/.local/bin/faygo. Далее: ~/.local/bin/faygo login; ~/.local/bin/faygo run")
