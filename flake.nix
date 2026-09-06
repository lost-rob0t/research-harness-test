{
  description = "Concurrent deep-research harness laboratory";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in {
      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = with pkgs; [
            python311
            uv
            nodejs_24
            git
            jq
            curl
            sbcl
            openssl
          ];
          LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.openssl ];
          shellHook = ''
            echo "research-harness-test: OpenRouter + Brave Search MCP + Fetch MCP"
            echo "MCP pins: Brave 2.1.3; Fetch 2026.8.18"
          '';
        };
      });
    };
}
