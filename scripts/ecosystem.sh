#!/usr/bin/env bash
# Clone the four sibling projects at the commits Themis was last tested against, install them in their own
# virtualenv, and run the ecosystem tests against them for real.
#
#   ./scripts/ecosystem.sh            # uses ./.siblings
#   ./scripts/ecosystem.sh /some/dir
#
# To test against newer commits, change the pins below and run it again.
set -euo pipefail

dir="${1:-.siblings}"
declare -A pins=(
  [agent-harness-sdk]=da182bebb0c5e1956e3751c0a233944ef1e68d51
  [aegis-war-room]=d884af6d29b2c0eedde3546133a22d10f97396a7
  [argus-sre-agent]=d9a24eb232c998e261b35213a86e350438ea8697
  [intentops]=d30da595b1708ab363a666569c9721c6bbd7f91a
)

mkdir -p "$dir"
for repo in "${!pins[@]}"; do
  [ -d "$dir/$repo/.git" ] || git clone --quiet "https://github.com/debanjan0212/$repo" "$dir/$repo"
  git -C "$dir/$repo" fetch --quiet origin "${pins[$repo]}"
  git -C "$dir/$repo" checkout --quiet "${pins[$repo]}"
done

[ -x "$dir/venv/bin/python" ] || python3 -m venv "$dir/venv"
"$dir/venv/bin/pip" install --quiet \
  -e "$dir/agent-harness-sdk[server]" -e "$dir/aegis-war-room" -e "$dir/argus-sre-agent" -e "$dir/intentops"

abs="$(cd "$dir" && pwd)"
THEMIS_SIBLINGS="$abs" THEMIS_SIBLINGS_PYTHON="$abs/venv/bin/python" python -m pytest -m ecosystem -q
