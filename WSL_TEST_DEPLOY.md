# Windows 11 + WSL2 测试部署

本说明用于保留 Windows 11 的内网测试环境。Windows Electron 启动器不部署到服务器；运行的是 Ubuntu 内的 Gradio/ComfyUI 与 vLLM 服务。

## 固定布局

- `E:\\WSL\\BRMMedia-Ubuntu`：当前已导入的 WSL2 Ubuntu 24.04 发行版的 ext4 虚拟磁盘。
- `D:\\brmmedia\\source`：可重新部署的源代码工作区；Windows 的保活和管理员脚本从此处读取。
- `/srv/brmmedia/app`：E 盘 WSL ext4 上的运行时副本，不应直接当作 Git 工作区修改。
- `/srv/brmmedia/ComfyUI/models`：ComfyUI 运行中的模型，均位于 E 盘的 WSL ext4 文件系统。
- `/srv/brmmedia/models`：Qwen 等独立服务的模型目录；缓存、日志和输出也位于 E 盘 WSL 文件系统。
- `D:\\model`：已导入的 ComfyUI 模型源目录；`baorong-model-import` 从此处以可断点续传方式同步到 E 盘。
- `D:\\models\\Qwen3.5-4B-AWQ-4bit`：Qwen 离线模型源；运行时副本位于 `/srv/brmmedia/models/Qwen3.5-4B-AWQ-4bit`。
- `D:\\brmmedia\\artifacts`：诊断、离线包和原 Windows `custom_nodes` 源码交付目录。
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

1. `Qwen3.5-4B-AWQ-4bit` 模型目录或同名 tar 包
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

- Nginx 是唯一对局域网开放的入口：`http://<Windows-LAN-IP>/`。当前地址由 DHCP 分配；在 Windows 上使用 `ipconfig` 查看物理网卡 IPv4，或在路由器为该主机设置 DHCP 静态租约。不要把旧地址写死到脚本中。
- 创建 `brmadmin` 的 Nginx Basic Auth 凭据；凭据不进入 Git。
- Gradio、ComfyUI、vLLM 仅监听 `127.0.0.1`。
- Windows 与 WSL 防火墙仅允许 `192.168.1.0/24` 访问 TCP 80。

## 当前服务与只读验收

常规模式中，`baorong-backend`、`nginx`、`qwen-vllm` 都应为 active；高显存视频模式下，`baorong-backend-highvram` 会替代常规后端，Qwen 停止是预期行为。

在 WSL 中运行以下命令可进行不创建任务、不读取凭据的验收：

```bash
sudo brmmedia-verify-runtime
```

该命令检查虚拟环境、服务、Gradio/ComfyUI/Qwen 内部 HTTP、关键 Gradio 组件和 API、八个工作流的 65 个节点类型、ComfyUI 锁定 commit、Qwen 回环绑定以及受控健康检查。应以 `RESULT=PASS` 结束。

## 局域网 API 任务状态

所有 `submit_workflow_1` 到 `submit_workflow_8` API 在任务进入工作台队列后返回 `task_id`，这表示“已受理”而非模型生成已结束。使用同一认证入口调用 `task_status` 并传入该 ID，可获得 `queued`、`running`、`completed`、`cancelled` 或 `failed` 状态、时间和输出文件名；响应不会暴露服务器绝对路径。

仓库的 `call_gradio_api.py` 用于调用 Gradio API 并读取 handler 的 SSE 完成事件。对异步工作流，请读取其返回的 `task_id` 后再轮询 `task_status`，不要把首次 `COMPLETE` 误认为推理完成。
