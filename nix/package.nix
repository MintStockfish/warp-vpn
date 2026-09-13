{ lib, stdenvNoCC, python3, curl, systemd, makeWrapper }:
stdenvNoCC.mkDerivation {
  pname = "warp-vpn";
  version = "0.1.0";
  # Only source files needed for the commands enter the store; no working configs.
  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../bin
      (lib.fileset.fileFilter (file: file.hasExt "py") ../lib)
      ../packaging/warp-vpn.desktop
      ../packaging/codex-warp.desktop
    ];
  };
  nativeBuildInputs = [ makeWrapper ];
  installPhase = ''
    mkdir -p $out/bin $out/lib
    cp -r bin/. $out/bin/
    cp -r lib/. $out/lib/
    chmod +x $out/bin/*
    for program in $out/bin/*; do
      substituteInPlace "$program" --replace-fail '/usr/bin/env python3' '${python3}/bin/python3'
      wrapProgram "$program" --prefix PATH : ${lib.makeBinPath [ curl systemd ]}
    done
    mkdir -p $out/share/applications
    cp packaging/*.desktop $out/share/applications/
    substituteInPlace $out/share/applications/*.desktop --replace-fail '@PREFIX@' "$out"
  '';
  meta = {
    description = "WARP-in-WARP control with per-application HTTP proxy launchers";
    license = lib.licenses.mit;
    platforms = lib.platforms.linux;
    mainProgram = "warp-vpn";
  };
}
