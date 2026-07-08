# Qwen3.6 27B vLLM 独立部署

本目录用于在第二张 RTX A5000 上单独部署 Qwen3.6 27B，让 ComfyUI 和 LLM 分卡运行：

```text
GPU0: ComfyUI / Gradio / 图像视频音频生成
GPU1: Qwen3.6 27B / vLLM / OpenAI-compatible API
```

## 推荐模型

默认使用：

```text
Intel/Qwen3.6-27B-int4-AutoRound
```

原因：

- RTX A5000 单卡 24GB，不适合把 Qwen3.6 27B BF16/FP8 作为默认方案。
- INT4 AutoRound 更适合单卡 24GB。
- Hugging Face 模型页提供了 vLLM serve 示例。

可选模型：

```text
Lorbus/Qwen3.6-27B-int4-AutoRound
cyankiwi/Qwen3.6-27B-AWQ-INT4
QuantTrio/Qwen3.6-27B-AWQ
```

NVIDIA `nvidia/Qwen3.6-27B-NVFP4` 更偏 Blackwell/NVFP4 场景，不建议作为 A5000 默认值。

## 安装

```bash
sudo apt update
sudo apt install -y git git-lfs python3 python3-venv python3-pip build-essential curl

cd /opt/baorongwanxiang/llm-backend-deploy
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

业务后端可以按 OpenAI-compatible API 调用：

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
QWEN_TENSOR_PARALLEL_SIZE=1
QWEN_GPU_MEMORY_UTILIZATION=0.92
QWEN_MAX_MODEL_LEN=32768
QWEN_MAX_NUM_SEQS=4
QWEN_MAX_NUM_BATCHED_TOKENS=8192
QWEN_EXTRA_ARGS=--enable-prefix-caching
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

## 参考

- vLLM Qwen3.6-27B recipe: `https://recipes.vllm.ai/Qwen/Qwen3.6-27B`
- Intel/Qwen3.6-27B-int4-AutoRound: `https://huggingface.co/Intel/Qwen3.6-27B-int4-AutoRound`
- Lorbus/Qwen3.6-27B-int4-AutoRound: `https://huggingface.co/Lorbus/Qwen3.6-27B-int4-AutoRound`
- Qwen3.6 27B quantized model list: `https://huggingface.co/models?other=base_model%3Aquantized%3AQwen%2FQwen3.6-27B`
