#!/bin/sh
# Fail if a published checkout contains one of TWILL's private artifact trees.
#
# This check deliberately scans the checkout rather than consulting git.  The
# definition of done is also run from a git-archive extraction, where there is
# no .git directory, and the CI checkout is the published tree being guarded.

set -u

if [ "$#" -gt 1 ]; then
  echo "usage: $0 [published-tree]" >&2
  exit 2
fi

tree=${1:-.}
if [ ! -d "$tree" ]; then
  echo "artifact-containment: not a directory: $tree" >&2
  exit 2
fi

tree=$(CDPATH= cd -- "$tree" 2>/dev/null && pwd -P) || {
  echo "artifact-containment: cannot resolve directory: ${1:-.}" >&2
  exit 2
}

status=0
for artifact_dir in lessons digests measurements guards; do
  artifact_path="$tree/$artifact_dir"

  # A file or symlink at the artifact root is a published artifact too.
  if [ -f "$artifact_path" ] || [ -L "$artifact_path" ]; then
    printf 'artifact-containment: forbidden published path: %s\n' \
      "$artifact_dir" >&2
    status=1
    continue
  fi
  [ -d "$artifact_path" ] || continue

  # Do not follow symlinks: the link itself is already enough to fail the
  # assertion, and following it could make the check escape the checkout.
  while IFS= read -r candidate; do
    [ -n "$candidate" ] || continue
    printf 'artifact-containment: forbidden published path: %s\n' \
      "${candidate#"$tree/"}" >&2
    status=1
  done <<EOF
$(find "$artifact_path" \( -type f -o -type l \) -print)
EOF
done

if [ "$status" -ne 0 ]; then
  echo "artifact-containment: published tree contains private artifacts" >&2
fi
exit "$status"
