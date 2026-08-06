# 包容万象数智启动器 / Ubuntu 后端部署包

本仓库当前重点提供可在 Ubuntu 上部署的 Gradio + ComfyUI 后端包，位于：

```text
ubuntu-backend-deploy/
```

Qwen vLLM 独立部署位于：

```text
llm-backend-deploy/
```

完整部署说明见：

```text
ubuntu-backend-deploy/README_UBUNTU_DEPLOY.md
llm-backend-deploy/README_QWEN_VLLM_DEPLOY.md
```

Windows 11 + WSL2 局域网测试、运行时验收和恢复边界见：

```text
WSL_TEST_DEPLOY.md
runtime-locks/production-profile-2026-08-03.md
runtime-locks/minimax-h3-target-profile-2026-08-05.md
```

## 重要说明

- **当前服务器部署只执行 `WSL_TEST_DEPLOY.md`。** MiniMax H3 目标 Profile 使用
  `/srv/brmmedia`、ComfyUI GPU0（A5000）、Qwen3.5-4B GPU1（A4000），并只经 Nginx
  Basic Auth 的 TCP 80 对局域网提供服务。
  本 README 下方的 `/opt/baorongwanxiang` 通用 Ubuntu 安装和旧离线包说明仅作历史/迁移参考，
  不得覆盖当前运行目录或 `.env`。
- Git 仓库不提交模型权重、Windows 便携运行时、Electron 打包产物和离线大包。
- 本地已生成包含模型的离线包：

```text
ubuntu-backend-deploy-offline-models.tar
```

该文件约 105GB，适合通过网盘、内网传输、移动硬盘或对象存储交付到 Ubuntu 服务器，不适合直接提交到 GitHub。

## Ubuntu 快速启动

从 GitHub 同步后，一键安装全栈：

```bash
cd /opt/baorongwanxiang/brmmedia
chmod +x install_ubuntu_full_stack.sh check_ubuntu_full_stack.sh install_ubuntu_systemd_services.sh
./install_ubuntu_full_stack.sh
./check_ubuntu_full_stack.sh
sudo ./install_ubuntu_systemd_services.sh "$USER"
```

只启动后端离线包：

```bash
tar -xf ubuntu-backend-deploy-offline-models.tar
cd ubuntu-backend-deploy
chmod +x install_ubuntu.sh start_backend.sh check_ubuntu_ready.sh tune_nvidia_performance.sh
./install_ubuntu.sh
./check_ubuntu_ready.sh
./start_backend.sh
```

MiniMax H3 使用动态显存卸载；不要设置 `--highvram` 或 `--gpu-only`。可按
`WSL_TEST_DEPLOY.md` 先执行 `check_h3_preflight.sh`，再进行受控升级和模型导入。

当前目标 Profile 的 Qwen vLLM 使用 GPU1（RTX A4000）与本地
`Qwen3.5-4B-AWQ-4bit`；ComfyUI 全部媒体流程使用 GPU0（RTX A5000）。

```text
QWEN_CUDA_VISIBLE_DEVICES=1
QWEN_MODEL=cyankiwi/Qwen3.5-4B-AWQ-4bit
QWEN_LOCAL_MODEL_DIR=/srv/brmmedia/models/Qwen3.5-4B-AWQ-4bit
QWEN_PREFER_LOCAL_MODEL=true
QWEN_PORT=8000
```
