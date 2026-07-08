# 包容万象数智启动器 Ubuntu 后端一体化部署文档

本文档合并了 Ubuntu 后端部署说明、自定义节点清单、模型清单和模型下载链接，方便后续直接发给 AI 或运维脚本进行快速拉取部署。

当前部署包只包含 Gradio 后端、工作流和 Linux 版 ComfyUI 启动桥接，不包含 Windows Electron、Windows Python、模型大文件。

如果使用 `ubuntu-backend-deploy-offline-models.tar` 离线包，则包内已经包含 `models/` 目录。执行 `./install_ubuntu.sh` 时会自动把这些模型同步到 `$COMFYUI_ROOT/models/`，通常不需要再从 Hugging Face 下载。

Qwen3.6 27B LLM 不放在本后端进程里，已单独整合到：

```text
../llm-backend-deploy/
```

推荐 GPU 分配：

```text
GPU0: ComfyUI/Gradio
GPU1: Qwen3.6 27B vLLM
```

## 1. 目录内容

```text
ubuntu-backend-deploy/
├── entry_yzy.py
├── webui.py
├── comfyui_server.py
├── workflows/
├── models/                         # 离线大包包含；轻量包可能没有
├── install_ubuntu.sh
├── start_backend.sh
├── check_ubuntu_ready.sh
├── tune_nvidia_performance.sh
├── .env.example
├── requirements-backend.txt
├── baorong-backend.service.example
└── README_UBUNTU_DEPLOY.md
```

## 2. 推荐环境

- Ubuntu 22.04/24.04
- NVIDIA GPU，建议 24GB VRAM 起步
- RAM 建议 64GB 起步
- Python 3.10 或 3.11
- git、ffmpeg、build-essential
- NVIDIA Driver 与 PyTorch CUDA 版本匹配

## 3. 快速部署

```bash
sudo apt update
sudo apt install -y git git-lfs ffmpeg python3 python3-venv python3-pip build-essential

sudo mkdir -p /opt/baorongwanxiang
sudo chown -R "$USER:$USER" /opt/baorongwanxiang

# 把 ubuntu-backend-deploy 复制到:
# /opt/baorongwanxiang/ubuntu-backend-deploy
cd /opt/baorongwanxiang/ubuntu-backend-deploy

chmod +x install_ubuntu.sh start_backend.sh
./install_ubuntu.sh
```

安装完成后：

1. 按本文档的“自定义节点”章节安装/复制节点。
2. 如果包内已有 `models/`，安装脚本会自动同步；否则按“模型清单与本地源路径”章节放置模型。
3. 检查 `.env` 的 `COMFYUI_ROOT`、`COMFYUI_PYTHON`、端口。
4. 启动：

```bash
./start_backend.sh
```

启动成功后会打印 Gradio 端口，默认从 `9000` 开始寻找可用端口。浏览器访问：

```text
http://服务器IP:9000
```

启动前建议跑一次就绪检查：

```bash
chmod +x check_ubuntu_ready.sh tune_nvidia_performance.sh
./check_ubuntu_ready.sh
```

## 4. 环境变量

`.env` 支持：

| 变量 | 说明 |
|---|---|
| `COMFYUI_ROOT` | Ubuntu 上的 ComfyUI 根目录 |
| `COMFYUI_PYTHON` | 用来启动 ComfyUI 的 Python，建议使用本包 `.venv/bin/python` |
| `COMFYUI_HOST` | ComfyUI 监听地址，默认 `127.0.0.1` |
| `COMFYUI_PORT` | ComfyUI 端口，默认 `8188` |
| `COMFYUI_ARGS` | 额外 ComfyUI 参数，例如 `--highvram` |
| `COMFYUI_STARTUP_TIMEOUT` | ComfyUI 启动等待秒数 |
| `COMFYUI_TASK_TIMEOUT` | 单个任务最大等待秒数 |

## 5. 性能模式

默认是稳健的 `balanced`。如果服务器就是专用推理机，并且显存足够，建议打开最大性能模式：

```bash
echo 'BRM_PERF_PROFILE=max' >> .env
echo 'COMFYUI_ARGS=--highvram' >> .env
```

启动脚本会自动设置这些运行期优化变量：

```text
PYTORCH_CUDA_ALLOC_CONF=backend:cudaMallocAsync,expandable_segments:True
CUDA_MODULE_LOADING=LAZY
NVIDIA_TF32_OVERRIDE=1
TORCH_ALLOW_TF32_CUBLAS_OVERRIDE=1
OMP_NUM_THREADS=<CPU核心数>
MKL_NUM_THREADS=<CPU核心数>
HF_HOME=<部署目录>/.cache/huggingface
TORCH_HOME=<部署目录>/.cache/torch
```

可选开启 NVIDIA 持久化模式：

```bash
./tune_nvidia_performance.sh
```

如果明确知道 GPU 安全功耗上限，可以显式设置：

```bash
MAX_POWER_LIMIT_WATTS=300 ./tune_nvidia_performance.sh
```

注意：脚本不会默认锁应用时钟或盲目提高功耗，避免不同 GPU/驱动组合下启动失败。

## 6. systemd 示例

编辑 `baorong-backend.service.example`，把 `YOUR_USER` 和路径改成实际值：

```bash
sudo cp baorong-backend.service.example /etc/systemd/system/baorong-backend.service
sudo systemctl daemon-reload
sudo systemctl enable --now baorong-backend
sudo journalctl -u baorong-backend -f
```

## 7. 自定义节点

安装目录：

```text
$COMFYUI_ROOT/custom_nodes/
```

当前 Windows 包里存在的节点如下。保留了本地能找到的 GitHub 链接；没有链接的节点建议从 Windows 包复制源码目录，或通过 ComfyUI Manager 安装同名节点。

| 节点目录 | 获取方式 |
|---|---|
| `comfyui-easy-use` | 复制 Windows 包源码目录，或用 ComfyUI Manager 安装 |
| `ComfyUI-GGUF` | 复制 Windows 包源码目录，或用 ComfyUI Manager 安装 |
| `ComfyUI-Index-TTS` | 复制 Windows 包源码目录，或参考 `https://github.com/chenpipi0807/ComfyUI-Index-TTS` |
| `comfyui-kjnodes` | 复制 Windows 包源码目录，或用 ComfyUI Manager 安装 |
| `ComfyUI-MelBandRoFormer` | 复制 Windows 包源码目录；模型源见 `https://huggingface.co/Kijai/MelBandRoFormer_comfy/tree/main` |
| `ComfyUI-PromptRelay` | `git clone https://github.com/kijai/ComfyUI-PromptRelay` |
| `comfyui-videohelpersuite` | 复制 Windows 包源码目录，或用 ComfyUI Manager 安装 |
| `comfyui_essentials` | 复制 Windows 包源码目录，或用 ComfyUI Manager 安装 |
| `comfyui_layerstyle` | 复制 Windows 包源码目录，或用 ComfyUI Manager 安装 |
| `efficiency-nodes-ED` | 复制 Windows 包源码目录；Windows 日志里有 `tsc_utils` 缺失警告，Ubuntu 也要重点验证 |
| `rgthree-comfy` | `git clone https://github.com/rgthree/rgthree-comfy.git` |
| `WhatDreamsCost-ComfyUI` | `git clone https://github.com/WhatDreamsCost/WhatDreamsCost-ComfyUI` |

安装节点依赖示例：

```bash
cd "$COMFYUI_ROOT/custom_nodes"

for req in */requirements.txt; do
  /opt/baorongwanxiang/ubuntu-backend-deploy/.venv/bin/python -m pip install -r "$req"
done
```

常见系统依赖：

```bash
sudo apt install -y ffmpeg libsndfile1 libgl1 libglib2.0-0
```

## 8. 模型清单与本地源路径

以下清单来自当前 Windows 包：

```text
H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/
```

迁移到 Ubuntu 时，建议在 `$COMFYUI_ROOT/models/` 下复刻同样目录结构。离线大包已经把这些文件放在 `ubuntu-backend-deploy/models/`，安装脚本会自动同步；如果使用轻量包，则优先用“本地源路径”复制，本地缺失时再使用第 9 章 Hugging Face 链接下载。

本地模型根目录：

```text
H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models
```

Ubuntu 目标根目录：

```text
$COMFYUI_ROOT/models
```

| Ubuntu 目标相对路径 | 本地源路径 | 本地大小 |
|---|---|---:|
| `diffusion_models/acestep/acestep_v1.5_xl_base_bf16.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/acestep/acestep_v1.5_xl_base_bf16.safetensors` | 9.29 GB |
| `diffusion_models/acestep/acestep_v1.5_xl_sft_bf16.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/acestep/acestep_v1.5_xl_sft_bf16.safetensors` | 9.29 GB |
| `diffusion_models/acestep/acestep_v1.5_xl_turbo_bf16.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/acestep/acestep_v1.5_xl_turbo_bf16.safetensors` | 9.29 GB |
| `diffusion_models/flux-2-klein-base-4b-fp8.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/flux-2-klein-base-4b-fp8.safetensors` | 3.81 GB |
| `diffusion_models/LTX2.3/LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/LTX2.3/LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf` | 16.54 GB |
| `diffusion_models/MelBandRoformer_fp32.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/diffusion_models/MelBandRoformer_fp32.safetensors` | 0.85 GB |
| `text_encoders/gemma-3-12b-it-heretic-Q4_K_M.gguf` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/text_encoders/gemma-3-12b-it-heretic-Q4_K_M.gguf` | 6.80 GB |
| `text_encoders/ltx-2.3_text_projection_bf16.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/text_encoders/ltx-2.3_text_projection_bf16.safetensors` | 2.15 GB |
| `text_encoders/qwen_0.6b_ace15.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/text_encoders/qwen_0.6b_ace15.safetensors` | 1.11 GB |
| `text_encoders/qwen_3_4b.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/text_encoders/qwen_3_4b.safetensors` | 7.49 GB |
| `text_encoders/qwen_4b_ace15.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/text_encoders/qwen_4b_ace15.safetensors` | 7.80 GB |
| `unet/z_image_turbo_bf16.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/unet/z_image_turbo_bf16.safetensors` | 11.46 GB |
| `vae/ace_1.5_vae.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/vae/ace_1.5_vae.safetensors` | 0.31 GB |
| `vae/ae.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/vae/ae.safetensors` | 0.31 GB |
| `vae/full_encoder_small_decoder.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/vae/full_encoder_small_decoder.safetensors` | 0.23 GB |
| `vae/LTX23_audio_vae_bf16.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/vae/LTX23_audio_vae_bf16.safetensors` | 0.34 GB |
| `vae/LTX23_video_vae_bf16.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/vae/LTX23_video_vae_bf16.safetensors` | 1.35 GB |
| `loras/LTX-2.3/LTX-2.3-22b-AV-LoRA-talking-head-v1.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/loras/LTX-2.3/LTX-2.3-22b-AV-LoRA-talking-head-v1.safetensors` | 0.40 GB |
| `loras/LTX-2.3/Ltx2.3-Licon-VBVR-I2V-390K-R32.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/loras/LTX-2.3/Ltx2.3-Licon-VBVR-I2V-390K-R32.safetensors` | 0.52 GB |
| `loras/LTX-2.3/ltx2.3-transition.safetensors` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/loras/LTX-2.3/ltx2.3-transition.safetensors` | 0.36 GB |
| `IndexTTS-2/` | `H:/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models/IndexTTS-2/` | 多文件目录，约 10GB+ |

如果 Ubuntu 能挂载到这块 Windows 磁盘，示例复制命令如下：

```bash
export COMFYUI_ROOT=/opt/baorongwanxiang/ComfyUI
export WIN_MODELS=/mnt/h/yzylauncher-win-ltx23/win-unpacked/python/ComfyUI_windows_portable/ComfyUI/models

mkdir -p "$COMFYUI_ROOT/models"
rsync -avh --progress "$WIN_MODELS/" "$COMFYUI_ROOT/models/"
```

如果从 Windows 主机传到 Ubuntu 服务器，可以在 Windows PowerShell 中执行：

```powershell
scp -r "H:\yzylauncher-win-ltx23\win-unpacked\python\ComfyUI_windows_portable\ComfyUI\models\*" user@服务器IP:/opt/baorongwanxiang/ComfyUI/models/
```

也可以只复制当前工作流需要的模型，避免传输无关文件。

## 9. 模型下载清单

下面记录了当前工作流实际引用的模型和可查到的下载源。默认优先使用第 8 章的本地源路径复制；本地缺失时，再使用 Hugging Face 官方/作者仓库下载。国内环境可让 AI 改成 ModelScope 或镜像源。

### 9.1 Z-Image Turbo / 文生图

来源：`https://huggingface.co/Comfy-Org/z_image_turbo`

```bash
mkdir -p "$COMFYUI_ROOT/models/diffusion_models" "$COMFYUI_ROOT/models/text_encoders" "$COMFYUI_ROOT/models/vae"

huggingface-cli download Comfy-Org/z_image_turbo \
  split_files/diffusion_models/z_image_turbo_bf16.safetensors \
  --local-dir /tmp/z_image_turbo --local-dir-use-symlinks False
mv /tmp/z_image_turbo/split_files/diffusion_models/z_image_turbo_bf16.safetensors "$COMFYUI_ROOT/models/diffusion_models/"

huggingface-cli download Comfy-Org/z_image_turbo \
  split_files/text_encoders/qwen_3_4b.safetensors \
  --local-dir /tmp/z_image_turbo --local-dir-use-symlinks False
mv /tmp/z_image_turbo/split_files/text_encoders/qwen_3_4b.safetensors "$COMFYUI_ROOT/models/text_encoders/"

huggingface-cli download Comfy-Org/z_image_turbo \
  split_files/vae/ae.safetensors \
  --local-dir /tmp/z_image_turbo --local-dir-use-symlinks False
mv /tmp/z_image_turbo/split_files/vae/ae.safetensors "$COMFYUI_ROOT/models/vae/"
```

### 9.2 Flux 2 Klein 图像编辑

来源：

- `https://huggingface.co/black-forest-labs/FLUX.2-klein-base-4b-fp8`
- `https://huggingface.co/black-forest-labs/FLUX.2-small-decoder`

```bash
mkdir -p "$COMFYUI_ROOT/models/diffusion_models" "$COMFYUI_ROOT/models/text_encoders" "$COMFYUI_ROOT/models/vae"

huggingface-cli download black-forest-labs/FLUX.2-klein-base-4b-fp8 \
  flux-2-klein-base-4b-fp8.safetensors \
  --local-dir "$COMFYUI_ROOT/models/diffusion_models" --local-dir-use-symlinks False

huggingface-cli download black-forest-labs/FLUX.2-small-decoder \
  full_encoder_small_decoder.safetensors \
  --local-dir "$COMFYUI_ROOT/models/vae" --local-dir-use-symlinks False

# qwen_3_4b.safetensors 已在 Z-Image 章节下载到 text_encoders。
```

### 9.3 ACE-Step 1.5 / 音乐生成

来源：

- `https://huggingface.co/Comfy-Org/ace_step_1.5_ComfyUI_files`
- `https://huggingface.co/ACE-Step/acestep-v15-xl-turbo`

当前工作流使用本地文件名：

```text
diffusion_models/acestep/acestep_v1.5_xl_turbo_bf16.safetensors
text_encoders/qwen_0.6b_ace15.safetensors
text_encoders/qwen_4b_ace15.safetensors
vae/ace_1.5_vae.safetensors
```

Comfy-Org 仓库里可直接拉取的 ACE-Step ComfyUI 文件：

```bash
mkdir -p "$COMFYUI_ROOT/models/diffusion_models/acestep" "$COMFYUI_ROOT/models/text_encoders" "$COMFYUI_ROOT/models/vae"

huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files \
  split_files/text_encoders/qwen_0.6b_ace15.safetensors \
  --local-dir /tmp/ace_step --local-dir-use-symlinks False
mv /tmp/ace_step/split_files/text_encoders/qwen_0.6b_ace15.safetensors "$COMFYUI_ROOT/models/text_encoders/"

huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files \
  split_files/text_encoders/qwen_4b_ace15.safetensors \
  --local-dir /tmp/ace_step --local-dir-use-symlinks False
mv /tmp/ace_step/split_files/text_encoders/qwen_4b_ace15.safetensors "$COMFYUI_ROOT/models/text_encoders/"

huggingface-cli download Comfy-Org/ace_step_1.5_ComfyUI_files \
  split_files/vae/ace_1.5_vae.safetensors \
  --local-dir /tmp/ace_step --local-dir-use-symlinks False
mv /tmp/ace_step/split_files/vae/ace_1.5_vae.safetensors "$COMFYUI_ROOT/models/vae/"
```

注意：当前 Windows 包里的文件名是 `acestep_v1.5_xl_turbo_bf16.safetensors`，而 Comfy-Org ComfyUI 文件仓库里常见模板文件名可能是 `acestep_v1.5_turbo.safetensors`。如果下载源文件名不同，需二选一：

1. 改工作流中的 `unet_name` 为实际下载文件名。
2. 或把下载文件重命名为工作流期望的文件名。

### 9.4 LTX 2.3 / 视频与数字人

来源：

- `https://huggingface.co/QuantStack/LTX-2.3-GGUF`
- `https://huggingface.co/Kijai/LTX2.3_comfy`
- `https://huggingface.co/DreamFast/gemma-3-12b-it-heretic`

```bash
mkdir -p "$COMFYUI_ROOT/models/diffusion_models/LTX2.3" "$COMFYUI_ROOT/models/text_encoders" "$COMFYUI_ROOT/models/vae"

huggingface-cli download QuantStack/LTX-2.3-GGUF \
  LTX-2.3-distilled-1.1/LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf \
  --local-dir /tmp/ltx23 --local-dir-use-symlinks False
mv /tmp/ltx23/LTX-2.3-distilled-1.1/LTX-2.3-22B-distilled-1.1-Q4_K_M.gguf \
  "$COMFYUI_ROOT/models/diffusion_models/LTX2.3/"

huggingface-cli download DreamFast/gemma-3-12b-it-heretic \
  gguf/gemma-3-12b-it-heretic-Q4_K_M.gguf \
  --local-dir /tmp/gemma_heretic --local-dir-use-symlinks False
mv /tmp/gemma_heretic/gguf/gemma-3-12b-it-heretic-Q4_K_M.gguf "$COMFYUI_ROOT/models/text_encoders/"

huggingface-cli download Kijai/LTX2.3_comfy \
  text_encoders/ltx-2.3_text_projection_bf16.safetensors \
  --local-dir /tmp/ltx23_comfy --local-dir-use-symlinks False
mv /tmp/ltx23_comfy/text_encoders/ltx-2.3_text_projection_bf16.safetensors "$COMFYUI_ROOT/models/text_encoders/"

huggingface-cli download Kijai/LTX2.3_comfy \
  vae/LTX23_audio_vae_bf16.safetensors vae/LTX23_video_vae_bf16.safetensors \
  --local-dir /tmp/ltx23_comfy --local-dir-use-symlinks False
mv /tmp/ltx23_comfy/vae/LTX23_audio_vae_bf16.safetensors "$COMFYUI_ROOT/models/vae/"
mv /tmp/ltx23_comfy/vae/LTX23_video_vae_bf16.safetensors "$COMFYUI_ROOT/models/vae/"
```

### 9.5 LTX 2.3 LoRA

来源：

- `https://huggingface.co/elix3r/LTX-2.3-22b-AV-LoRA-talking-head`
- `https://huggingface.co/LiconStudio/Ltx2.3-VBVR-lora-I2V`
- `https://huggingface.co/joyfox/LTX-2.3-Transition-LORA`

```bash
mkdir -p "$COMFYUI_ROOT/models/loras/LTX-2.3"

huggingface-cli download elix3r/LTX-2.3-22b-AV-LoRA-talking-head \
  LTX-2.3-22b-AV-LoRA-talking-head-v1.safetensors \
  --local-dir "$COMFYUI_ROOT/models/loras/LTX-2.3" --local-dir-use-symlinks False

huggingface-cli download LiconStudio/Ltx2.3-VBVR-lora-I2V \
  Ltx2.3-Licon-VBVR-I2V-390K-R32.safetensors \
  --local-dir "$COMFYUI_ROOT/models/loras/LTX-2.3" --local-dir-use-symlinks False

huggingface-cli download joyfox/LTX-2.3-Transition-LORA \
  ltx2.3-transition.safetensors \
  --local-dir "$COMFYUI_ROOT/models/loras/LTX-2.3" --local-dir-use-symlinks False
```

### 9.6 MelBandRoFormer / 数字人音频处理

来源：`https://huggingface.co/Kijai/MelBandRoFormer_comfy/tree/main`

```bash
mkdir -p "$COMFYUI_ROOT/models/diffusion_models"

huggingface-cli download Kijai/MelBandRoFormer_comfy \
  MelBandRoformer_fp32.safetensors \
  --local-dir "$COMFYUI_ROOT/models/diffusion_models" --local-dir-use-symlinks False
```

### 9.7 IndexTTS-2 / 语音克隆

来源：

- `https://huggingface.co/IndexTeam/IndexTTS-2`
- `https://huggingface.co/facebook/w2v-bert-2.0`
- `https://huggingface.co/nvidia/bigvgan_v2_22khz_80band_256x`
- `https://huggingface.co/funasr/campplus`
- `https://huggingface.co/amphion/MaskGCT`

```bash
mkdir -p "$COMFYUI_ROOT/models/IndexTTS-2"

huggingface-cli download IndexTeam/IndexTTS-2 \
  --local-dir "$COMFYUI_ROOT/models/IndexTTS-2" --local-dir-use-symlinks False

huggingface-cli download facebook/w2v-bert-2.0 \
  --local-dir "$COMFYUI_ROOT/models/IndexTTS-2/w2v-bert-2.0" --local-dir-use-symlinks False

huggingface-cli download nvidia/bigvgan_v2_22khz_80band_256x \
  --local-dir "$COMFYUI_ROOT/models/IndexTTS-2/bigvgan/bigvgan_v2_22khz_80band_256x" --local-dir-use-symlinks False

wget -O "$COMFYUI_ROOT/models/IndexTTS-2/campplus_cn_common.bin" \
  "https://huggingface.co/funasr/campplus/resolve/main/campplus_cn_common.bin"

mkdir -p "$COMFYUI_ROOT/models/IndexTTS-2/semantic_codec"
wget -O "$COMFYUI_ROOT/models/IndexTTS-2/semantic_codec/model.safetensors" \
  "https://huggingface.co/amphion/MaskGCT/resolve/main/semantic_codec/model.safetensors"
```

## 10. 一键模型拉取草案

给 AI 生成正式脚本时，可以让它以这段为基础整理成 `download_models.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

COMFYUI_ROOT="${COMFYUI_ROOT:-/opt/baorongwanxiang/ComfyUI}"
python -m pip install -U "huggingface_hub[cli]"
git lfs install

# 然后按本文档第 9 章逐段执行 huggingface-cli download / wget。
```

## 11. Linux 路径提醒

工作流 JSON 里有部分模型路径是 Windows 风格：

```text
LTX2.3\\xxx
LTX-2.3\\xxx
acestep\\xxx
```

Linux 推荐改为：

```text
LTX2.3/xxx
LTX-2.3/xxx
acestep/xxx
```

如果节点内部会自动标准化路径，也可以先不改，实际提交工作流验证为准。

## 12. 注意事项

- 当前 Windows 包里的 `ComfyUI_windows_portable`、`python_embeded`、`.exe`、DLL 不要复制到 Ubuntu 当运行环境使用。
- 自定义节点依赖必须在 Ubuntu venv 里重新安装，不能复用 Windows 的 `site-packages`。
- 大模型下载前确认磁盘空间，完整包建议至少预留 150GB。
- 首次迁移建议逐个工作流测试：文生图 -> 图生图 -> TTS -> 音乐 -> 视频/数字人。
