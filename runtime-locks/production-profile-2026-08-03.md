# BRMMedia 已验证生产运行 Profile（2026-08-03）

此文件记录返修后服务器实际通过局域网验收的运行组合。它不是模型权重备份；模型仍应从受控的 D 盘源或离线交付物恢复。

## 主机与运行目录

- Windows 11 + WSL2 Ubuntu 24.04，Python `3.12.3`
- 运行根目录：`/srv/brmmedia/app`
- ComfyUI：`/srv/brmmedia/ComfyUI`，commit `42d2aa55432b57371ddc9d4078ae250b54227641`
- GPU0：RTX A5000 24 GiB（Qwen）；GPU1：RTX A4000 16 GiB（ComfyUI）
- NVIDIA 驱动：`595.79`

## 后端

- Torch `2.5.1+cu121`、torchvision `0.20.1+cu121`、torchaudio `2.5.1+cu121`
- Gradio `6.20.0`、requests `2.34.2`、mutagen `1.48.1`
- ComfyUI 与 Gradio 仅监听 `127.0.0.1`，由 Nginx 提供认证后的局域网入口。

## Qwen3.5-4B

- 模型：`cyankiwi/Qwen3.5-4B-AWQ-4bit`，服务名 `qwen35-4b-awq`
- vLLM `0.26.0`、Torch `2.11.0+cu130`、Transformers `5.14.1`
- GPU 显存上限 `0.72`、上下文 `4096`、单并发、`--enforce-eager`
- 仅监听 `127.0.0.1:8000`；外部访问必须走 Nginx `/qwen/v1` 的 Basic Auth。

## 恢复原则

1. 先按两个 `*.lock` 文件恢复 Python 环境，再导入模型与自定义节点。
2. ComfyUI 必须 checkout 到上述 commit；升级应建立新 profile 并通过 smoke workflow。
3. 恢复后执行 `brmmedia-healthcheck`、局域网工作台、`/qwen/v1/models` 与最小工作流验收。
