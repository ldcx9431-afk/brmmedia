# MiniMax H3 目标生产 Profile（待验收）

此文件是 2026-08-05 切换的目标与回退依据，并不代表已经通过真实推理验收。旧的
`production-profile-2026-08-03.md` 仍是当前已验证 LTX 组合的回退快照。

## GPU 与服务

- GPU0 RTX A5000 24 GiB：`baorong-backend`、ComfyUI 和全部媒体工作流。
- GPU1 RTX A4000 16 GiB：`qwen-vllm`，`QWEN_MAX_MODEL_LEN=4096`、单并发、
  `QWEN_GPU_MEMORY_UTILIZATION=0.70`、`QWEN_MAX_NUM_BATCHED_TOKENS=2048`。
- `baorong-backend-highvram` 必须停用；ComfyUI 使用动态卸载，不使用
  `--highvram` 或 `--gpu-only`。

## H3 固定组件

- ComfyUI commit：`563b98eefbe643a4cd510ee7f0b43e79880d5a3f`（原生 H3 节点；已验证包含 `nodes_minimax_h3.py`、FL2VA 与 Sigma Shift）。
- Hugging Face：`Comfy-Org/MiniMax-H3@0bd506d2e895983a9663037febda27aa3948cf48`。
- 本地组件：FL2VA INT8、Qwen3-VL-32B NVFP4 AWQ、视频 VAE、音频 VAE，约 42GB。
- D: 只作权重源；E: WSL ext4 是运行副本。H3 仅替换文生视频和图生视频；首尾帧、
  数字人仍使用现有 LTX2.3。

## 验收门槛与回退

执行 `check_h3_preflight.sh` 后，先验证 10 次 Qwen 对话，再验证各 3 次 H3 preview
文生视频/图生视频；无 OOM 后才测试 quality 及全量回归。切换时必须先停止媒体后端，
将 systemd 单元安装到候选运行目录，再运行激活脚本；激活脚本会拒绝旧服务路径。若任一
阶段失败，停止 H3 服务、恢复本文件所记录的前一 ComfyUI commit，并重新启用旧 LTX
工作流快照。
