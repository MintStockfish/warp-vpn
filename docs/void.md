# Void Linux — основной вариант

Используется runit. X11, dwm, Wayland и полноценная среда рабочего стола
для командной строки не требуются.

## 1. Зависимости

```sh
sudo xbps-install -S python3 curl ca-certificates git
```

Установите Mihomo и WARPSCOUT по [инструкции](dependencies.md). Для первого
переноса проще использовать Void glibc; особенности musl описаны там же.
Команды ниже предполагают настроенный `sudo`; допустим эквивалент через `doas`.

## 2. Установка из репозитория

Откройте корень клонированного репозитория:

```sh
sudo python3 install.py --backend runit --mihomo /usr/local/bin/mihomo
```

Если Mihomo установлен пакетным менеджером в `/usr/bin/mihomo`, передайте этот
путь. Установщик создаёт `/etc/sv/warp-vpn/run`, файл `down` и ссылку
`/var/service/warp-vpn`. Служба доступна для ручного запуска, но остаётся
выключенной при старте системы. Подробности механизма `down` — в
[руководстве Void](https://docs.voidlinux.org/config/services/index.html).

## 3. Получение подключения

В обычной пользовательской сессии:

```sh
warp-vpn setup --exclude-country BY
sudo warp-vpn install-config "$HOME/.local/share/warp-vpn"
warp-vpn proxy
warp-vpn status
codex-warp
```

`BY` замените кодом своей страны либо опустите параметр для автоматического
определения. Для полного VPN: `warp-vpn on`; для отключения: `warp-vpn off`.
Начальный запуск или переключение может запросить административный пароль.
Если прокси уже работает, повторный `codex-warp` не перезапускает службу.

## Обновление и удаление

Для обновления остановите службу, обновите исходники и повторите установку:

```sh
warp-vpn off
git pull
sudo python3 install.py --backend runit --mihomo /usr/local/bin/mihomo
```

Установщик сохраняет порт и приватные конфиги, обновляя тип службы и путь
к Mihomo в `settings.json` согласно переданным параметрам.
После обновления выполните `warp-vpn proxy`.

Для удаления сначала выполните `warp-vpn off`. Затем удалите ссылку
`/var/service/warp-vpn`, каталог `/etc/sv/warp-vpn`, три команды из
`/usr/local/bin` и `/usr/local/lib/warp-vpn`. При необходимости удалите
`warp-vpn.desktop` и `codex-warp.desktop` из `/usr/local/share/applications`.
Приватные `/etc/warp-vpn`, `~/.local/share/warp-vpn` и состояние
`/var/lib/warp-vpn` удаляются отдельно по вашему решению.

## Firewall и диагностика

Обычно маршруты и перехват DNS создаёт сам Mihomo. Если у вас настроен
собственный nftables/iptables firewall, проверьте, что он пропускает трафик
интерфейса `warp-vpn`. Не переносите цепочку `nixos-fw-rpfilter` на Void:
она относится к конкретному firewall NixOS.

```sh
sudo sv status /var/service/warp-vpn
warp-vpn doctor
```

Если служба завершается слишком рано, остановите её и запустите обработчик
на переднем плане для чтения ошибки:

```sh
warp-vpn off
sudo /usr/local/bin/warp-vpn _serve
```

`Ctrl+C` остановит этот диагностический запуск. Не запускайте его параллельно
с работающей службой. Дополнительные случаи — в [диагностике](troubleshooting.md).
