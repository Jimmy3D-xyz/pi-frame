#!/bin/bash
# Move each tag to the commit on main with the same files, message and date
# (see workflows/move-tags.yml). Stops without changing anything if any tag
# has no twin, or more than one.
set -euo pipefail
git fetch -q --tags --force origin
git config user.name "github-actions[bot]"
git config user.email "41898+github-actions[bot]@users.noreply.github.com"

declare -A twin
for tag in $(git tag -l); do
  old=$(git rev-list -n 1 "$tag")
  if git merge-base --is-ancestor "$old" origin/main; then
    echo "$tag: already on main"
    continue
  fi
  key="$(git log -1 --format='%T %at %s' "$old")"
  matches=$(git log origin/main --format='%H %T %at %s' | awk -v k="$key" '{h=$1; $1=""; sub(/^ /,""); if ($0==k) print h}')
  n=$(printf '%s\n' "$matches" | grep -c . || true)
  if [ "$n" != 1 ]; then
    echo "::error::$tag ($old): found $n matching commits on main, so nothing was moved"
    exit 1
  fi
  twin[$tag]=$matches
  echo "$tag: $old -> $matches"
done

if [ "${DRY_RUN:-false}" = true ]; then
  echo "(dry run: nothing moved)"
  exit 0
fi
for tag in "${!twin[@]}"; do
  git tag -f "$tag" "${twin[$tag]}" >/dev/null
  git push -q --force origin "refs/tags/$tag"
  echo "moved $tag"
done
echo "${#twin[@]} tag(s) moved"
