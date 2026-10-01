#!/usr/bin/env bash
# Tag and publish the next GitHub Release for the commit in $GITHUB_SHA.
# Called by .github/workflows/release.yml. Needs GH_TOKEN, GITHUB_SHA and
# GITHUB_REPOSITORY; BUMP_INPUT (minor|patch|major) is set on manual runs only.
set -euo pipefail

SEMVER='^v[0-9]+\.[0-9]+\.[0-9]+$'

if git tag --points-at "$GITHUB_SHA" | grep -Eq "$SEMVER"; then
  echo "Commit $GITHUB_SHA is already released; nothing to do."
  exit 0
fi

bump="${BUMP_INPUT:-}"
if [ -z "$bump" ]; then
  # The PR that produced this commit (empty for a direct push).
  labels=$(gh api "repos/$GITHUB_REPOSITORY/commits/$GITHUB_SHA/pulls" \
             --jq '.[0].labels[].name' 2>/dev/null || true)
  case " $(echo "$labels" | tr '\n' ' ') " in
    *" release:skip "*)  echo "The merged PR is labelled release:skip; no release."; exit 0 ;;
    *" release:major "*) bump=major ;;
    *" release:patch "*) bump=patch ;;
    *)                   bump=minor ;;
  esac
fi

latest=$(git tag --list | grep -E "$SEMVER" | sort -V | tail -n 1 || true)
IFS=. read -r major minor patch <<< "${latest:-v0.0.0}"
major=${major#v}
case "$bump" in
  major) next="v$((major + 1)).0.0" ;;
  patch) next="v${major}.${minor}.$((patch + 1))" ;;
  minor) next="v${major}.$((minor + 1)).0" ;;
  *)     echo "Unknown bump '$bump' (expected minor, patch or major)." >&2; exit 1 ;;
esac

echo "Releasing $next (previous: ${latest:-none}, bump: $bump)"
gh release create "$next" --target "$GITHUB_SHA" --title "$next" --generate-notes
