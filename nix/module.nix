{ config, lib, pkgs, ... }:
let
  cfg = config.services.warp-vpn;
  package = pkgs.callPackage ./package.nix {};
in {
  options.services.warp-vpn = {
    enable = lib.mkEnableOption "WARP-in-WARP with a local proxy and full VPN mode";
    proxyPort = lib.mkOption { type = lib.types.port; default = 10881; };
    mihomoPackage = lib.mkOption { type = lib.types.package; default = pkgs.mihomo; };
  };
  config = lib.mkIf cfg.enable {
    assertions = [{
      assertion = cfg.proxyPort >= 1024;
      message = "services.warp-vpn.proxyPort must be at least 1024.";
    }];
    environment.systemPackages = [ package cfg.mihomoPackage ];
    environment.etc."warp-vpn/settings.json".text = builtins.toJSON {
      service_manager = "systemd";
      proxy_port = cfg.proxyPort;
      mihomo = "${cfg.mihomoPackage}/bin/mihomo";
    };
    systemd.services.warp-vpn = {
      description = "WARP-in-WARP VPN and local proxy";
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      wantedBy = []; # Manual activation only.
      serviceConfig = {
        ExecStart = "${package}/bin/warp-vpn _serve";
        StateDirectory = "warp-vpn";
        StateDirectoryMode = "0755";
        RuntimeDirectory = "warp-vpn";
        RuntimeDirectoryMode = "0755";
        RuntimeDirectoryPreserve = "yes";
        # Strings refer to private files on the host; keys never enter the store.
        LoadCredential = [ "tun.json:/etc/warp-vpn/tun.json" "proxy.json:/etc/warp-vpn/proxy.json" ];
        Restart = "on-failure";
        RestartSec = 3;
        TimeoutStopSec = 20;
        UMask = "0077";
        NoNewPrivileges = true;
        CapabilityBoundingSet = [ "CAP_NET_ADMIN" "CAP_NET_RAW" ];
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
      };
    };
    # Replies impersonating LAN DNS servers can arrive through TUN.
    networking.firewall.extraCommands = lib.mkIf
      (config.networking.firewall.checkReversePath != false && !config.networking.nftables.enable)
      (lib.mkAfter ''
        iptables -t mangle -I nixos-fw-rpfilter 1 -i warp-vpn -j RETURN
        ip6tables -t mangle -I nixos-fw-rpfilter 1 -i warp-vpn -j RETURN
      '');
  };
}
