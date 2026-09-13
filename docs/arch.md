# Arch Linux

Используется systemd. Команды и конфигурация туннеля общие с Void.

## Установка

```sh
sudo pacman -S --needed python curl ca-certificates git
```

Установите Mihomo и WARPSCOUT по [инструкции зависимостей](dependencies.md).
Из корня клонированного репозитория:

```sh
sudo python3 install.py --backend systemd --mihomo /usr/local/bin/mihomo
warp-vpn setup --exclude-country BY
sudo warp-vpn install-config "$HOME/.local/share/warp-vpn"
warp-vpn proxy
warp-vpn status
```

Если используете пакет с бинарником `/usr/bin/mihomo`, измените `--mihomo`.
Служба устанавливается в `/etc/systemd/system/warp-vpn.service`, выполняется
`daemon-reload`. Установщик не запускает её и не включает автозапуск.

```sh
codex-warp       # Codex и его программы, поддерживающие HTTP-прокси
warp-vpn on     # VPN для всего компьютера
warp-vpn off
```

## Диагностика

```sh
systemctl status warp-vpn.service
sudo journalctl -u warp-vpn.service -n 50 --no-pager
warp-vpn doctor
```

Для полного режима нужен доступ к `/dev/net/tun`. При собственном firewall
проверьте правила для интерфейса `warp-vpn`, особенно DNS и reverse-path filtering.
Установщик не меняет существующие правила firewall.

## Обновление и удаление

Остановите службу, обновите репозиторий и повторите `install.py` с теми же
параметрами. Приватные конфиги и порт сохраняются; тип службы и путь к Mihomo
в `settings.json` обновляются согласно параметрам установщика.

Для удаления сначала выполните `warp-vpn off` и, если вручную включали
автозапуск, `sudo systemctl disable warp-vpn.service`. Удалите созданный unit,
три команды из `/usr/local/bin`, `/usr/local/lib/warp-vpn` и два `.desktop` файла
из `/usr/local/share/applications`; затем выполните `sudo systemctl daemon-reload`.
Приватные конфиги и состояние удаляются отдельно по вашему решению.
