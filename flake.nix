{
  description = "Motorized fader controller: host app, NixOS service";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems
        (system: f nixpkgs.legacyPackages.${system});

      # Plugins shipped in the source tree: every module in faders/extensions/.
      builtinPlugins = map (nixpkgs.lib.removeSuffix ".py")
        (builtins.filter (n: nixpkgs.lib.hasSuffix ".py" n && n != "__init__.py")
          (builtins.attrNames (builtins.readDir ./faders/extensions)));

      python = pkgs: pkgs.python3.withPackages (ps: [ ps.pyserial ]);
    in
    {
      packages = forAllSystems (pkgs: {
        default = pkgs.stdenvNoCC.mkDerivation {
          pname = "motorized-faders";
          version = "0.1.0";
          src = nixpkgs.lib.fileset.toSource {
            root = ./.;
            fileset = ./faders;
          };
          nativeBuildInputs = [ pkgs.makeWrapper ];
          installPhase = ''
            mkdir -p $out/lib/motorized-faders
            cp -r faders $out/lib/motorized-faders/
            makeWrapper ${python pkgs}/bin/python3 $out/bin/motorized-faders \
              --add-flags "-m faders.run" \
              --prefix PYTHONPATH : $out/lib/motorized-faders \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.pulseaudio ]}
          '';
          meta.mainProgram = "motorized-faders";
        };
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/motorized-faders";
        };
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell { packages = [ (python pkgs) pkgs.pulseaudio ]; };
      });

      checks = forAllSystems (pkgs: {
        tests = pkgs.runCommand "motorized-faders-tests" { } ''
          cp -r ${./.}/. src && chmod -R u+w src && cd src
          ${python pkgs}/bin/python3 tests/run_all.py
          touch $out
        '';
      });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.motorized-faders;
          configFile = pkgs.writeText "motorized-faders.json" (builtins.toJSON {
            port = cfg.port;
            num_faders = cfg.numFaders;
            extensions = cfg.plugins;
          });
        in
        {
          options.services.motorized-faders = {
            enable = lib.mkEnableOption "the motorized fader host (systemd user service)";

            package = lib.mkOption {
              type = lib.types.package;
              default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
              description = "Package providing bin/motorized-faders.";
            };

            port = lib.mkOption {
              type = lib.types.nullOr lib.types.str;
              default = null;
              example = "/dev/ttyACM0";
              description = "Serial port. null finds the Pico by USB VID 0x2E8A.";
            };

            numFaders = lib.mkOption {
              type = lib.types.int;
              default = 4;
              description = "Number of faders on the board.";
            };

            plugins = lib.mkOption {
              type = with lib.types; attrsOf (either (listOf attrs) attrs);
              default = { };
              example = lib.literalExpression ''
                {
                  pulse_sink = [
                    { fader = 0; sink = "sink-master"; max = 100; label = "Master"; }
                    { fader = 3; sink = "sink-music"; max = 150; label = "Music"; }
                  ];
                }
              '';
              description = ''
                Plugins to load, keyed by module name in faders/extensions/
                (available: ${lib.concatStringsSep ", " builtinPlugins}). Each
                value is one instance config or a list of them; see the
                plugin's docstring for its keys.
              '';
            };

            verbose = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Log every volume write, SET: and state edge.";
            };
          };

          config = lib.mkIf cfg.enable {
            assertions = map (name: {
              assertion = builtins.elem name builtinPlugins;
              message = "services.motorized-faders.plugins.${name}: no such plugin "
                + "(available: ${lib.concatStringsSep ", " builtinPlugins})";
            }) (builtins.attrNames cfg.plugins);

            # Logged-in user gets the Pico's tty without joining dialout.
            services.udev.extraRules = ''
              SUBSYSTEM=="tty", ATTRS{idVendor}=="2e8a", TAG+="uaccess"
            '';

            # A user service: plugins like pulse_sink talk to the user's
            # PipeWire session.
            systemd.user.services.motorized-faders = {
              description = "Motorized fader host";
              wantedBy = [ "graphical-session.target" ];
              partOf = [ "graphical-session.target" ];
              after = [ "graphical-session.target" "pipewire-pulse.service" ];
              restartTriggers = [ configFile ];
              serviceConfig = {
                ExecStart = "${lib.getExe cfg.package} --config ${configFile}"
                  + lib.optionalString cfg.verbose " -v";
                # Replugs are handled in-process; this only covers crashes.
                Restart = "always";
                RestartSec = 2;
              };
            };
          };
        };
    };
}
