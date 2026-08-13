#!/usr/bin/env bash
# Configure one reproducible ComfyUI v0.32 H3 canary benchmark cell.
# This script only edits the private canary env.  It never changes production
# .env, service units, workflows or model files.
set -euo pipefail

APP_ROOT="${BRMMEDIA_APP_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
CANARY_ENV="${BRMMEDIA_H3_CANARY_ENV:-$APP_ROOT/runtime-locks/h3-v032-canary.env}"
LOCK_FILE="${BRMMEDIA_H3_RUNTIME_LOCK:-$APP_ROOT/runtime-locks/h3-comfyui-v0.32.0.env}"
BACKEND_UNIT="${BRMMEDIA_H3_CANARY_BACKEND_UNIT:-baorong-backend-h3-canary}"
API_UNIT="${BRMMEDIA_H3_CANARY_API_UNIT:-brmmedia-lan-api-h3-canary}"

usage() {
  cat <<'EOF'
Usage: sudo ./configure_h3_canary_ab.sh --attention workflow-sage|kitchen
       [--fast-disk on|off] [--cache default|lru1]

  workflow-sage  Keep COMFYUI_ARGS free of a global attention flag. The
                 private workflows must contain the KJNodes Sage patch.
  kitchen        Add ComfyUI v0.32 --use-ck-attention. The private workflows
                 must not contain a workflow-level Sage patch.

Each cell is loopback-canary-only. Stop the canary before reconfiguring it.
EOF
}

die() { echo "[ERROR] $*" >&2; exit 1; }
set_env() {
  local key="$1" value="$2" escaped tmp
  escaped="$(printf '%s' "$value" | sed 's/[&|]/\\&/g')"
  tmp="${CANARY_ENV}.tmp.$$"
  awk -v key="$key" -v value="$escaped" '
    BEGIN { replaced=0 }
    $0 ~ "^" key "=" { if (!replaced) print key "=\"" value "\""; replaced=1; next }
    { print }
    END { if (!replaced) print key "=\"" value "\"" }
  ' "$CANARY_ENV" > "$tmp"
  chmod 0600 "$tmp"
  if stat -c '%u:%g' "$CANARY_ENV" >/dev/null 2>&1; then
    chown "$(stat -c '%u:%g' "$CANARY_ENV")" "$tmp"
  else
    chown "$(stat -f '%u:%g' "$CANARY_ENV")" "$tmp"
  fi
  mv "$tmp" "$CANARY_ENV"
}
env_value() {
  sed -n "s/^$1=//p" "$CANARY_ENV" | tail -n1 | sed -e 's/^"//' -e 's/"$//'
}

attention=""
fast_disk="off"
cache="default"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --attention) [ "$#" -ge 2 ] || die "--attention requires a value"; attention="$2"; shift 2 ;;
    --fast-disk) [ "$#" -ge 2 ] || die "--fast-disk requires on or off"; fast_disk="$2"; shift 2 ;;
    --cache) [ "$#" -ge 2 ] || die "--cache requires default or lru1"; cache="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown argument: $1" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "Run with sudo: sudo $0"
[ -f "$CANARY_ENV" ] || die "Canary env is missing: $CANARY_ENV"
[ -f "$LOCK_FILE" ] || die "Runtime lock is missing: $LOCK_FILE"
if systemctl is-active --quiet "$BACKEND_UNIT" || systemctl is-active --quiet "$API_UNIT"; then
  die "Stop the H3 canary before changing its benchmark cell."
fi

case "$attention" in workflow-sage|kitchen) ;; *) die "--attention must be workflow-sage or kitchen" ;; esac
case "$fast_disk" in on|off) ;; *) die "--fast-disk must be on or off" ;; esac
case "$cache" in default|lru1) ;; *) die "--cache must be default or lru1" ;; esac

# shellcheck disable=SC1090
source "$LOCK_FILE"
workflow_dir="$(env_value BRMMEDIA_WORKFLOW_DIR)"
[ -d "$workflow_dir" ] || die "Private workflow snapshot is missing: $workflow_dir"

sage_marker='PathchSageAttentionKJ|PatchSageAttentionKJ|SageAttentionKJ'
if [ "$attention" = "workflow-sage" ]; then
  grep -ERq --include='MiniMaxH3-*.json' "$sage_marker" "$workflow_dir" || \
    die "workflow-sage requires a Sage patch in the private H3 workflows."
else
  if grep -ERq --include='MiniMaxH3-*.json' "$sage_marker" "$workflow_dir"; then
    die "Kitchen Attention cannot be combined with a workflow-level Sage patch. Prepare a separate no-Sage private workflow snapshot first."
  fi
fi

args=()
[ "$attention" = "kitchen" ] && args+=(--use-ck-attention)
[ "$fast_disk" = "on" ] && args+=(--fast-disk)
[ "$cache" = "lru1" ] && args+=(--cache-lru 1)
args_text="${args[*]:-}"

# Refuse unsafe or ambiguous tokens even though this script constructs the
# list itself.  This also protects future edits to the matrix.
case " $args_text " in
  *" --highvram "*|*" --gpu-only "*|*" --lowvram "*|*" --novram "*|*" --disable-smart-memory "*|*" --cache-none "*|*" --disable-dynamic-vram "*|*" --disable-async-offload "*)
    die "Generated benchmark cell contains a forbidden H3 runtime argument: $args_text" ;;
esac

set_env COMFYUI_ARGS "$args_text"
set_env BRMMEDIA_H3_AB_ATTENTION "$attention"
set_env BRMMEDIA_H3_AB_FAST_DISK "$fast_disk"
set_env BRMMEDIA_H3_AB_CACHE "$cache"
set_env BRMMEDIA_H3_COMFYUI_VERSION "$BRMMEDIA_H3_COMFYUI_VERSION"
set_env BRMMEDIA_H3_COMFYUI_REF "$BRMMEDIA_H3_COMFYUI_REF"
set_env BRMMEDIA_H3_AB_CELL "${attention}-fastdisk_${fast_disk}-cache_${cache}"

echo "[OK] Configured private H3 v0.32 canary cell: ${attention}-fastdisk_${fast_disk}-cache_${cache}"
echo "     COMFYUI_ARGS=$args_text"
echo "     Start only with $APP_ROOT/start_minimax_h3_canary.sh"
