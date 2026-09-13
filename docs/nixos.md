# NixOS

На NixOS используется модуль `nix/module.nix`. `install.py` здесь намеренно
не применяется. Нужен nixpkgs с Mihomo, поддерживающим AmneziaWG; пакет
проверен на nixpkgs 26.05 с Mihomo 1.19.24.

## Модуль

Разместите копию исходников в стабильном месте, например
`/etc/nixos/warp-vpn-src`. Добавьте в системную конфигурацию:

```nix
{
  imports = [ ./warp-vpn-src/nix/module.nix ];
  services.warp-vpn.enable = true;
}
```

```sh
sudo nixos-rebuild switch
```

Модуль устанавливает команды, Mihomo и службу `warp-vpn.service`.
Служба не запускается автоматически; приватные ключи в Nix store не попадают.
Если Mihomo нужно взять из другой версии nixpkgs, задайте
`services.warp-vpn.mihomoPackage`.

## Приватные конфиги

Установите WARPSCOUT по [инструкции](dependencies.md) и выполните обычным
пользователем:

```sh
warp-vpn setup --exclude-country BY
```

Скопируйте только два сгенерированных конфига:

```sh
sudo install -m 600 "$HOME/.local/share/warp-vpn/tun.json" /etc/warp-vpn/tun.json
sudo install -m 600 "$HOME/.local/share/warp-vpn/proxy.json" /etc/warp-vpn/proxy.json
warp-vpn proxy
warp-vpn status
```

Не записывайте ключи в `.nix`-файлы и не добавляйте их в дерево исходников.
Не используйте здесь `install-config`: `settings.json` управляется NixOS.
При смене порта согласуйте `warp-vpn setup --port ...` с
`services.warp-vpn.proxyPort` и пересоберите систему.

## Переход со старой локальной настройки

Если уже настроены отдельные `mihomo.service` / `warp-proxy.service` из
предыдущей версии, сначала выключите старый VPN. Затем удалите импорт
старого модуля и подключите новый. Старые службы и новый `warp-vpn.service`
не должны использовать одни ключи или порт одновременно.

Существующий полный `config.json` совместим по формату с новым `tun.json`;
старый `proxy.json` соответствует новому `proxy.json`. Сначала сделайте
приватную резервную копию. Новый модуль не переносит файлы автоматически.
Пользовательские Polkit-правила старого модуля больше не нужны:
новая команда использует обычный административный доступ.

## Firewall

Для стандартного iptables firewall NixOS модуль добавляет ограниченное
исключение reverse-path filtering на интерфейсе `warp-vpn`. Оно требуется
для перехваченных DNS-ответов с адресом локального маршрутизатора.
Остальные правила сохраняются.

При `networking.nftables.enable = true` это исключение не добавляется;
полный режим с таким firewall нужно проверить отдельно. Прокси-режим
не создаёт TUN и не требует этого исключения.

Для удаления выключите VPN, уберите импорт/включение модуля и выполните
`nixos-rebuild switch`. Удаление приватных конфигов остаётся отдельным действием.
