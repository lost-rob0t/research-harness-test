{
  description = "Concurrent deep-research harness laboratory";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    prolog-rlm = {
      url = "git+ssh://forgejo@git.starintel.actor/nsaspy/prolog-rlm.git?rev=e1ed6c2b5f717d9bb380fcf22d144737299197f8";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, prolog-rlm }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
    in {
      devShells = nixpkgs.lib.genAttrs systems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          prologRlm = prolog-rlm.packages.${system}.prolog-rlm;
        in {
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
              swi-prolog
              prologRlm
            ];
            LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [ pkgs.openssl ];
            shellHook = ''
              export PROLOG_RLM_SWIPL="${prologRlm}/bin/prolog-rlm-swipl"
              echo "research-harness-test: OpenRouter + Brave Search MCP + Fetch MCP"
              echo "MCP pins: Brave 2.1.3; Fetch 2026.8.18"
              echo "Prolog-RLM swipl: $PROLOG_RLM_SWIPL"
            '';
          };
        });
    };
}
