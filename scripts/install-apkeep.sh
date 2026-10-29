#!/usr/bin/env bash
# Install a pinned apkeep release (EFForg/apkeep) and check its SHA-256.
set -euo pipefail

APKEEP_VERSION="1.1.0"
APKEEP_SHA256="badd7ad9fa7d2f32abe01eaf33b7ff086361a95bbc000a33bf44b630d4bf2140"

bin_dir="${1:-${RUNNER_TEMP:-/tmp}/apkeep-bin}"
mkdir -p "$bin_dir"
curl -fsSL --retry 3 -o "$bin_dir/apkeep" \
  "https://github.com/EFForg/apkeep/releases/download/${APKEEP_VERSION}/apkeep-x86_64-unknown-linux-gnu"
echo "${APKEEP_SHA256}  $bin_dir/apkeep" | sha256sum --check -
chmod +x "$bin_dir/apkeep"

if [[ -n "${GITHUB_PATH:-}" ]]; then
  echo "$bin_dir" >> "$GITHUB_PATH"
fi
