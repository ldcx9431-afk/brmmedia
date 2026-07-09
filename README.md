# 包容万象数智启动器 / Ubuntu 后端部署包

本仓库当前重点提供可在 Ubuntu 上部署的 Gradio + ComfyUI 后端包，位于：

```text
ubuntu-backend-deploy/
```

Qwen3.6 27B vLLM 独立部署位于：

```text
llm-backend-deploy/
```

完整部署说明见：

```text
ubuntu-backend-deploy/README_UBUNTU_DEPLOY.md
llm-backend-deploy/README_QWEN_VLLM_DEPLOY.md
```

## 重要说明

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

高性能模式：

```bash
echo 'BRM_PERF_PROFILE=max' >> .env
echo 'COMFYUI_ARGS=--highvram' >> .env
./tune_nvidia_performance.sh
```

Qwen vLLM 默认使用第二张 A5000：

```text
QWEN_CUDA_VISIBLE_DEVICES=1
QWEN_MODEL=cyankiwi/Qwen3.6-35B-A3B-AWQ-4bit
QWEN_LOCAL_MODEL_DIR=./models/Qwen3.6-35B-A3B-AWQ-4bit
QWEN_PREFER_LOCAL_MODEL=true
QWEN_PORT=8000
```
