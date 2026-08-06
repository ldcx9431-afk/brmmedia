#!/usr/bin/env bash
# Start H3 component downloads as transient systemd units.  Unlike an SSH
# background job, these continue through an SSH disconnect and are resumable.
set -euo pipefail

MODEL_SOURCE_ROOT="${BRMMEDIA_MODEL_SOURCE_ROOT:-/mnt/d/model}"
MODEL_DIR="$MODEL_SOURCE_ROOT/MiniMax-H3"
REPO="${BRMMEDIA_H3_REPO:-Comfy-Org/MiniMax-H3}"
REVISION="${BRMMEDIA_H3_REVISION:-0bd506d2e895983a9663037febda27aa3948cf48}"
# Keep the immutable revision and SHA-256 gate independent from transport so
# a LAN proxy or a verified mirror can be used without changing model identity.
H3_BASE_URL="${BRMMEDIA_H3_BASE_URL:-https://huggingface.co}"
H3_BASE_URL="${H3_BASE_URL%/}"
SERVICE_USER="${BRMMEDIA_SERVICE_USER:-brm}"
# The corporate proxy has been observed to close long Xet transfers early.
# Keep each request small enough to finish, then append only a header-checked
# byte range.  One MiB is deliberately conservative for the observed proxy:
# 8MiB Xet responses were frequently terminated early, while 1MiB ranges
# complete reliably and never require discarding a partial response.
CHUNK_BYTES="${BRMMEDIA_H3_CHUNK_BYTES:-1048576}"
MIN_CHUNK_BYTES="${BRMMEDIA_H3_MIN_CHUNK_BYTES:-1048576}"
# Long blind curl retries keep requesting the same overlarge range through a
# proxy that has already truncated it.  One short retry is enough for a
# transient reset; afterwards the validated-range loop shrinks that worker.
CURL_RETRIES="${BRMMEDIA_H3_CURL_RETRIES:-1}"
CURL_RETRY_DELAY="${BRMMEDIA_H3_CURL_RETRY_DELAY:-1}"
# Each batch is downloaded in parallel, but its verified ranges are appended
# strictly in offset order.  Default to one range for conservative deployments;
# a constrained LAN relay can opt into a small value after a no-write probe.
PARALLEL_RANGES="${BRMMEDIA_H3_PARALLEL_RANGES:-1}"

# `systemd-run` does not automatically inherit the stage service's process
# environment.  Propagate an explicitly configured proxy to short-lived
# workers so resumable downloads can use a LAN relay without changing the
# immutable source, revision, or checksum gates.
proxy_env_args=()
for proxy_env_name in HTTPS_PROXY HTTP_PROXY ALL_PROXY NO_PROXY; do
  proxy_env_value="${!proxy_env_name:-}"
  if [ -n "$proxy_env_value" ]; then
    proxy_env_args+=("--setenv=${proxy_env_name}=${proxy_env_value}")
  fi
done

declare -A components=(
  [diffusion]='diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors'
  [text]='text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors'
  [video]='vae/minimax_h3_video_vae_fp16.safetensors'
  [audio]='vae/minimax_h3_audio_vae_fp32.safetensors'
)

expected_size() {
  case "$1" in
    diffusion) echo 20970379616 ;;
    text) echo 15687142551 ;;
    video) echo 5207808496 ;;
    audio) echo 605254808 ;;
    *) return 1 ;;
  esac
}

expected_sha256() {
  case "$1" in
    diffusion) echo e889202c41dafb67b10d67b97f0d8541508036a6090af23425a5c2615d03c47a ;;
    text) echo 35a88d51044231fe332301d7a62aa81e3f2cba62febeb446e2c1e3e0ef76f2c6 ;;
    video) echo 7c1f131492e7eddacaac9069a61b81bdd39de5cc96561e677c5eab1cdce5e522 ;;
    audio) echo 8e505d95dd1561d47abd43d4238fd40d9bb1ae9e147ed0a4cba778d76ae4db48 ;;
    *) return 1 ;;
  esac
}

download_component() {
  local name="$1" relative file expected expected_hash actual actual_hash attempt end chunk effective_chunk
  local range_start range_end range_chunk index received content_range batch_valid pid
  local -a tmp_files=() header_files=() range_starts=() range_ends=() range_chunks=() worker_pids=()
  relative="${components[$name]:-}"
  [ -n "$relative" ] || { echo "[ERROR] Unknown component: $name" >&2; return 2; }
  file="$MODEL_DIR/$relative"
  expected="$(expected_size "$name")"
  expected_hash="$(expected_sha256 "$name")"
  install -d "$(dirname "$file")"
  if ! [[ "$CHUNK_BYTES" =~ ^[1-9][0-9]*$ ]] || ! [[ "$MIN_CHUNK_BYTES" =~ ^[1-9][0-9]*$ ]] || \
     ! [[ "$CURL_RETRIES" =~ ^[0-9]+$ ]] || ! [[ "$CURL_RETRY_DELAY" =~ ^[0-9]+$ ]] || \
     ! [[ "$PARALLEL_RANGES" =~ ^[1-8]$ ]]; then
    echo "[ERROR] H3 chunk/retry settings must be valid and parallel ranges must be 1-8." >&2
    return 2
  fi
  effective_chunk="$CHUNK_BYTES"
  if [ "$effective_chunk" -lt "$MIN_CHUNK_BYTES" ]; then
    effective_chunk="$MIN_CHUNK_BYTES"
  fi

  # A `.chunk.*` file is never appended until the complete HTTP range and its
  # Content-Range header have been checked.  Remove leftovers from a killed
  # transient worker before resuming; this preserves the verified destination
  # prefix while preventing D: from accumulating abandoned temporary ranges.
  find "$(dirname "$file")" -maxdepth 1 -type f -name "$(basename "$file").chunk.*" -delete
  cleanup_chunks() {
    # EXIT can run after this function has returned, at which point Bash has
    # released its local arrays.  Do not let `set -u` turn a successful
    # checksum-verified worker into a failed unit during cleanup.
    if ! declare -p tmp_files >/dev/null 2>&1 || ! declare -p header_files >/dev/null 2>&1; then
      return 0
    fi
    if [ "${#tmp_files[@]}" -gt 0 ]; then
      rm -f -- "${tmp_files[@]}" "${header_files[@]}"
    fi
  }
  clear_cleanup_traps() {
    trap - EXIT INT TERM
  }
  trap cleanup_chunks EXIT
  trap 'cleanup_chunks; exit 130' INT
  trap 'cleanup_chunks; exit 143' TERM

  # Xet-backed Hugging Face redirects can occasionally close a long ranged
  # response early (curl 18).  Download immutable, bounded ranges and append
  # only after both length and Content-Range prove it is the requested segment.
  for attempt in $(seq 1 10000); do
    actual="$(stat -c%s "$file" 2>/dev/null || echo 0)"
    if [ "$actual" -eq "$expected" ]; then
      actual_hash="$(sha256sum "$file" | awk '{print $1}')"
      if [ "$actual_hash" = "$expected_hash" ]; then
        echo "[OK] $name complete and SHA-256 verified: $actual bytes"
        cleanup_chunks
        clear_cleanup_traps
        return 0
      fi
      echo "[WARN] $name has expected size but invalid SHA-256; resetting only this component for redownload." >&2
      truncate -s 0 "$file"
      continue
    fi
    if [ "$actual" -gt "$expected" ]; then
      echo "[ERROR] $name exceeds expected byte count ($actual > $expected)" >&2
      cleanup_chunks
      clear_cleanup_traps
      return 1
    fi
    tmp_files=()
    header_files=()
    range_starts=()
    range_ends=()
    range_chunks=()
    worker_pids=()
    for index in $(seq 0 $((PARALLEL_RANGES - 1))); do
      range_start=$((actual + index * effective_chunk))
      [ "$range_start" -lt "$expected" ] || break
      range_chunk="$effective_chunk"
      if [ $((expected - range_start)) -lt "$range_chunk" ]; then
        range_chunk=$((expected - range_start))
      fi
      range_end=$((range_start + range_chunk - 1))
      tmp_files+=("${file}.chunk.$$.${index}")
      header_files+=("${file}.chunk.$$.${index}.headers")
      range_starts+=("$range_start")
      range_ends+=("$range_end")
      range_chunks+=("$range_chunk")
      rm -f "${tmp_files[$index]}" "${header_files[$index]}"
      echo "[INFO] $name attempt=$attempt range=$range_start-$range_end/$expected batch=$((index + 1))/${PARALLEL_RANGES}"
      (
        curl --silent --show-error --fail --location --range "$range_start-$range_end" \
          --dump-header "${header_files[$index]}" \
          --retry "$CURL_RETRIES" --retry-delay "$CURL_RETRY_DELAY" --retry-all-errors --connect-timeout 30 \
          --speed-time 90 --speed-limit 1024 --output "${tmp_files[$index]}" \
          "$H3_BASE_URL/$REPO/resolve/$REVISION/$relative" || true
      ) &
      worker_pids+=("$!")
    done
    for pid in "${worker_pids[@]}"; do
      wait "$pid" || true
    done
    batch_valid=1
    for index in "${!tmp_files[@]}"; do
      received="$(stat -c%s "${tmp_files[$index]}" 2>/dev/null || echo 0)"
      content_range="$(awk 'BEGIN{IGNORECASE=1} /^content-range:/ {line=$0} END {gsub(/\r/, "", line); print line}' "${header_files[$index]}" 2>/dev/null || true)"
      if [ "$received" -ne "${range_chunks[$index]}" ] || ! printf '%s\n' "$content_range" | grep -qi "^content-range: bytes ${range_starts[$index]}-${range_ends[$index]}/$expected$"; then
        echo "[WARN] $name rejected incomplete/unexpected segment: received=$received expected=${range_chunks[$index]} range=${content_range:-missing}" >&2
        batch_valid=0
      fi
    done
    if [ "$batch_valid" -eq 1 ]; then
      for index in "${!tmp_files[@]}"; do
        dd if="${tmp_files[$index]}" of="$file" oflag=append conv=notrunc status=none
      done
      cleanup_chunks
      tmp_files=()
      header_files=()
      continue
    fi
    cleanup_chunks
    tmp_files=()
    header_files=()
    # A proxy can tolerate normal 8MiB transfers yet cut a particular large
    # Xet response.  Preserve the verified prefix and shrink only future
    # requests for this worker instead of wasting retries on the same range.
    if [ "$effective_chunk" -gt "$MIN_CHUNK_BYTES" ]; then
      effective_chunk=$((effective_chunk / 2))
      if [ "$effective_chunk" -lt "$MIN_CHUNK_BYTES" ]; then
        effective_chunk="$MIN_CHUNK_BYTES"
      fi
      echo "[INFO] $name will retry with ${effective_chunk}-byte segments." >&2
    fi
    sleep 5
  done
  echo "[ERROR] $name did not complete after repeated resumable attempts" >&2
  cleanup_chunks
  clear_cleanup_traps
  return 1
}

show_status() {
  local name relative file expected actual complete_note
  for name in diffusion text video audio; do
    relative="${components[$name]}"
    file="$MODEL_DIR/$relative"
    expected="$(expected_size "$name")"
    actual="$(stat -c%s "$file" 2>/dev/null || echo 0)"
    complete_note=""
    if [ "$actual" -eq "$expected" ]; then
      complete_note=" (size complete; SHA-256 is verified by the worker/import gate)"
    fi
    printf '%-10s %-12s %s/%s bytes  %s%s\n' "$name" \
      "$(systemctl is-active "brmmedia-h3-download-$name" 2>/dev/null || true)" "$actual" "$expected" "$relative" "$complete_note"
  done
}

if [ "${1:-}" = "--worker" ]; then
  download_component "${2:?usage: $0 --worker <component>}"
  exit $?
fi
if [ "${1:-}" = "--status" ]; then
  show_status
  exit 0
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "[ERROR] Run with sudo: sudo $0 [--status]" >&2
  exit 1
fi
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  echo "[ERROR] Service user does not exist: $SERVICE_USER" >&2
  exit 1
fi
command -v curl >/dev/null 2>&1 || { echo "[ERROR] curl is required" >&2; exit 1; }
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$MODEL_DIR"

for name in diffusion text video audio; do
  relative="${components[$name]}"
  file="$MODEL_DIR/$relative"
  expected="$(expected_size "$name")"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" "$(dirname "$file")"
  actual="$(stat -c%s "$file" 2>/dev/null || echo 0)"
  if [ "$actual" -eq "$expected" ]; then
    # Start a short-lived worker even for a size-complete file.  It performs
    # the immutable LFS SHA-256 gate before declaring this component reusable.
    if systemctl is-active --quiet "brmmedia-h3-download-$name"; then
      echo "[INFO] $name integrity verification is already active."
      continue
    fi
    systemd-run --unit="brmmedia-h3-download-$name" --collect \
      --property="User=$SERVICE_USER" --property=Nice=10 --property="WorkingDirectory=$MODEL_DIR" \
      --setenv="BRMMEDIA_H3_CHUNK_BYTES=$CHUNK_BYTES" \
      --setenv="BRMMEDIA_H3_MIN_CHUNK_BYTES=$MIN_CHUNK_BYTES" \
      --setenv="BRMMEDIA_H3_CURL_RETRIES=$CURL_RETRIES" \
      --setenv="BRMMEDIA_H3_CURL_RETRY_DELAY=$CURL_RETRY_DELAY" \
      --setenv="BRMMEDIA_H3_PARALLEL_RANGES=$PARALLEL_RANGES" \
      --setenv="BRMMEDIA_H3_BASE_URL=$H3_BASE_URL" \
      "${proxy_env_args[@]}" \
      /usr/bin/env bash "$(readlink -f "$0")" --worker "$name"
    continue
  fi
  if [ "$actual" -gt "$expected" ]; then
    echo "[ERROR] $name exceeds its expected byte count ($actual > $expected)." >&2
    exit 1
  fi
  if systemctl is-active --quiet "brmmedia-h3-download-$name"; then
    echo "[INFO] $name download is already active."
    continue
  fi
  systemd-run --unit="brmmedia-h3-download-$name" --collect \
    --property="User=$SERVICE_USER" --property=Nice=10 --property="WorkingDirectory=$MODEL_DIR" \
    --setenv="BRMMEDIA_H3_CHUNK_BYTES=$CHUNK_BYTES" \
    --setenv="BRMMEDIA_H3_MIN_CHUNK_BYTES=$MIN_CHUNK_BYTES" \
    --setenv="BRMMEDIA_H3_CURL_RETRIES=$CURL_RETRIES" \
    --setenv="BRMMEDIA_H3_CURL_RETRY_DELAY=$CURL_RETRY_DELAY" \
    --setenv="BRMMEDIA_H3_PARALLEL_RANGES=$PARALLEL_RANGES" \
    --setenv="BRMMEDIA_H3_BASE_URL=$H3_BASE_URL" \
    "${proxy_env_args[@]}" \
    /usr/bin/env bash "$(readlink -f "$0")" --worker "$name"
done
show_status
