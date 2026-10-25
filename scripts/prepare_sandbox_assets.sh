#!/usr/bin/env bash
# Build the asset bundle a sandbox copies into each workspace:
#   <out>/skills    AgentSociety Claude Code skills (patched, minus proprietary ones)
#   <out>/runtime   .agentsociety/bin/ags.py launcher
#   <out>/plugins   bundled Claude Code plugins (easypaper)
#
#   scripts/prepare_sandbox_assets.sh <out-dir>
set -euo pipefail

out="${1:?usage: prepare_sandbox_assets.sh <out-dir>}"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="$repo/upstream/agentsociety/extension"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

mkdir -p "$work/extension"
for part in skills runtime plugins; do
  cp -R "$src/$part" "$work/extension/$part"
done
"$repo/scripts/apply_upstream_patches.sh" "$work"

# Belt and braces: these are proprietary upstream and must never ship.
for forbidden in docx pdf pptx xlsx; do
  if [ -e "$work/extension/skills/$forbidden" ]; then
    echo "refusing to bundle proprietary skill: $forbidden" >&2
    exit 1
  fi
done
find "$work/extension" -name '__pycache__' -type d -prune -exec rm -rf {} +

rm -rf "$out"
mkdir -p "$(dirname "$out")"
mv "$work/extension" "$out"
echo "sandbox assets written to $out"
