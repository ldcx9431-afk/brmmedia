# SeedVR2 7B / 3B INT8 图像 / 视频增强修复

## 固定模型与运行方式

工作流使用 ComfyUI 原生 SeedVR2 节点，不安装自定义节点、不升级现有 ComfyUI，也不改变 Qwen 服务。图片和视频共用工作台媒体队列；只要至少一种扩散模型与共享 VAE 已安装，工作台即可提交对应模式，媒体并发仍强制为 1。7B 为原有默认高质量模式；3B 是额外的快速模式，安装时与 7B 共存，严禁用 3B 覆盖或删除 7B。

固定来源为 Hugging Face `Comfy-Org/SeedVR2` revision `10f035adc869a5b3ffc466360b869641511c0610`：

| 用途 | 文件 | 大小 | SHA-256 |
| --- | --- | ---: | --- |
| 7B INT8 diffusion model | `seedvr2_7b_int8_convrot.safetensors` | 8,334,897,976 bytes | `5aa0d25fc9d35e449b659d0c9a5dcb22e2a4fa04032101b95a39da42b32c1be6` |
| 3B INT8 fast model | `seedvr2_3b_int8_convrot.safetensors` | 3.46 GB | `c3dec8bcc5916843a8a858572970597462e1f2dc598d6dfd818f6cd40f53a157` |
| VAE | `ema_vae_fp16.safetensors` | 501,324,814 bytes | `20678548f420d98d26f11442d3528f8b8c94e57ee046ef93dbb7633da8612ca1` |

模型卡：[固定 revision 文件列表](https://huggingface.co/Comfy-Org/SeedVR2/tree/10f035adc869a5b3ffc466360b869641511c0610)。实现参考：[Comfy-Org 7B 图片工作流](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/utility_seedvr2_7b_int8_upscale_image.json)、[Comfy-Org 视频工作流](https://github.com/Comfy-Org/workflow_templates/blob/main/templates/utility_seedvr2_3b_int8_upscale_video.json) 和 [SeedVR2 时间分块节点说明](https://github.com/Comfy-Org/embedded-docs/blob/main/comfyui_embedded_docs/docs/SeedVR2TemporalChunk/en.md)。

将文件放入当前服务实际使用的 `$COMFYUI_ROOT`。7B、3B 放在同一扩散模型目录并各自保留文件；两种模式共用该 revision 中同一个 VAE：

```text
$COMFYUI_ROOT/models/diffusion_models/seedvr2_7b_int8_convrot.safetensors
$COMFYUI_ROOT/models/diffusion_models/seedvr2_3b_int8_convrot.safetensors
$COMFYUI_ROOT/models/vae/ema_vae_fp16.safetensors
```

两个模型都安装时，UI 默认仍选 7B；如果只装了其中一个，UI 默认选择已安装者。UI 可单独选择“3B 快速模式”。能力接口会分别报告 7B/3B 是否可用；所选模型未安装时任务不会入队。API 通过 `model_variant=7b|3b` 选择，省略时保持 `7b`，以兼容既有客户端。

`$COMFYUI_ROOT` 必须从 `baorong-backend` 服务环境读取，不能假定旧 ComfyUI 目录就是生产目录。LAN API 是独立的 systemd 服务，不会继承 `start_backend.sh` 导出的环境变量；因此需为 `brmmedia-lan-api` 安装 [`brmmedia-lan-api-seedvr2.conf.example`](brmmedia-lan-api-seedvr2.conf.example) 对应的 drop-in，并将其中路径设为与工作台相同的 `$COMFYUI_ROOT`。否则能力接口会报告 `models_missing`，素材也可能被写入错误的 ComfyUI 目录。工作台只会为“已安装的模型 + 共享 VAE”启用相应模式；缺少 3B 不影响既有 7B 使用。

## 能力与资源限制

- 工作台将 SeedVR2 从“图像创作”移到独立的“媒体处理”主菜单；其中可切换图片/视频。
- 工作流标识：`seedvr2-enhance`；图片与视频由 `mode=image|video` 区分。
- 图片上传继续使用现有 `image` 上传限制；视频通过 `POST /api/v1/files?kind=video` 上传，最多 2 GiB，接受 MP4/MOV/MKV/WebM/AVI，扩展名大小写不敏感（如 `.mp4`、`.MP4`、`.mov`、`.MOV` 均可）。
- 视频要求恒定帧率，支持 1–120 fps、时长最多 12 小时；VFR 会在上传阶段拒绝，避免输出音画漂移。
- `scale` 仅支持 1 或 2；`strength=light|standard|strong` 分别映射去噪值 `0.6/0.8/1.0`。
- 视频界面提供按倍率、720p、1080p 输出预设；固定分辨率按短边设置并保持源画幅，API 对应 `resolution_preset=native|720p|1080p`。
- 4K UHD（2160 短边；16:9 为 3840×2160）目前不在 UI 提供；API 仍拒绝此档位，不会入队。虽然 ComfyUI 有时间分块降低长序列显存，但这不等于单帧 4K 已通过 A5000 验证；只有实际显存、长任务稳定性及画质验收通过后才能启用。
- 默认输出限制：最长边 2560 px、总像素 4,000,000。可由 `BRM_SEEDVR2_MAX_OUTPUT_EDGE`、`BRM_SEEDVR2_MAX_OUTPUT_PIXELS` 调低或调高；放宽前需在 A5000 上完成显存、内存和画质验收。服务会在素材上传/任务提交时报告限制，不做静默缩小。
- 视频按最多 241 帧（4n+1）和配置的片段时长上限拆段，在同一个工作台任务中串行处理；ComfyUI 原生 temporal chunk/merge 控制显存并处理内部重叠。输出保持源帧率，源音轨在合并阶段复用；无音轨时输出无音轨 MP4。
- 视频任务默认超时 12 小时。`BRM_SEEDVR2_TASK_TIMEOUT` 可缩短超时，不可超过 12 小时；`BRM_SEEDVR2_SEGMENT_SECONDS` 可把外部分段上限设为 30–600 秒，实际还会受 241 帧上限约束。
- 提交前按输入大小和倍率估算临时空间；不足时拒绝排队。每段完成后清理 ComfyUI 暂存输出，取消/失败时删除中间文件和未完成结果。

所有限制都可在 `GET /api/v1/capabilities` 的 `seedvr2-enhance.options` 中发现。API 输入字段、异步任务查询和产物下载见仓库根目录 [`API接口文档.md`](../API接口文档.md)。

## 部署与验收顺序

1. 确认工作台、LAN API、ComfyUI 和 Qwen 健康；确认 GPU 队列空闲及模型/输出盘空间充足。
2. 备份当前生产 `webui.py`、`lan_api.py`、`comfyui_server.py`、`seedvr2_support.py`（如已存在）、LAN API systemd drop-in（如已存在）和 Nginx 站点配置；模型先下载到临时文件，校验 SHA-256 与大小后再原子移动到 `$COMFYUI_ROOT/models/`。
3. `/api/` 的 Nginx `client_max_body_size` 设为 `2100M`，只为 multipart 开销留余量；应用仍严格将视频文件限制在 2 GiB。运行 `nginx -t` 后 reload。
4. 更新工作台/API 代码后先执行生产虚拟环境 `py_compile`。确认当前 ComfyUI `/object_info` 中包含 `SeedVR2Conditioning`、`SeedVR2Preprocess`、`SeedVR2PostProcessing`、`SeedVR2TemporalChunk`、`SeedVR2TemporalMerge`；每个准备开放的扩散权重和共享 VAE 均被 Loader 发现。安装 3B 时只新增该文件，禁止覆盖 7B。
5. 安装/更新 LAN API drop-in 后执行 `systemctl daemon-reload`，再重启 `baorong-backend` 与 `brmmedia-lan-api`；确认两者使用相同的 `$COMFYUI_ROOT`、`/api/v1/health` 为 `200`，`/api/v1/capabilities` 显示 SeedVR2 `ready`，Qwen 健康检查仍通过。
6. 依次验收 1×/2× 图片、带音轨短视频、无音轨短视频、多段视频；通过 `ffprobe` 检查输出可解码、帧率/时长/尺寸合理、源音轨有且仅有预期保留。观察峰值 VRAM、系统内存、磁盘余量、取消清理和队列串行。验收素材与产物通过后清理。

未通过任一验收时，停止 SeedVR2 任务，恢复上一步代码和 Nginx 备份、reload/restart 原服务并复测健康检查。模型文件可以保留在磁盘，但必须将工作台/API 回滚到不包含此功能的代码版本；不得让未验收的工作流继续接收请求。

## 本次生产验收记录（2026-09-24）

- 生产 7B 与 VAE 已固定安装并通过 SHA-256 校验；3B 快速模型为单独的可选权重，部署前需额外安装并校验其 SHA-256。ComfyUI 原生 SeedVR2 节点和模型加载器均可用。LAN API 能力接口按已安装权重报告 `seedvr2-enhance` 状态。没有升级 ComfyUI、安装额外节点或重启 Qwen 服务。
- 工作台“图像/视频增强修复 SeedVR2”独立页签经生产 Python 环境 `build_ui()` 预检；图片任务经 LAN API 完整提交、执行并下载验证。视频原生 `SaveVideo` 的 `codec` 动态选项已按 ComfyUI API 格式改为字符串 `h264`；`SeedVR2TemporalChunk.chunking_mode` 使用字符串 `auto`。
- 256×256 合成短视频经 2×输出为 512×512、1 fps、5 帧、5 秒 H.264 MP4；带音轨样例保留 5 秒 AAC 原音轨，无音轨样例输出不含音轨的 MP4。三段产物均由 `ffprobe` 验证可解码。
- 246 秒、246 帧无音轨样例识别为 2 段并串行成功，输出为 512×512、1 fps、246 帧、246 秒 H.264 MP4；处理约 62 秒。A5000 峰值显存占用约 18,063 MiB / 24,564 MiB，任务后回落至约 9.4 GiB；Qwen 所在 GPU 未参与 SeedVR2 推理，Qwen `/qwen/v1/models` 检查仍为 HTTP 200。
- 这轮样例验证了任务闭环、音轨处理、分段合并与资源行为；画质表现仍需用业务真实图片/视频由使用者确认。测试输入、生成产物与临时验收文件在验收后清理，模型权重和部署回滚备份保留。
