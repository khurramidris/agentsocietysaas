#!/usr/bin/env bash
# Apply upstream/patches/*.patch to a COPY of the vendored upstream tree.
#
#   scripts/apply_upstream_patches.sh <target-dir>
#
# <target-dir> must contain a copy of upstream/agentsociety (or the subset the
# patches touch, with the same relative layout). The vendored snapshot under
# upstream/agentsociety itself is never modified, so `sync_upstream.py` can be
# re-run at any time and the patches re-applied on top.
set -euo pipefail

target="${1:?usage: apply_upstream_patches.sh <target-dir>}"
patch_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../upstream/patches" && pwd)"

shopt -s nullglob
patches=("$patch_dir"/*.patch)
if [ ${#patches[@]} -eq 0 ]; then
  echo "no patches found in $patch_dir" >&2
  exit 0
fi

for p in "${patches[@]}"; do
  # Dry-run first so a partially applicable patch never leaves a half-patched tree.
  patch --directory "$target" --strip 1 --forward --dry-run --silent < "$p"
  patch --directory "$target" --strip 1 --forward --silent < "$p"
  echo "applied $(basename "$p")"
done
