#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
args=()
pull=true
for arg in "$@"; do
  case "$arg" in
    --no-pull) pull=false ;;
    --help|-h|--dry-run) pull=false; args+=("$arg") ;;
    *) args+=("$arg") ;;
  esac
done
if $pull; then
  if [[ -n "$(git status --porcelain)" ]]; then
    echo 'Helyi modositas van. Mentsd/commitold, vagy tudatos helyi teszthez hasznald a --no-pull opciot.' >&2
    exit 1
  fi
  git pull --ff-only
fi
exec python3 scripts/manage.py update "${args[@]}"
