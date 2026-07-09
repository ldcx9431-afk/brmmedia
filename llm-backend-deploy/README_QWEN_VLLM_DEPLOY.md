# Qwen3.6 35B-A3B AWQ vLLM 独立部署

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

## 参考

- cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit: `https://huggingface.co/cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit`
- vLLM Qwen3.6-27B recipe: `https://recipes.vllm.ai/Qwen/Qwen3.6-27B`
- Qwen3.6 量化模型列表: `https://huggingface.co/models?search=Qwen3.6%2035B%20AWQ`
