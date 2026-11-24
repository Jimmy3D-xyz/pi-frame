#!/bin/bash
# One-off (history scrub, 2026-11-24): point each tag at the commit listed in
# .github/tag-map.txt. Every target must be on main; otherwise nothing moves.
set -euo pipefail
git fetch -q --tags --force origin
while read -r tag sha; do
  git cat-file -e "$sha^{commit}" || { echo "::error::$tag: $sha isn't in the repository"; exit 1; }
  git merge-base --is-ancestor "$sha" origin/main || { echo "::error::$tag: $sha isn't on main"; exit 1; }
done < .github/tag-map.txt
while read -r tag sha; do
  if [ "$(git rev-list -n1 "$tag" 2>/dev/null || true)" = "$sha" ]; then echo "$tag: already there"; continue; fi
  echo "$tag -> $sha"
  if [ "${DRY_RUN:-false}" != true ]; then
    git tag -f "$tag" "$sha" >/dev/null
    git push -q --force origin "refs/tags/$tag"
  fi
done < .github/tag-map.txt
echo "done"
