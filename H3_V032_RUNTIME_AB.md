# ComfyUI v0.32 H3 隔离运行时与 A/B 交付

本说明只覆盖运行时候选，不会下载 Turbo LoRA、修改工作流参数或切换生产。
候选使用独立 ComfyUI checkout、物理 venv、环境文件、输出目录和回环端口。

## 固定版本与安全边界

- ComfyUI `v0.32.0` 固定为 commit
  `c2bcbecd82ec5ae66594340b395c24ef0217b238`。
- PyTorch `2.11.0+cu130`、torchvision `0.26.0+cu130`、torchaudio
  `2.11.0+cu130`，GPU 必须为 A5000 `sm86`。
- SageAttention 固定官方 v2.2.0 commit
  `eb615cf6cf4d221338033340ee2de1c37fbdba4a`；`comfy-kitchen==0.2.30`
  由 ComfyUI v0.32 requirements 固定。
- 候选默认目录 `/srv/brmmedia/ComfyUI-h3-v032-canary`，默认 venv
  `<candidate>/runtime-locks/venvs/h3-v032-canary`，回环端口保持
  Gradio `9001`、ComfyUI `8189`、REST `9101`。
- 不允许 Nginx 指向候选，也不允许在生产 backend 活跃时启动候选。

禁止 `--highvram`、`--gpu-only`、`--lowvram`、`--novram`、
`--disable-smart-memory`、`--cache-none`、`--disable-dynamic-vram` 和
`--disable-async-offload`。H3 保留动态 VRAM、智能缓存与 NVIDIA 异步卸载。

## 准备候选

先排空媒体队列并停止生产 backend。以下命令只准备隔离目录：

```bash
runtime=/srv/brmmedia/releases/h3-<reviewed-commit>
sudo systemctl stop baorong-backend
sudo "$runtime/prepare_minimax_h3_canary.sh"
sudo BRMMEDIA_H3_SAGE_SOURCE_BUILD=1 \
  "$runtime/prepare_h3_cuda13_sage_candidate.sh"
```

准备脚本会以 `runtime-locks/h3-comfyui-v0.32.0.env` 为不可变锁，拒绝 tag、
commit、版本、CUDA、GPU 或物理 venv 漂移。它不覆盖生产 `.venv`。

## A/B 矩阵

每个单元必须停止候选、配置、启动、执行相同 seed/提示词/输入、记录三次后再停止。
Sage 与 Kitchen Attention 不能叠加：

```bash
# A：工作流级 Sage，默认 RAM pressure cache
sudo "$runtime/configure_h3_canary_ab.sh" \
  --attention workflow-sage --fast-disk off --cache default

# B：工作流级 Sage + NVMe disk-backed offload
sudo "$runtime/configure_h3_canary_ab.sh" \
  --attention workflow-sage --fast-disk on --cache default

# C：工作流级 Sage + LRU 1
sudo "$runtime/configure_h3_canary_ab.sh" \
  --attention workflow-sage --fast-disk off --cache lru1

# D/E/F：使用无 Sage Patch 的独立私有工作流快照，再测试 Kitchen
sudo "$runtime/configure_h3_canary_ab.sh" \
  --attention kitchen --fast-disk off --cache default
```

Kitchen 单元若发现私有 H3 工作流仍有 `PathchSageAttentionKJ`（KJNodes
保留的历史类名拼写），配置器会直接失败，防止双重 patch。反向亦然。

每次启动前都会自动执行：

```bash
sudo "$runtime/verify_h3_canary_runtime.sh"
sudo "$runtime/start_minimax_h3_canary.sh"
```

运行时门禁检查精确 ComfyUI commit/version、CUDA 13、sm86、Sage 或
comfy-kitchen、参数与 A/B 元数据一致性，再进入原有实时 workflow gate。

## 记录与晋级规则

固定同一提示词、seed 和 I2V 输入，先三次 5 秒 preview，再测试 15 秒 quality。
每个单元记录冷/热启动、首步、采样、VAE 视频/音频解码、总耗时、显存、RAM、
页面文件、NVMe 读取、温度、输出时长与双声道音频。

只在三次中位数显著优于基线、无 OOM/重启/音频缺失，并完成其他媒体回归后，
才能把**该精确参数**写入后续生产 release。`--fast-disk` 或 `--cache-lru 1`
均不得凭单次任务直接晋级。候选失败时：

```bash
sudo "$runtime/stop_minimax_h3_canary.sh"
sudo systemctl start baorong-backend
```

候选 checkout、venv、日志和输出保留用于审计；不要运行生产激活脚本。

运行时单元选定后，再用同一运行时做 `standard`、`turbo_balanced`、`turbo_fast`
工作流 A/B。不得把运行时变量和 LoRA 模式同时变化后归因于某一项。两个 LightX2V
v1.0 权重必须通过下载 revision、字节数和 SHA-256 三重门禁；默认仍为 standard。
