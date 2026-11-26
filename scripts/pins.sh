# The sibling commits Themis was last tested against. Sourced by ecosystem.sh and the bench-real workflow.
# To test against newer commits, change a pin here and run ./scripts/ecosystem.sh.
declare -A pins=(
  [agent-harness-sdk]=afd6e1ea577eb2ba4e68d457d385c97b8189243a
  [aegis-war-room]=6309797028ed43748ee4a406851df89a46720060
  [argus-sre-agent]=b257343cc9e0db177394505e8ef1aab589e93b40
  [intentops]=d30da595b1708ab363a666569c9721c6bbd7f91a
)

# clone_pinned <dir> <repo>...: each repo checked out at its pin under <dir>
clone_pinned() {
  local dir="$1" repo
  shift
  mkdir -p "$dir"
  for repo in "$@"; do
    [ -d "$dir/$repo/.git" ] || git clone --quiet "https://github.com/debanjan0212/$repo" "$dir/$repo"
    git -C "$dir/$repo" fetch --quiet origin "${pins[$repo]}"
    git -C "$dir/$repo" checkout --quiet "${pins[$repo]}"
  done
}
