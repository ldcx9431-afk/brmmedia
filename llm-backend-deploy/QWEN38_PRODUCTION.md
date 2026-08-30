# Qwen3.8-27B production record

## Active service

- Service: `qwen38-llama.service` (enabled at boot)
- Runtime: `llama.cpp` CUDA build, commit `9d77fa17254e1dee4b9e92504c91611a60b1359f`
- Model: `Qwen3.8-27B-Q4_K_M.gguf`
- API model id: `qwen38-27b-q4-k-m`
- API: the existing authenticated `/qwen/v1` route; the server itself listens only on `127.0.0.1:8001`.
- GPUs: the two A4000 cards only (`CUDA_VISIBLE_DEVICES=1,2`), with layer split and `--tensor-split 1,1`.
- Context / concurrency: 4096 tokens / one active request.

The A5000 remains dedicated to ComfyUI media work. The legacy Qwen3.5-4B vLLM service and model are retained as a disabled cold standby.

## Quantization decision

The requested Unsloth Dynamic V3 `UD-Q4_K_XL` artifact is present locally and was SHA-256 verified at revision `f1bfb127c64f7072bdd2cad55f258b9c8b2910fe`:

`bee238bbeb3dc0a34bde4d0dedbaee1f98c009e8bb4226f03070054c12fb1372`

It did not finish API-ready initialization with the tested CUDA `llama.cpp` runtime, so it is deliberately **not** the active production model. The standard Unsloth `Q4_K_M` artifact loaded correctly across both A4000 cards and passed production acceptance. Keep the UD file as a future candidate; do not substitute it into the service profile without a separate canary test.

## Verified acceptance

- Ten consecutive direct API conversations passed.
- `GET /v1/models` returns `qwen38-27b-q4-k-m`.
- Streaming `POST /v1/chat/completions` returns server-sent chunks.
- The authenticated Nginx `/qwen/v1` boundary remains in place.
- At idle, the Qwen service occupies both A4000 cards; the A5000 remains free for media generation.

## Operations

Check the active service inside WSL:

```powershell
wsl -d BRMMedia-Ubuntu -u root -- systemctl status qwen38-llama
wsl -d BRMMedia-Ubuntu -u root -- curl http://127.0.0.1:8001/v1/models
```

Switch back to the retained Qwen3.5-4B cold standby only if needed:

```powershell
wsl -d BRMMedia-Ubuntu -u root -- /srv/brmmedia/app/llm-backend-deploy/switch_qwen_model.sh qwen35
```

The switch script waits for the target health check and restores the prior active model if the new target fails. It intentionally does not contain or print access credentials.
