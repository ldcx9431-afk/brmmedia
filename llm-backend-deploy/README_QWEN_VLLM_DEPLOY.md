# Qwen3.6 35B-A3B AWQ vLLM 独立部署

> **当前 BRMMedia 生产 Profile（2026-08-03）不是本页的旧 35B 方案。**
> 目标生产组合使用本地 `Qwen3.5-4B-AWQ-4bit`，由 GPU1（RTX A4000）运行，模型目录为
> `/srv/brmmedia/models/Qwen3.5-4B-AWQ-4bit`，仅经 Nginx 的认证 `/qwen/v1`
> 路径对局域网提供服务。精确版本与启动参数见
> `../runtime-locks/production-profile-2026-08-03.md`。请勿把本页示例直接覆盖
> 现网 `.env`；下文保留为 Qwen3.6 35B 的独立参考部署方案。

本目录用于在第二张 RTX A5000 上单独部署 Qwen3.6 35B-A3B AWQ，让 ComfyUI 和 LLM 分卡运行：

```text
GPU0: ComfyUI / Gradio / 图像视频音频生成
GPU1: Qwen3.6 35B-A3B AWQ / vLLM / OpenAI-compatible API
```

## 推荐模型

默认使用：

```text
cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit
```

本机已下载的离线模型目录：

```text
llm-backend-deploy/models/Qwen3.6-35B-A3B-AWQ-4bit
```

启动脚本默认：

```text
QWEN_PREFER_LOCAL_MODEL=true
QWEN_LOCAL_MODEL_DIR=./models/Qwen3.6-35B-A3B-AWQ-4bit
```

如果该目录存在，会优先使用本地模型路径，不再从 Hugging Face 拉取。

推荐原因：

- A5000 单卡 24GB 更适合 AWQ 4bit，而不是 BF16/FP8。
- 35B-A3B MoE 的能力和速度平衡比 27B dense 更适合本项目。
- vLLM 提供 OpenAI-compatible API，适合后续接入提示词优化、脚本生成和智能编排。

## 安装

```bash
sudo apt update
sudo apt install -y git git-lfs python3 python3-venv python3-pip build-essential curl

cd /opt/baorongwanxiang/brmmedia/llm-backend-deploy
chmod +x install_qwen_vllm.sh start_qwen_vllm.sh check_qwen_vllm.sh
./install_qwen_vllm.sh
```

## 启动

```bash
./start_qwen_vllm.sh
```

默认监听：

```text
http://服务器IP:8000/v1
```

默认固定使用第二张 GPU：

```bash
QWEN_CUDA_VISIBLE_DEVICES=1
```

## 检查

```bash
./check_qwen_vllm.sh
```

## 与主后端对接

业务后端可按 OpenAI-compatible API 调用：

```text
LLM_BASE_URL=http://127.0.0.1:8000/v1
LLM_MODEL=qwen
```

curl 示例：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [
      {"role": "user", "content": "优化一个文生视频提示词：城市夜景、赛博朋克、雨天。"}
    ],
    "temperature": 0.4,
    "max_tokens": 512
  }'
```

## A5000 推荐参数

`.env` 默认值：

```text
QWEN_MODEL=cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit
QWEN_LOCAL_MODEL_DIR=./models/Qwen3.6-35B-A3B-AWQ-4bit
QWEN_PREFER_LOCAL_MODEL=true
QWEN_CUDA_VISIBLE_DEVICES=1
QWEN_TENSOR_PARALLEL_SIZE=1
QWEN_GPU_MEMORY_UTILIZATION=0.92
QWEN_MAX_MODEL_LEN=32768
QWEN_MAX_NUM_SEQS=4
QWEN_MAX_NUM_BATCHED_TOKENS=8192
QWEN_EXTRA_ARGS=--enable-prefix-caching --quantization awq
```

调优建议：

- 显存紧张：把 `QWEN_MAX_MODEL_LEN` 改成 `16384`。
- 吞吐不够：把 `QWEN_MAX_NUM_BATCHED_TOKENS` 改成 `16384` 后压测。
- 并发多：逐步把 `QWEN_MAX_NUM_SEQS` 从 `4` 提到 `8`。
- 首次启动稳定后，再测试 `QWEN_ENABLE_MTP=true`。

## systemd

```bash
sudo cp qwen-vllm.service.example /etc/systemd/system/qwen-vllm.service
sudo sed -i "s/YOUR_USER/$USER/g" /etc/systemd/system/qwen-vllm.service
sudo systemctl daemon-reload
sudo systemctl enable --now qwen-vllm
sudo journalctl -u qwen-vllm -f
```

## 本地模型目录迁移

GitHub 不提交 `llm-backend-deploy/models/`，需要随离线包、网盘、移动硬盘或内网传输到服务器。

当前本机路径：

```text
H:/yzylauncher-win-ltx23/llm-backend-deploy/models/Qwen3.6-35B-A3B-AWQ-4bit
```

Ubuntu 目标路径：

```text
/opt/baorongwanxiang/brmmedia/llm-backend-deploy/models/Qwen3.6-35B-A3B-AWQ-4bit
```

## Qwen3.8-27B 双 A4000（Windows 原生 llama.cpp）

Qwen3.8 的 `UD-Q4_K_XL` 需要两张 A4000 分担模型层。由于官方 CUDA
预编译 `llama-server` 面向 Windows，本项目将该服务放在 Windows 主机上，
WSL 只通过 NAT 网关访问其回环受限端口；局域网用户仍只访问既有的
`/qwen/v1` Nginx Basic Auth 入口。

固定来源：

```text
repo: unsloth/Qwen3.8-27B-GGUF
revision: f1bfb127c64f7072bdd2cad55f258b9c8b2910fe
file: Qwen3.8-27B-UD-Q4_K_XL.gguf
size: 17923394624 bytes
sha256: bee238bbeb3dc0a34bde4d0dedbaee1f98c009e8bb4226f03070054c12fb1372
```

路径边界：模型源副本 `D:\model`，NVMe 运行副本
`E:\BRMMedia\qwen38-llama\models`。运行服务固定
`CUDA_VISIBLE_DEVICES=1,2`、`--split-mode layer`、`--tensor-split 1,1`、
`--ctx-size 4096`、单并发；不会占用 A5000。

管理员安装与切换：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
& C:\Users\deploy\qwen38-stage\llm-backend-deploy\install_qwen38_windows_admin.ps1
& C:\Users\deploy\qwen38-stage\llm-backend-deploy\switch_qwen_active_windows_admin.ps1 qwen38
```

安装脚本会创建仅允许 WSL NAT 网段访问的 Windows 防火墙规则与
`BRMMedia-Qwen38-Llama` 开机任务。切换脚本先停止 vLLM 4B、启动 27B、
通过模型健康检查后才让 Nginx 上游切换；失败会恢复 4B。回退：

```powershell
& C:\Users\deploy\qwen38-stage\llm-backend-deploy\switch_qwen_active_windows_admin.ps1 qwen35
```

不要同时常驻两个模型；Qwen3.5-4B vLLM 是冷备。双卡 `tensor` 模式只在
与 `layer` 的固定提示词基准比较更快且稳定后才允许替换默认模式。

## 参考

- cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit: `https://huggingface.co/cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit`
- vLLM Qwen3.6-27B recipe: `https://recipes.vllm.ai/Qwen/Qwen3.6-27B`
- Qwen3.6 量化模型列表: `https://huggingface.co/models?search=Qwen3.6%2035B%20AWQ`
