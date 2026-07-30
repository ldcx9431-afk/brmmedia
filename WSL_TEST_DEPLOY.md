# Windows 11 + WSL2 测试部署

本说明用于保留 Windows 11 的内网测试环境。Windows Electron 启动器不部署到服务器；运行的是 Ubuntu 内的 Gradio/ComfyUI 与 vLLM 服务。

## 固定布局

- `E:\\WSL\\BRMMedia-Ubuntu`：当前已导入的 WSL2 Ubuntu 24.04 发行版的 ext4 虚拟磁盘。
- `/srv/brmmedia/app`：Git 工作区。
- `/srv/brmmedia/ComfyUI/models`：ComfyUI 运行中的模型，均位于 E 盘的 WSL ext4 文件系统。
- `/srv/brmmedia/models`：Qwen 等独立服务的模型目录；缓存、日志和输出也位于 E 盘 WSL 文件系统。
- `D:\\model`：已导入的 ComfyUI 模型源目录；`baorong-model-import` 从此处以可断点续传方式同步到 E 盘。
- `D:\\brmmedia\\artifacts`：Qwen 离线模型包和原 Windows `custom_nodes` 源码交付目录。
- `D:\\brmmedia\\archive`：历史输出与备份。

不要从 `/mnt/d` 或 `/mnt/e` 直接运行模型。

## Windows 前置步骤

以管理员身份运行仓库根目录的 `prepare_windows_wsl.ps1`，然后重启 Windows。
脚本只启用 WSL2 前置功能并更新运行时，不会在 C 盘安装 Ubuntu。重启后再将 Ubuntu 导入 `E:\\WSL\\BRMMedia-Ubuntu`。

## GPU 策略

| 模式 | 服务 | CUDA 设备 | 物理 GPU |
|---|---|---:|---|
| 常规 | ComfyUI/Gradio | `1` | RTX A4000 16 GB |
| 常规 | Qwen/vLLM | `0` | RTX A5000 24 GB |
| 高显存视频 | ComfyUI/Gradio | `0` | RTX A5000 24 GB |

高显存视频服务与 Qwen 服务互斥。

## 模型交付要求

ComfyUI 的基础模型已从 `D:\\model` 导入。启动完整推理栈前，还需将以下资源交付到 `D:\\brmmedia\\artifacts`：

1. `Qwen3.6-35B-A3B-AWQ-4bit` 模型目录或同名 tar 包
2. `custom_nodes.tar`，其中包含仓库未提交、但工作流所依赖的 ComfyUI 自定义节点源码

导入前先计算 SHA-256。Qwen 解压后放入 `/srv/brmmedia/models`；自定义节点解压后放入 `/srv/brmmedia/ComfyUI/custom_nodes`。模型或节点缺失时只能验证服务骨架，不能宣称生成工作流已验证。

## 工作流验证顺序

恢复服务后，先在 WSL 内执行以下无推理预检；它会逐一检查八个项目工作流需要的 ComfyUI 节点，缺少的节点会按工作流名称列出：

```bash
cd /srv/brmmedia/app
python3 validate_comfy_workflows.py --url http://127.0.0.1:8188
```

预检全部显示 `READY` 后，按以下顺序提交小规格真实任务并核对产物文件：文生图、图片编辑、音乐、语音克隆、文生视频、图生视频、首尾帧视频、数字人。视频类先使用最短时长；高显存视频模式与 Qwen vLLM 服务互斥。

## 内网访问

- Nginx 是唯一对局域网开放的入口：`http://192.168.1.106/`。
- 创建 `brmadmin` 的 Nginx Basic Auth 凭据；凭据不进入 Git。
- Gradio、ComfyUI、vLLM 仅监听 `127.0.0.1`。
- Windows 与 WSL 防火墙仅允许 `192.168.1.0/24` 访问 TCP 80。
