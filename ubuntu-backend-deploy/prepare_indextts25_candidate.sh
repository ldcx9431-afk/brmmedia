#!/usr/bin/env bash
# Prepare (but do not route production traffic to) IndexTTS-2.5.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="${BRMMEDIA_APP_ROOT:-$SCRIPT_DIR}"
LOCK_FILE="${INDEXTTS25_LOCK_FILE:-$APP_ROOT/runtime-locks/indextts25-v2.5.env}"
[[ -f "$LOCK_FILE" ]] || { echo "[ERROR] Missing runtime lock: $LOCK_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$LOCK_FILE"
set +a

RUNTIME_ROOT="${INDEXTTS25_RUNTIME_ROOT:-$APP_ROOT/runtime-locks/indextts25-v2.5}"
SOURCE_ROOT="${INDEXTTS25_SOURCE_ROOT:-$RUNTIME_ROOT/source}"
VENV_ROOT="${INDEXTTS25_VENV:-$RUNTIME_ROOT/venv}"
MODEL_SOURCE_DIR="${INDEXTTS25_MODEL_SOURCE_DIR:-/mnt/d/model/IndexTTS-2.5}"
MODEL_DIR="${INDEXTTS25_MODEL_DIR:-$RUNTIME_ROOT/models/IndexTTS-2.5}"
ENV_FILE="${INDEXTTS25_ENV_FILE:-$APP_ROOT/.env.indextts25}"
REPORT_DIR="$RUNTIME_ROOT/reports"

UV_BIN="${INDEXTTS25_UV_BIN:-$(command -v uv || true)}"
[[ -x "$UV_BIN" ]] || { echo "[ERROR] uv is required to install the isolated Python 3.11 runtime" >&2; exit 1; }
command -v git >/dev/null || { echo "[ERROR] git is required" >&2; exit 1; }
NVIDIA_SMI="${BRMMEDIA_NVIDIA_SMI:-}"
if [[ -z "$NVIDIA_SMI" ]]; then
  for candidate in nvidia-smi /usr/lib/wsl/lib/nvidia-smi /usr/bin/nvidia-smi; do
    if [[ "$candidate" == */* ]] && [[ -x "$candidate" ]]; then NVIDIA_SMI="$candidate"; break; fi
    if command -v "$candidate" >/dev/null 2>&1; then NVIDIA_SMI="$(command -v "$candidate")"; break; fi
  done
fi
[[ -x "$NVIDIA_SMI" ]] || { echo "[ERROR] NVIDIA driver is unavailable" >&2; exit 1; }

gpu="$("$NVIDIA_SMI" --query-gpu=name,compute_cap,memory.total,driver_version --format=csv,noheader,nounits | sed -n '1p')"
echo "[INFO] GPU preflight: $gpu"
if ! grep -qi 'A5000' <<<"$gpu"; then
  echo "[ERROR] Candidate must be prepared against the A5000 media GPU." >&2
  exit 1
fi
available_kib="$(awk '/MemAvailable:/ {print $2}' /proc/meminfo)"
if (( available_kib < 64 * 1024 * 1024 )); then
  echo "[ERROR] At least 64 GiB available WSL memory is required; found $((available_kib / 1024 / 1024)) GiB." >&2
  exit 1
fi
mkdir -p "$RUNTIME_ROOT" "$REPORT_DIR" "$MODEL_SOURCE_DIR" "$MODEL_DIR"
if [[ "$(df --output=avail -B1 "$RUNTIME_ROOT" | tail -1 | tr -d ' ')" -lt $((30 * 1024 * 1024 * 1024)) ]]; then
  echo "[ERROR] Less than 30 GiB free on the E/NVMe runtime filesystem." >&2
  exit 1
fi

if [[ -f "$SOURCE_ROOT/.brmmedia-source-commit" ]] && [[ "$(<"$SOURCE_ROOT/.brmmedia-source-commit")" == "$INDEXTTS25_SOURCE_COMMIT" ]]; then
  # Air-gapped recovery can import an audited source archive.  The marker is
  # written only after its source commit was checked on the staging machine.
  echo "[INFO] Using verified imported IndexTTS-2.5 source archive."
elif [[ -d "$SOURCE_ROOT/.git" ]] && [[ "$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null || true)" != "$INDEXTTS25_SOURCE_COMMIT" ]]; then
  # Some WSL proxy paths reset HTTP/2 pack transfers; pin this tiny source
  # checkout to HTTP/1.1 and fetch only the required tag/commit.
  git -C "$SOURCE_ROOT" -c http.version=HTTP/1.1 fetch --depth 1 origin "$INDEXTTS25_SOURCE_COMMIT"
elif [[ ! -d "$SOURCE_ROOT/.git" ]]; then
  case "$SOURCE_ROOT" in
    "$RUNTIME_ROOT"/source) ;;
    *) echo "[ERROR] Refusing to replace an unexpected source directory: $SOURCE_ROOT" >&2; exit 1 ;;
  esac
  rm -rf "$SOURCE_ROOT"
  git -c http.version=HTTP/1.1 clone --depth 1 --branch "$INDEXTTS25_SOURCE_TAG" \
    "$INDEXTTS25_SOURCE_REPOSITORY" "$SOURCE_ROOT"
fi
if [[ -d "$SOURCE_ROOT/.git" ]]; then
  git -C "$SOURCE_ROOT" checkout --detach "$INDEXTTS25_SOURCE_COMMIT"
  [[ "$(git -C "$SOURCE_ROOT" rev-parse HEAD)" == "$INDEXTTS25_SOURCE_COMMIT" ]] || { echo "[ERROR] source revision mismatch" >&2; exit 1; }
else
  [[ "$(<"$SOURCE_ROOT/.brmmedia-source-commit")" == "$INDEXTTS25_SOURCE_COMMIT" ]] || { echo "[ERROR] imported source revision mismatch" >&2; exit 1; }
fi

PYTHON_SELECTOR="${INDEXTTS25_PYTHON_BIN:-$INDEXTTS25_PYTHON}"
if [[ "$PYTHON_SELECTOR" == */* ]]; then
  [[ -x "$PYTHON_SELECTOR" ]] || { echo "[ERROR] Requested Python binary is unavailable: $PYTHON_SELECTOR" >&2; exit 1; }
else
  "$UV_BIN" python install "$PYTHON_SELECTOR"
fi
"$UV_BIN" venv --clear --python "$PYTHON_SELECTOR" "$VENV_ROOT"
VENV_PYTHON="$VENV_ROOT/bin/python"

# Keep the upstream lock immutable in the provenance directory.  The official
# documentation also publishes the Aliyun mirror for unstable regions.  A
# candidate-local lock changes only the registry URL, not package versions or
# hashes, then remains `--locked` and checksum-verifiable.
UPSTREAM_LOCK_SHA256="2bcec9c6bd4d20d733bdc0537a2f52b39fd67a2e88ad2308836f56fd72da1283"
MIRROR_LOCK_SHA256="3bfe618597eac0eee4aeb921f987adb9bb9583ec008aded85c92ff64050fd9b9"
LOCK_FILE="$SOURCE_ROOT/uv.lock"
PROVENANCE_DIR="$RUNTIME_ROOT/provenance"
install -d -m 0750 "$PROVENANCE_DIR"
if [[ "$(sha256sum "$LOCK_FILE" | awk '{print $1}')" == "$UPSTREAM_LOCK_SHA256" ]]; then
  cp -f "$LOCK_FILE" "$PROVENANCE_DIR/uv.lock.upstream"
  sed -i 's#https://pypi.org/simple#https://mirrors.aliyun.com/pypi/simple#g' "$LOCK_FILE"
fi
[[ "$(sha256sum "$LOCK_FILE" | awk '{print $1}')" == "$MIRROR_LOCK_SHA256" ]] || fail "unexpected IndexTTS candidate lockfile"

# `unidic-lite` is a 47 MiB source distribution needed for Japanese support.
# Keep a checksum-verified copy in the isolated candidate cache so an
# unreliable PyPI connection cannot repeatedly corrupt its archive mid-build.
ARTIFACT_DIR="$RUNTIME_ROOT/artifacts"
TORCH_ARCHIVE="$ARTIFACT_DIR/torch-2.8.0+cu128-cp311-cp311-manylinux_2_28_x86_64.whl"
TORCH_SHA256="039b9dcdd6bdbaa10a8a5cd6be22c4cb3e3589a341e5f904cbb571ca28f55bed"
UNIDIC_ARCHIVE="$ARTIFACT_DIR/unidic-lite-1.0.8.tar.gz"
UNIDIC_SHA256="db9d4572d9fdd4d00a97949d4b0741ec480ee05a7e7e2e32f547500dae27b245"
install -d -m 0750 "$ARTIFACT_DIR"
[[ -f "$TORCH_ARCHIVE" ]] || fail "missing verified PyTorch CUDA artifact: $TORCH_ARCHIVE"
[[ "$(sha256sum "$TORCH_ARCHIVE" | awk '{print $1}')" == "$TORCH_SHA256" ]] || fail "PyTorch CUDA artifact SHA-256 mismatch"
"$UV_BIN" pip install --python "$VENV_PYTHON" --no-deps "$TORCH_ARCHIVE"
if [[ ! -f "$UNIDIC_ARCHIVE" ]] || [[ "$(sha256sum "$UNIDIC_ARCHIVE" | awk '{print $1}')" != "$UNIDIC_SHA256" ]]; then
  rm -f "$UNIDIC_ARCHIVE"
  curl --fail --location --retry 12 --retry-all-errors --connect-timeout 30 \
    --output "$UNIDIC_ARCHIVE" \
    "https://mirrors.aliyun.com/pypi/packages/55/2b/8cf7514cb57d028abcef625afa847d60ff1ffbf0049c36b78faa7c35046f/unidic-lite-1.0.8.tar.gz"
fi
[[ "$(sha256sum "$UNIDIC_ARCHIVE" | awk '{print $1}')" == "$UNIDIC_SHA256" ]] || fail "unidic-lite artifact SHA-256 mismatch"
"$UV_BIN" pip install --python "$VENV_PYTHON" --no-deps "$UNIDIC_ARCHIVE"

# The official project's locked CUDA 12.8 Torch dependencies are resolved in
# this private venv.  Do not request the optional DeepSpeed extra.
(
  cd "$SOURCE_ROOT"
# The candidate mirror lock above matches this explicitly configured index.
UV_NO_MANAGED_PYTHON=1 UV_PROJECT_ENVIRONMENT="$VENV_ROOT" \
    "$UV_BIN" sync --locked --python "$VENV_PYTHON" \
    --default-index "https://mirrors.aliyun.com/pypi/simple"
)
"$UV_BIN" pip install --python "$VENV_PYTHON" "fastapi>=0.115,<1" "uvicorn[standard]>=0.30,<1" "python-multipart>=0.0.20,<1"

if [[ ! -f "$MODEL_SOURCE_DIR/config_v2_5.yaml" ]]; then
  echo "[INFO] Downloading fixed IndexTTS-2.5 model revision to D source copy..."
  "$VENV_ROOT/bin/hf" download "$INDEXTTS25_MODEL_REPOSITORY" \
    --revision "$INDEXTTS25_MODEL_REVISION" --local-dir "$MODEL_SOURCE_DIR"
fi
[[ -f "$MODEL_SOURCE_DIR/config_v2_5.yaml" ]] || { echo "[ERROR] Model source copy is incomplete" >&2; exit 1; }
echo "[INFO] Copying verified source model from D to E/NVMe runtime..."
rsync -a --checksum "$MODEL_SOURCE_DIR/" "$MODEL_DIR/"
[[ -f "$MODEL_DIR/config_v2_5.yaml" ]] || { echo "[ERROR] Runtime model copy is incomplete" >&2; exit 1; }

"$VENV_PYTHON" - "$MODEL_DIR" "$REPORT_DIR/model-manifest.json" <<'PY'
import hashlib, json, sys
from pathlib import Path
root, destination = map(Path, sys.argv[1:])
files = []
for path in sorted(p for p in root.rglob('*') if p.is_file()):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(block)
    files.append({'path': str(path.relative_to(root)), 'size_bytes': path.stat().st_size, 'sha256': digest.hexdigest()})
destination.write_text(json.dumps({'root': str(root), 'files': files}, ensure_ascii=False, indent=2) + '\n')
print(f'[INFO] Wrote {len(files)} model hashes to {destination}')
PY

printf '%s\n' \
  '# Generated by prepare_indextts25_candidate.sh; update only through this script.' \
  "INDEXTTS25_VENV=$VENV_ROOT" \
  "INDEXTTS25_PYTHON_BIN=$PYTHON_SELECTOR" \
  "INDEXTTS25_SOURCE_ROOT=$SOURCE_ROOT" \
  "INDEXTTS25_MODEL_DIR=$MODEL_DIR" \
  "INDEXTTS25_RUN_DIR=$RUNTIME_ROOT/run" \
  "INDEXTTS25_SOURCE_COMMIT=$INDEXTTS25_SOURCE_COMMIT" \
  "INDEXTTS25_MODEL_REVISION=$INDEXTTS25_MODEL_REVISION" \
  "INDEXTTS25_CUDA_VISIBLE_DEVICES=$INDEXTTS25_CUDA_VISIBLE_DEVICES" \
  "INDEXTTS25_USE_BF16=$INDEXTTS25_USE_BF16" \
  "INDEXTTS25_USE_CUDA_KERNEL=$INDEXTTS25_USE_CUDA_KERNEL" \
  "INDEXTTS25_HOST=$INDEXTTS25_HOST" \
  "INDEXTTS25_PORT=$INDEXTTS25_PORT" \
  "INDEXTTS25_MAX_TEXT_CHARS=$INDEXTTS25_MAX_TEXT_CHARS" \
  "INDEXTTS25_MAX_REFERENCE_BYTES=$INDEXTTS25_MAX_REFERENCE_BYTES" \
  >"$ENV_FILE"
chmod 600 "$ENV_FILE"
"$VENV_PYTHON" - <<'PY'
import importlib
for name in ('torch', 'indextts.infer_v2_5', 'fastapi', 'uvicorn'):
    importlib.import_module(name)
print('[INFO] Candidate imports succeeded')
PY
echo "[INFO] Candidate prepared.  It is not exposed to LAN and production routing is unchanged."
