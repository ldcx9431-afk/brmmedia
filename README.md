# 包容万象数智启动器 / Ubuntu 后端部署包

本仓库当前重点提供可在 Ubuntu 上部署的 Gradio + ComfyUI 后端包，位于：

```text
ubuntu-backend-deploy/
```

完整部署说明见：

```text
ubuntu-backend-deploy/README_UBUNTU_DEPLOY.md
```

## 重要说明

- Git 仓库不提交模型权重、Windows 便携运行时、Electron 打包产物和离线大包。
- 本地已生成包含模型的离线包：

```text
ubuntu-backend-deploy-offline-models.tar
```

该文件约 105GB，适合通过网盘、内网传输、移动硬盘或对象存储交付到 Ubuntu 服务器，不适合直接提交到 GitHub。

## Ubuntu 快速启动

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
