#!/usr/bin/env python3
"""Install an official sing-box binary and the CLI for the current Linux user."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import shutil
import tarfile
import urllib.request

VERSION = "1.14.3"


def main():
    os.umask(0o077)
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or not arch:
        raise SystemExit("Установщик поддерживает только Linux x86_64 / aarch64")
    name = f"sing-box-{VERSION}-linux-{arch}.tar.gz"
    url = f"https://api.github.com/repos/SagerNet/sing-box/releases/tags/v{VERSION}"
    request = urllib.request.Request(url, headers={"User-Agent": "faygo-cli-installer"})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next(item for item in release["assets"] if item["name"] == name)
    digest = asset.get("digest", "")
    if not digest.startswith("sha256:"):
        raise SystemExit("GitHub не вернул SHA-256; установка остановлена")
    print(f"Скачиваю официальный sing-box {VERSION} ({arch})…", flush=True)
    with urllib.request.urlopen(asset["browser_download_url"], timeout=120) as response:
        data = response.read(100 * 1024 * 1024 + 1)
    if hashlib.sha256(data).hexdigest() != digest.removeprefix("sha256:"):
        raise SystemExit("SHA-256 не совпадает; установка остановлена")
    library = Path.home() / ".local/lib/faygo-cli"
    library.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        member = archive.getmember(f"sing-box-{VERSION}-linux-{arch}/sing-box")
        if not member.isfile():
            raise SystemExit("Некорректный архив sing-box")
        source = archive.extractfile(member)
        temporary = library / "sing-box.download"
        with temporary.open("wb") as destination:
            shutil.copyfileobj(source, destination)
        temporary.chmod(0o700)
        temporary.replace(library / "sing-box")
    bindir = Path.home() / ".local/bin"
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / "faygo"
    if target.exists() and target.read_bytes() != Path(__file__).with_name("faygo").read_bytes():
        raise SystemExit("~/.local/bin/faygo уже существует и отличается; не перезаписываю")
    shutil.copyfile(Path(__file__).with_name("faygo"), target)
    target.chmod(0o700)
    print("Готово: ~/.local/bin/faygo. Если команда не найдена: export PATH=\"$HOME/.local/bin:$PATH\"")
    print("Далее: faygo login; faygo profile; faygo servers; faygo run")


if __name__ == "__main__":
    main()
