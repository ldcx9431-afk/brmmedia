# Qwen3.8-27B production record

## Active service

- Service: `qwen38-llama.service` (enabled at boot)
- Runtime: `llama.cpp` CUDA build, commit `9d77fa17254e1dee4b9e92504c91611a60b1359f`
- Model: `Qwen3.8-27B-UD-Q4_K_M.gguf` (Unsloth)
- API model id: `qwen38-27b-ud-q4-k-m`
- API: the existing authenticated `/qwen/v1` route; the server itself listens only on `127.0.0.1:8001`.
- Modality: text and image input (enabled 2026-10-09). The matching `mmproj-F16.gguf` is loaded with `--no-mmproj-offload`; the vision encoder runs on CPU. Each image has a 1024 visual-token budget. Single-image color/OCR and text/image SSE passed acceptance; multi-image and video input are not yet acceptance-tested.
- GPUs: the two A4000 cards only (`CUDA_VISIBLE_DEVICES=1,2`), with layer split and `--tensor-split 1,1`.
- Context / concurrency: 140288 tokens / one active request, K/V cache `q8_0`. The 128K-input + 4K-output profile has been tested successfully.

The A5000 remains dedicated to ComfyUI media work. The legacy Qwen3.5-4B vLLM service and model are retained as a disabled cold standby.

## Quantization decision

The active file is the Unsloth Dynamic V3 `UD-Q4_K_M` artifact, SHA-256 verified at revision `4ca720788d1e01f1bff70c033e0d0028fd02e502` (`322e194ff79741c7baa497c240f677f54b201b0efab44ca8e50f122b39123482`).

The requested Unsloth Dynamic V3 `UD-Q4_K_XL` artifact is present locally and was SHA-256 verified at revision `f1bfb127c64f7072bdd2cad55f258b9c8b2910fe`:

`bee238bbeb3dc0a34bde4d0dedbaee1f98c009e8bb4226f03070054c12fb1372`

It did not finish API-ready initialization with the tested CUDA `llama.cpp` runtime, so it is deliberately **not** the active production model. The smaller Unsloth Dynamic V3 `UD-Q4_K_M` artifact loaded correctly across both A4000 cards and passed production acceptance. Keep the XL file as a future candidate; do not substitute it into the service profile without a separate canary test.

## Verified acceptance

- Ten consecutive direct API conversations passed.
- `GET /v1/models` returns `qwen38-27b-ud-q4-k-m`.
- Streaming `POST /v1/chat/completions` returns server-sent chunks.
- The authenticated Nginx `/qwen/v1` boundary remains in place.
- At idle, the Qwen service occupies both A4000 cards; the A5000 remains free for media generation.

## Operations

### Image component and verification

The projector comes from the same Unsloth revision as the active main weight: `4ca720788d1e01f1bff70c033e0d0028fd02e502`. File: `mmproj-F16.gguf`, size `927607488` bytes, SHA-256 `cbb841a9ee0636b2ec172f5bb8df2ea8dfeb01e90fe7c6126581d662a0b4e43e`. The launcher checks the configured SHA-256 at every start and refuses to start with a missing/corrupt component. The default main-weight-only profile is retained by leaving `QWEN38_MMPROJ` unset.

Download with `bash llm-backend-deploy/download_qwen38_mmproj.sh`, then set the four `QWEN38_MMPROJ*` / `QWEN38_IMAGE_MAX_TOKENS` settings from `.env.qwen38-27b-q4km.example`. Keep `QWEN38_MMPROJ_OFFLOAD=0` with the current long-context profile: the first A4000 has less than 1 GiB spare VRAM, while the matching projector alone is about 0.93 GB before activation buffers. Host RAM has ample room for CPU vision encoding. Changing GPU offload or concurrency requires a separate resource test.

After loading, run `python3 llm-backend-deploy/accept_qwen38_vision.py --gateway` inside WSL. It checks actual image color/OCR grounding, text responses and text/image SSE; the gateway option creates a one-day acceptance key and disables/deletes it in a `finally` block. It never prints the key. Applications keep `/qwen-api/v1` and `qwen38-27b-ud-q4-k-m`, supplying images as Base64 `image_url` parts. The existing Gradio Qwen chat page remains text-only; this deployment enables images for API integrations.

### 2026-10-09 deployment

Production changes are limited to the active launcher and its `.env.qwen38-27b`. Runtime, main GGUF, GPU selection, 140288 context, q8_0 cache, single concurrency, existing ports and gateway routes are retained. The media services were not restarted. The launcher/config and service/Nginx snippet were backed up before the change; the automated deployment restores the previous launcher/config if startup or acceptance fails, and restores the healthcheck timer on exit.

An initial acceptance script failed to handle a valid SSE finish frame whose `content` was null, after image tests had passed. Automatic rollback restored the healthy text profile. After fixing the acceptance parser, the second deployment completed successfully. Both the direct endpoint and Nginx/Bearer endpoint passed text, color, OCR and SSE checks; a separate client-side LAN test also passed image SSE. Temporary acceptance keys were physically removed.

A supplementary 1600x640 synthetic bitmap-font OCR probe returned `ERM42` instead of `BRM42`, with a normal successful API response and no crash. This is an observed recognition-quality limitation, not a promise of lossless OCR. The repeatable acceptance keeps exact OCR on the clear 400x160 fixture and checks 1600x640 image grounding/resource handling with a color fixture; business document OCR still needs representative-image review and checking of important values.

Client-side LAN acceptance of the 1600x640 color fixture passed in about 6.16 seconds (1028 total prompt tokens including the template/question); small fixtures took about 0.7-1.2 seconds. These are synthetic-smoke measurements, not general latency guarantees. Post-test GPU allocations were approximately 15536/11711 MiB on the two A4000 cards, and `/props` reported vision support, one slot and the original main weight. The 140288 configured context is retained; combined 128K-text-plus-image input was not retested in this change.

The retained original text-profile backup is `/srv/brmmedia/runtime-locks/qwen38-vision-20261009-132643`. To roll back, wait for an idle Qwen slot, restore `start_qwen38_llama.sh` and `.env.qwen38-27b` from that directory into `/srv/brmmedia/app/llm-backend-deploy/`, restart `qwen38-llama`, then check health/text responses. Keep the downloaded projector for later tests. Do not roll back to the disabled Qwen3.5 service merely to disable image input.

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
