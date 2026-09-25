#!/usr/bin/env bash
# Build the pinned llama.cpp CUDA server inside WSL.  It is intentionally
# independent from ComfyUI and vLLM so the Qwen3.5 cold standby is untouched.
set -euo pipefail

TAG="${LLAMA_CPP_TAG:-b10236}"
PINNED_COMMIT="${LLAMA_CPP_COMMIT:-1464c62d88f699ec9700c8010bbfdbc603a9efd6}"
SRC="${QWEN38_LLAMA_SOURCE:-/srv/brmmedia/qwen38-llama-src}"
BUILD="${QWEN38_LLAMA_BUILD:-/srv/brmmedia/qwen38-llama-build}"
RUNTIME="${QWEN38_LLAMA_RUNTIME:-/srv/brmmedia/qwen38-llama}"
CUDA_COMPILER="${CUDACXX:-/usr/local/cuda/bin/nvcc}"

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 2; }
[ -x "$CUDA_COMPILER" ] || { echo "CUDA compiler missing: $CUDA_COMPILER" >&2; exit 2; }

if [ ! -d "$SRC/.git" ]; then
  # The deployment host uses an HTTP proxy that occasionally resets HTTP/2
  # streams. Force HTTP/1.1 and retry a shallow, pinned checkout instead of
  # accepting a partial source tree.
  rm -rf "$SRC"
  for attempt in 1 2 3 4 5; do
    if git -c http.version=HTTP/1.1 -c http.lowSpeedLimit=1 -c http.lowSpeedTime=120 \
      clone --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp.git "$SRC"; then
      break
    fi
    rm -rf "$SRC"
    [ "$attempt" = 5 ] && exit 1
    sleep $((attempt * 5))
  done
fi
actual_commit="$(git -C "$SRC" rev-parse HEAD)"
[ "$actual_commit" = "$PINNED_COMMIT" ] || {
  echo "Pinned llama.cpp checkout mismatch: expected $PINNED_COMMIT, got $actual_commit" >&2
  exit 2
}

# Source archives produced on macOS can contain AppleDouble metadata files.
# They are not source and CMake must never attempt to compile them as CUDA.
find "$SRC" -type f -name '._*' -delete

cmake -S "$SRC" -B "$BUILD" \
  -DGGML_CUDA=ON \
  -DCMAKE_CUDA_COMPILER="$CUDA_COMPILER" \
  -DCMAKE_CUDA_ARCHITECTURES=86 \
  -DGGML_NATIVE=OFF \
  -DLLAMA_CURL=OFF \
  -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILD" --target llama-server -j"$(nproc)"

install -d -m 0755 "$RUNTIME/bin"
install -m 0755 "$BUILD/bin/llama-server" "$RUNTIME/bin/llama-server"
"$RUNTIME/bin/llama-server" --version
printf '%s\n' "$actual_commit" > "$RUNTIME/llama.cpp.commit"
echo "[OK] llama.cpp $TAG ($actual_commit) built for CUDA sm86."
