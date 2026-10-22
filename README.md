# Faygo VPN для Linux

Неофициальный клиент. Нужен действующий UUID-ключ Faygo.

## 1. Установи

Открой терминал **в папке проекта**.

**Ubuntu / Debian:**

```sh
sudo apt-get install python3 curl iproute2 nftables
python3 install.py
```

**Arch Linux:**

```sh
sudo pacman -S --needed python curl iproute2 nftables
python3 install.py
```

## 2. Вставь ключ

```sh
~/.local/bin/faygo login
```

Вставь свой токен — UUID-ключ Faygo — и нажми Enter.
При вводе символы не отображаются — это нормально.
Клиент сам сохранит ключ в `~/.config/faygo-cli/key`. Вручную создавать файл не нужно.
Не выкладывай этот файл в GitHub.

## 3. Включи VPN

Сначала отключи другой VPN, если он работает.

```sh
~/.local/bin/faygo run --server 2
```

`2` — Швеция. Команда может запросить пароль `sudo`.
Дождись сообщения **«Faygo VPN включён»**. Интернет Linux будет идти через Faygo.
Оставь терминал открытым. **Для остановки нажми Ctrl+C.**

Другие страны:

```sh
~/.local/bin/faygo servers
```

В WSL запускай команду внутри терминала Ubuntu. VPN работает для Ubuntu, **не для Windows**.

## Если после аварийного закрытия пропал интернет

При обрыве VPN прямой выход блокируется. Останови клиент через Ctrl+C.
Если терминал уже закрыт, открой новый и выполни:

```sh
~/.local/bin/faygo recover
```

## Необязательно: имя устройства

По умолчанию — `Faygo CLI Linux`. Пример своего `X-Device-Name`:

```sh
printf '%s\n' 'MacBook Air qq (macOS Version 27.0.1 (Build 26A434))' \
  > ~/.config/faygo-cli/device-name
```

Делай это после `login`. Можешь подставить своё имя латиницей.
