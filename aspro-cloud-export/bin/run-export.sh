#!/bin/bash
set -euo pipefail
set +x
umask 077
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
key_file="${ASPRO_KEY_FILE:-$HOME/.config/aspro-export/api-key}"
if [[ ! -f "$key_file" || "$(stat -c '%a' "$key_file")" != "600" || ! -O "$key_file" ]]; then
    printf 'Key file must be owned by this user and have permissions 600.\n' >&2
    exit 1
fi
export ASPRO_API_KEY
ASPRO_API_KEY="$(cat -- "$key_file")"
labels_file="${ASPRO_USER_LABELS_FILE:-$HOME/.config/aspro-export/user-labels.json}"
if [[ -f "$labels_file" ]]; then
    export ASPRO_USER_LABELS_FILE="$labels_file"
fi
account_labels="${ASPRO_ACCOUNT_LABELS_FILE:-$HOME/.config/aspro-export/account-labels.json}"
if [[ -f "$account_labels" ]]; then
    export ASPRO_ACCOUNT_LABELS_FILE="$account_labels"
fi
: "${ASPRO_BASE_URL:?Set ASPRO_BASE_URL to the account HTTPS origin}"
cd -- "$project_dir"
exec "$project_dir/.venv/bin/python" -m aspro_export "$@"
