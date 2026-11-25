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
  [agent-harness-sdk]=3359e5fa2b656485cb6e1ef93711ce27eaf6eb0c
  [aegis-war-room]=6309797028ed43748ee4a406851df89a46720060
  [argus-sre-agent]=b257343cc9e0db177394505e8ef1aab589e93b40
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
