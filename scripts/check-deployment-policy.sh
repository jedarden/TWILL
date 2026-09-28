#!/bin/sh
# Enforce the deployment constraints documented in docs/plan/plan.md.
#
# This checker intentionally uses only POSIX tools.  It is run against both a
# working checkout and a git-archive extraction, so it must not depend on git,
# an installed YAML parser, or untracked files.

set -u

if [ "$#" -gt 1 ]; then
  echo "usage: $0 [tree]" >&2
  exit 2
fi

tree=${1:-.}
if [ ! -d "$tree" ]; then
  echo "deployment-policy: not a directory: $tree" >&2
  exit 2
fi

tree=$(CDPATH= cd -- "$tree" 2>/dev/null && pwd -P) || {
  echo "deployment-policy: cannot resolve directory: ${1:-.}" >&2
  exit 2
}

status=0
forbidden_tag=latest

violation() {
  printf 'deployment-policy: %s\n' "$1" >&2
  status=1
}

# These are deliberately invalid test inputs.  They are run directly by
# tests/test_deployment_policy.py, but are not deployable repository content.
is_policy_fixture() {
  case "$1" in
    tests/fixtures/deployment-policy/*) return 0 ;;
    *) return 1 ;;
  esac
}

relative_path() {
  case "$1" in
    "$tree"/*) printf '%s\n' "${1#"$tree/"}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

# A GitHub Actions workflow is forbidden even if the directory is empty: the
# path itself is an attempt to introduce that CI surface.
while IFS= read -r workflow_dir; do
  [ -n "$workflow_dir" ] || continue
  relative=$(relative_path "$workflow_dir")
  is_policy_fixture "$relative" && continue
  violation "GitHub Actions workflow path is forbidden: $relative"
done <<EOF
$(find "$tree" \( -type d -o -type l \) -path '*/.github/workflows' -print)
EOF

# Deployment manifests are YAML.  Keep the resource check line-oriented so
# the gate remains usable from a clean archive without third-party packages.
while IFS= read -r manifest; do
  [ -n "$manifest" ] || continue
  relative=$(relative_path "$manifest")
  is_policy_fixture "$relative" && continue

  while IFS= read -r line; do
    [ -n "$line" ] || continue
    violation "$relative:$line: Job and CronJob resources are forbidden"
  done <<EOF
$(grep -nE "^[[:space:]-]*kind:[[:space:]]*[\"']?(Job|CronJob)[\"']?([[:space:]]*#.*)?$" "$manifest" 2>/dev/null || true)
EOF

  while IFS= read -r line; do
    [ -n "$line" ] || continue

    image=$(printf '%s\n' "$line" | sed -E "s/^[[:space:]-]*image:[[:space:]]*[\"']?([^\"'[:space:]#]+).*/\1/")
    [ -n "$image" ] || continue

    # A digest is an allowed immutable reference.  Inspect only the optional
    # tag before '@', so the digest's own ':' is not mistaken for a tag.
    image_without_digest=${image%%@*}
    image_leaf=${image_without_digest##*/}
    tag=
    case "$image_leaf" in
      *:*) tag=${image_leaf##*:} ;;
    esac

    case "$tag" in
      "$forbidden_tag")
        violation "$relative:$line: :$forbidden_tag image tags are forbidden"
        ;;
      "")
        ;;
      *)
        if printf '%s\n' "$tag" | grep -Eq '^[[:xdigit:]]{7,64}$'; then
          violation "$relative:$line: bare-SHA image tags are forbidden"
        fi
        ;;
    esac
  done <<EOF
$(grep -nE '^[[:space:]-]*image:[[:space:]]*[^[:space:]#]+' "$manifest" 2>/dev/null || true)
EOF
done <<EOF
$(find "$tree" -type f \( -name '*.yaml' -o -name '*.yml' \) -print)
EOF

if [ "$status" -ne 0 ]; then
  echo "deployment-policy: check failed" >&2
fi
exit "$status"
