# BRMMedia 局域网 API

> **更新日期：2026-08-13**
> 推荐业务系统调用稳定 REST API v1。原有 Gradio API 保留给既有脚本与工作台调试，不建议新业务继续依赖它的 SSE 协议。

## 1. 入口、认证与快速检查

| 项目 | 规则 |
| --- | --- |
| 局域网入口 | `http://<Windows-LAN-IP>` |
| REST API 根路径 | `http://<Windows-LAN-IP>/api/v1` |
| OpenAPI | `GET /api/v1/openapi.json`；交互文档 `GET /api/v1/docs` |
| 认证 | Nginx HTTP Basic Auth；账号密码只保存在私有 `DEPLOYMENT.md` 或密码管理器，不进入 Git、代码与命令历史 |
| 内部服务 | Gradio `9000`、ComfyUI `8188`、LAN API `9100`、IndexTTS‑2.5 `9205`、Qwen `8000` 都只监听 WSL 回环地址，不能直接从局域网访问 |

Windows 使用 DHCP 时，LAN IP 可能变化。请以服务器物理网卡的 `ipconfig` IPv4 为准，或在路由器中为其设置 DHCP 固定租约；不要在客户端代码中写死旧地址。

```bash
export BRM_BASE='http://<Windows-LAN-IP>'
export BRM_API="$BRM_BASE/api/v1"
export BRM_USER='brmadmin'
# 不要把密码写入 shell 历史。
read -rs BRM_PASSWORD; echo

curl --fail --user "$BRM_USER:$BRM_PASSWORD" "$BRM_API/health"
curl --fail --user "$BRM_USER:$BRM_PASSWORD" "$BRM_API/capabilities"
```

`health` 返回 `200` 且 `status: ok` 说明 Gradio 与 ComfyUI 可用；依赖未就绪时返回 `503` 和 `degraded`。`capabilities` 给出本次部署支持的工作流、尺寸、素材字段、上传上限与素材有效期；H3 视频工作流还会在 `workflows.<workflow>.options.params` 返回字段类型、必填项、默认值和枚举。完整 HTTP 请求/响应结构以 OpenAPI 为准。

## 2. 推荐调用闭环

```mermaid
sequenceDiagram
    participant Client as 局域网业务系统
    participant API as /api/v1
    participant Queue as BRMMedia 工作台队列
    Client->>API: POST /files（需要素材时）
    API-->>Client: asset_id
    Client->>API: POST /tasks（workflow + params + asset_id）
    API->>Queue: 内部提交
    API-->>Client: 202 + task_id
    loop 轮询
        Client->>API: GET /tasks/{task_id}
        API-->>Client: queued/running/completed
    end
    Client->>API: GET artifacts/{filename}
    API-->>Client: 文件下载
```

- `asset_id` 是上传素材的短期引用，默认保留 7 天；过期后重新上传即可。
- `POST /tasks` 返回 `202 Accepted` 仅表示进入工作台队列，**不表示生成完成**。
- `GET /tasks/{task_id}` 的 `state` 为 `completed` 后才会出现 `artifacts`；使用其中的 `download_url` 下载。服务器绝对路径不会出现在任一响应中。
- 不提供外部“中断当前任务”接口：ComfyUI 中断是全局动作，可能误伤其他调用方，必须由工作台操作员确认。

## 3. 上传素材

`POST /files?kind=image` 或 `POST /files?kind=audio`，请求为 `multipart/form-data`，文件字段名固定为 `file`。

支持图片 `png/jpg/jpeg/webp/bmp/gif`，音频 `mp3/wav/flac/m4a/aac/ogg`。上传上限默认 256 MiB，以 `/capabilities` 的 `max_upload_bytes` 为准。服务会把素材安全导入 ComfyUI 输入区，调用者不需要、也不能提供服务器路径。

```bash
IMAGE_JSON=$(curl --fail --user "$BRM_USER:$BRM_PASSWORD" \
  -F 'file=@./reference.png' \
  "$BRM_API/files?kind=image")

IMAGE_ASSET_ID=$(printf '%s' "$IMAGE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["asset_id"])')
```

音频上传响应额外包含 `duration`，供数字人工作流直接使用：

```json
{
  "asset_id": "aabbcc...",
  "kind": "audio",
  "source_filename": "voice.wav",
  "size_bytes": 483920,
  "sha256": "…",
  "duration": 8.742,
  "expires_at": 1780000000.0
}
```

## 4. 提交任务

`POST /tasks`，JSON 请求体：

```json
{
  "workflow": "text-to-image",
  "params": {
    "prompt": "清晨薄雾中的湖畔木屋，电影感",
    "size": "1024 × 1024",
    "batch": 1
  }
}
```

```bash
curl --fail --user "$BRM_USER:$BRM_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"workflow":"text-to-image","params":{"prompt":"清晨薄雾中的湖畔木屋，电影感","size":"1024 × 1024","batch":1}}' \
  "$BRM_API/tasks"
```

成功返回 `202`：

```json
{
  "task_id": "0123456789abcdef0123456789abcdef",
  "workflow": "text-to-image",
  "state": "accepted",
  "queue_position": 1,
  "links": {"status": "/api/v1/tasks/0123456789abcdef0123456789abcdef"}
}
```

### 工作流与参数

| workflow | 必填参数 | 可选参数/素材 |
| --- | --- | --- |
| `text-to-image` | `prompt` | `size`（默认 `1024 × 1024`）、`batch`（1–4） |
| `image-edit` | `prompt`、`image_asset_id` | 图片编辑 |
| `text-to-video` | `prompt` | MiniMax H3；`size` 表示画幅比例、`seconds`、`profile`（`draft` / `preview` 默认 / `quality`）、`acceleration`（`standard` 默认 / `turbo_balanced` / `turbo_fast`） |
| `image-to-video` | `prompt`、`image_asset_id` | MiniMax H3；其余视频参数同文生视频 |
| `first-last-frame-video` | `prompt`、`first_image_asset_id`、`last_image_asset_id` | `seconds`（2–360） |
| `talking-head` | `prompt`、`image_asset_id`、`audio_asset_id`、`duration` | `size`（默认 `768 × 1024`）；应使用上传音频返回的 `duration` |
| `voice-clone` | `prompt`、`ref_audio_asset_id` | IndexTTS‑2.5：`language`（`zh/en/ja/es/ar`，默认 `zh`）、`speed`（0.5–2.0，默认 1.0）；旧 `temperature` 仍接受但已废弃且不影响 2.5 推理 |
| `music-generate` | `tags` | `lyrics`、`duration`（1–600）、`bpm`（30–300）、`language`、`model`（仅限 `/capabilities` 当前列出的已安装权重） |

例如，图片编辑使用上传得到的引用：

```bash
curl --fail --user "$BRM_USER:$BRM_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d "{\"workflow\":\"image-edit\",\"params\":{\"prompt\":\"改成黄昏暖色调\",\"image_asset_id\":\"$IMAGE_ASSET_ID\"}}" \
  "$BRM_API/tasks"
```

非法参数、类型不匹配的素材引用和过期素材均返回 `422`；工作台或 ComfyUI 不可用返回 `503`；文件上传到 ComfyUI 失败返回 `502`。

### IndexTTS‑2.5 语音克隆规则

当部署配置为 `BRMMEDIA_VOICE_ENGINE=indextts25` 时，`voice-clone` 由独立 Python 3.11 / CUDA 服务在 `127.0.0.1:9205` 合成，仍通过全局单媒体队列占用 A5000，因此不会与 H3、图片或音乐任务争抢显存。服务原生返回 **22.05 kHz WAV**，工作台和 REST 任务产物统一保存为可试听、可下载的 **MP3**。候选验收期间 `/capabilities` 会诚实显示仍在使用的 `IndexTTS-2（回退）`，不会把未切换的 2.5 伪装成现网能力。

- `language` 只接受 `zh`、`en`、`ja`、`es`、`ar`；请让待合成文本与该值一致。
- `speed` 是原生语速，范围 `0.5–2.0`，`1.0` 为正常速度。
- `temperature` 仅为旧 IndexTTS‑2 调用兼容而保留。2.5 接收它但明确忽略，客户端应迁移到 `language` 与 `speed`。
- 本轮不启用情绪文本、情绪向量或额外情绪参考音频，避免引入 QwenEmotion 模型及新的显存竞争。

```bash
AUDIO_JSON=$(curl --fail --user "$BRM_USER:$BRM_PASSWORD" -F 'file=@./speaker.wav' "$BRM_API/files?kind=audio")
REF_ID=$(printf '%s' "$AUDIO_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["asset_id"])')

curl --fail --user "$BRM_USER:$BRM_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d "{\"workflow\":\"voice-clone\",\"params\":{\"prompt\":\"你好，这是 IndexTTS 二点五语音克隆测试。\",\"ref_audio_asset_id\":\"$REF_ID\",\"language\":\"zh\",\"speed\":1.0}}" \
  "$BRM_API/tasks"
```

### MiniMax H3 视频规则

`text-to-video` 与 `image-to-video` 已保留原 REST workflow 标识，调用方不需要迁移路径；实际引擎为本地开源 **MiniMax H3 Base**，输出是带模型原生同步立体声音频的 MP4。

- `profile=draft` 是官方模板同规格的极速草稿：约 `0.4MP`、`73` 帧、约 `3` 秒，仅用于提示词、构图与运动预览；它只接受 `seconds=3`。
- `profile=preview`（默认）使用约 `480` 像素短边，省略时为 `5` 秒；`quality` 使用约 `768` 像素短边，省略时为 `6` 秒。所有加速模式均允许 `4–15` 秒，最长边不超过 `1344`；结果时长仍以实际帧网格为准。
- `acceleration=standard` 是原官方 20 步 `res_multistep` 质量与回退路径，不加载 LoRA。
- `acceleration=turbo_balanced` 使用 LightX2V/ModelTC v1.0 8 步 LoRA、Euler、Sigma `12/3`，支持各档位与画幅。
- `acceleration=turbo_fast` 使用 LightX2V/ModelTC v1.0 4 步 768P LoRA、Euler、Sigma `6/3`。仅接受 `profile=quality` 与横向 `16:9`，在 `4–6` 秒按其训练规格实际生成 `1344 × 768`；`7–15` 秒会透明降级为兼容的 8 步 Turbo 路径。
- 15 秒 H3 会产生 `362` 帧。为适配 24GB A5000，长时请求若超过约 `0.786MP` 会自动下调到安全的 32 像素网格画布（例如 16:9 quality 由 `1344 × 768` 变为 `1152 × 640`）；Turbo 同时从 Sage 融合核切换到 `pytorch-stable` attention。任务的 `effective_settings` 会返回 `requested_acceleration`、实际 `acceleration`、`execution_policy`、`attention_backend` 和真实尺寸，调用方应以这些字段为准。
- 两个 Turbo 权重均固定 Hugging Face revision、文件大小与 SHA-256；启动门禁校验不通过时，候选拒绝启动。LightX2V 是第三方官方发布，并非 MiniMax 官方加速器，必须与 `standard` 做画面、运动、提示词遵循和原生音频 A/B 后再决定默认策略。
- `size` 只表达画幅比例；服务会计算模型可用的 32 像素网格画布。工作台只展示 `1:1`、`4:3`、`3:4`、`16:9`、`9:16` 五种画幅，不展示并不存在的 2K/4K H3 输出尺寸。图生视频会把上传图适配到该画布。
- H3 使用 24fps、`17k+5` 帧网格，实际帧数和时长可能略上调；在 `GET /tasks/{task_id}` 的 `effective_settings` 中读取真实 `width`、`height`、`frames` 和 `effective_seconds`。
- 业务系统应先读取 `GET /capabilities` 中 H3 workflow 的 `options.params` 构建表单。它明确列出 `profile` 枚举、`seconds.default_by_profile`、`size` 的 `enum_source` 和图生视频所需的 `image_asset_id`。

示例：

```bash
curl --fail --user "$BRM_USER:$BRM_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"workflow":"text-to-video","params":{"prompt":"雨后街道反射霓虹灯，电影感，环境声","size":"1920 × 1080","seconds":5,"profile":"preview","acceleration":"turbo_balanced"}}' \
  "$BRM_API/tasks"
```

## 5. 查询与下载

```bash
TASK_ID='<POST /tasks 返回的 task_id>'
curl --fail --user "$BRM_USER:$BRM_PASSWORD" "$BRM_API/tasks/$TASK_ID"
```

H3 示例完成状态（`1920 × 1080`、`preview`、请求 5 秒）如下。其他尺寸/时长必须以实际返回的 `effective_settings` 为准：

```json
{
  "task_id": "0123456789abcdef0123456789abcdef",
  "state": "completed",
  "status": "已完成",
  "finished_at": 1780000000.0,
  "error": null,
  "effective_settings": {
    "profile": "preview",
    "acceleration": "turbo_balanced",
    "engine": "MiniMax H3 + LightX2V Turbo v1.0 8-step",
    "steps": 8,
    "sampler": "euler",
    "requested_size": "1920 × 1080",
    "requested_seconds": 5,
    "width": 864,
    "height": 480,
    "frames": 124,
    "effective_seconds": 5.167
  },
  "artifacts": [
    {
      "name": "任务_MiniMaxH3文生视频_20260805-090000_1234.mp4",
      "download_url": "/api/v1/tasks/0123456789abcdef0123456789abcdef/artifacts/任务_MiniMaxH3文生视频_20260805-090000_1234.mp4"
    }
  ]
}
```

运行中的任务还会返回 ComfyUI 执行关联和进度。`prompt_id` 会在 ComfyUI
接受工作流后立即持久化；服务重启时会用它重新附着到原任务，不会重复提交生成：

```json
{
  "task_id": "0123456789abcdef0123456789abcdef",
  "state": "running",
  "prompt_id": "35ef78c7-772f-43aa-b82e-a7fa018259af",
  "execution": {
    "stage": "sampling",
    "node_id": "15",
    "current_step": 3,
    "total_steps": 8,
    "progress": 0.375,
    "last_progress_at": 1780000060.0,
    "recovered_after_restart": false
  }
}
```

`execution.stage` 可能依次出现 `queued`、`building_workflow`、`submitted`、
`execution_start`、`executing`、`sampling`、`finalizing`、`saving_artifacts`、
`completed`；异常终态为 `interrupted`、`timed_out` 或 `failed`。WebSocket
暂时不可用时后端自动回退到 `/history/{prompt_id}` 轮询，因此客户端只需继续
查询本 REST 接口。

可按 2–5 秒间隔轮询。`state` 的含义：

| state | 含义 |
| --- | --- |
| `queued` | 已受理，等待工作台 worker |
| `running` | 正在生成 |
| `completed` | 已完成，可下载 `artifacts` |
| `cancelled` | 已由工作台操作员中断或清空队列 |
| `timed_out` | 已超过 H3 4 小时等待上限；后端会按 `prompt_id` 执行 `/interrupt`、从 `/queue` 删除目标并轮询 `/queue`/`/history` 确认，不会仅改变工作台状态后让 GPU 继续运行。查看 `error` 中的确认摘要 |
| `failed` | 生成失败，查看安全摘要 `error` |

下载时复用 Basic Auth。客户端应直接使用服务返回的 `download_url`，不要猜测文件名或内部目录：

```bash
curl --fail --user "$BRM_USER:$BRM_PASSWORD" \
  --remote-name \
  "$BRM_BASE/api/v1/tasks/$TASK_ID/artifacts/<服务返回的 name>"
```

## 6. Qwen OpenAI 兼容 API

Qwen 与生成队列独立，入口是 `http://<Windows-LAN-IP>/qwen/v1`：

```bash
curl --user "$BRM_USER:$BRM_PASSWORD" "$BRM_BASE/qwen/v1/models"

curl --user "$BRM_USER:$BRM_PASSWORD" \
  "$BRM_BASE/qwen/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen35-4b-awq",
    "messages": [{"role": "user", "content": "请用一句话介绍自己。"}],
    "temperature": 0.7
  }'
```

Qwen 固定使用 GPU1（RTX A4000），媒体工作流固定使用 GPU0（RTX A5000）；两项服务正常情况下应同时在线。工作台中的“Qwen 大模型”标签页适合浏览器内流式验证。

## 7. 兼容层：原 Gradio API

旧脚本仍可使用 `GET /gradio_api/info` 与固定端点：`/submit_workflow_1` 至 `/submit_workflow_8`、`/task_status`。其中旧的 `/submit_workflow_3`、`/submit_workflow_4` 已改为同一套 H3 的 **preview** 兼容入口：参数位置不变，但时长现在只接受 `4–15` 秒；旧 LTX 的 `2–360` 秒取值不再适用。

`/submit_workflow_3_h3`、`/submit_workflow_4_h3` 是 REST 桥接使用的 Gradio 实现细节。它们因 Gradio 运行机制会出现在 API 元数据中并受入口 Basic Auth 保护，但**不承诺对外稳定性**，业务系统不要直接依赖。所有 Gradio 端点都使用 v2 POST + SSE，上传文件名需已经在 ComfyUI 输入目录中；新业务系统应使用 REST API。

仓库工具 [`call_gradio_api.py`](call_gradio_api.py) 已支持局域网 Basic Auth，密码只能从环境变量读取：

```bash
export BRM_USER='brmadmin'
read -rs BRM_PASSWORD; echo
python3 call_gradio_api.py submit_workflow_1 \
  '["一只坐在窗边的橘猫", "512 × 512", 1]' \
  --base-url "$BRM_BASE" --timeout 60
```

其 `COMPLETE=` 仅表示任务进入工作台队列。请用对应 `task_id` 调用 REST `GET /api/v1/tasks/{task_id}` 查询真实完成状态。

## 8. 运维与变更约定

- `brmmedia-lan-api` 是独立 systemd 服务，监听 `127.0.0.1:9100`；Nginx `/api/` 代理是唯一对外入口。
- 部署、服务器恢复或更新后，先执行 `sudo brmmedia-verify-runtime`；`RESULT=PASS` 表示服务与接口结构就绪。H3 上线还必须完成 Qwen 连续 10 次回环对话、`accept_minimax_h3_video.sh --full` 的本地 MP4（视频 + 双声道音频）实测，以及其余媒体工作流回归后，才算生产验收通过。
- 修改任何 workflow 参数、上传限制或 REST 响应时，必须同步更新 `capabilities`、本文件、私有 `DEPLOYMENT.md` 与运行时验收。
- 所有自动化调用都应设置超时、轮询退避和重试上限；不要用并发堆积替代队列调度。视频、数字人等高显存任务通常建议工作台并发为 1。
