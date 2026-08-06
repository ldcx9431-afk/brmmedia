# Windows 11 + WSL2 测试部署

本说明用于保留 Windows 11 的内网测试环境。Windows Electron 启动器不部署到服务器；运行的是 Ubuntu 内的 Gradio/ComfyUI 与 vLLM 服务。

## 固定布局

- `E:\\WSL\\BRMMedia-Ubuntu`：当前已导入的 WSL2 Ubuntu 24.04 发行版的 ext4 虚拟磁盘。
- `D:\\brmmedia\\source`：可重新部署的源代码工作区；Windows 的保活和管理员脚本从此处读取。
- `/srv/brmmedia/app`：E 盘 WSL ext4 上的运行时副本，不应直接当作 Git 工作区修改。
- `/srv/brmmedia/ComfyUI/models`：ComfyUI 运行中的模型，均位于 E 盘的 WSL ext4 文件系统。
- `/srv/brmmedia/models`：Qwen 等独立服务的模型目录；缓存、日志和输出也位于 E 盘 WSL 文件系统。
- `D:\\model`：已导入的 ComfyUI 模型源目录；`baorong-model-import` 从此处以可断点续传方式同步到 E 盘。
- `D:\\model\\MiniMax-H3`：MiniMax H3 约 42GB 的受控源权重；必须先落到这里，再导入 `/srv/brmmedia/ComfyUI/models`。
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
| 生产 | ComfyUI/Gradio（全部媒体流程） | `0` | RTX A5000 24 GB |
| 生产 | Qwen/vLLM | `1` | RTX A4000 16 GB |

两项服务必须同时在线。禁止 `--highvram`、`--gpu-only` 和旧的 `baorong-backend-highvram`，让 MiniMax H3 的内置 Qwen3-VL 编码器在 A5000 任务期间动态装卸。

## 模型交付要求

ComfyUI 的基础模型已从 `D:\\model` 导入。启动完整推理栈前，还需将以下资源交付到 `D:\\brmmedia\\artifacts`：

1. `Qwen3.5-4B-AWQ-4bit` 模型目录或同名 tar 包
2. `custom_nodes.tar`，其中包含仓库未提交、但工作流所依赖的 ComfyUI 自定义节点源码

导入前先计算 SHA-256。Qwen 解压后放入 `/srv/brmmedia/models`；自定义节点解压后放入 `/srv/brmmedia/ComfyUI/custom_nodes`。模型或节点缺失时只能验证服务骨架，不能宣称生成工作流已验证。

## 工作流验证顺序

切换 MiniMax H3 前必须执行以下受控步骤。下载、导入和服务切换是独立阶段：下载权重不改变当前 LTX 服务；只有 H3 预检与实测通过后才设置 `BRMMEDIA_VIDEO_ENGINE=h3` 并重启后端。

```bash
cd /srv/brmmedia/app
./download_minimax_h3_models.sh
./import_comfy_models.sh
./verify_comfy_models.sh
sudo systemctl stop baorong-backend
./prepare_minimax_h3_comfyui.sh
sudo systemctl start baorong-backend
```

预检要求 GPU0=A5000、GPU1=A4000、WSL 可见内存不少于 64 GB、Windows E: 页面文件不少于 64 GB，并在 D: 模型源与 E: WSL 运行目录都保留至少 50 GB 空间。下载脚本固定 Hugging Face revision，并不会把 Token 写入脚本或仓库。如经运维确认主机容量足够但页面文件检查不可读，可一次性显式设置 `BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE=1`；这会在预检日志中留下记录，不能作为常规默认配置。

下载器默认使用 HF CLI；若代理与 Python TLS 不兼容，设置 `BRMMEDIA_H3_TRANSPORT=curl`，它会用同一 revision 的 `curl -L -C -` 断点续传四个文件。不要删除 `D:\model\MiniMax-H3` 中的未完成文件，后续运行会从已有字节继续。

若需要在 SSH 断开后继续下载，优先执行 `sudo ./start_minimax_h3_download.sh`。它为四个文件创建低优先级临时 systemd 下载单元；用 `sudo ./start_minimax_h3_download.sh --status` 查询每个文件的状态与字节数，失败后直接再次运行同一命令即可续传。

H3 未验收时保持 `BRMMEDIA_VIDEO_ENGINE=ltx23`（默认）；需回退 ComfyUI 时，在停止 `baorong-backend` 后执行 `./rollback_minimax_h3_comfyui.sh`，再启动服务并运行 `sudo brmmedia-verify-runtime`。

确认没有媒体任务运行、H3 权重导入完成后，使用 `sudo ./activate_minimax_h3.sh` 执行固定版本升级并将运行时引擎置为 `h3`。激活后先跑文生视频、图生视频各一次 preview，并用 `ffprobe` 确认 MP4 含音频流；再各跑三次 preview、一次 quality，最后才运行完整回归验收。

受控升级会将旧 ComfyUI commit、工作区状态和后端 Python 依赖版本冻结到 `runtime-locks/comfyui-h3-backups/`；升级过程中失败会自动恢复旧 commit 与已冻结的 Python 包版本。

服务器上已有旧工作区出现未提交修改时，不要强行 `git pull` 或覆盖 `/srv/brmmedia/app`。先从干净 Git clone 执行 `sudo ./stage_h3_runtime_release.sh /mnt/d/brmmedia/releases/h3-<commit>`，它会在 E: 创建独立运行副本并复用已验证的 venv、配置、任务历史与素材输出；旧运行目录保留为回退目标。

恢复服务后，先在 WSL 内执行以下无推理预检；它会逐一检查项目工作流需要的 ComfyUI 节点，缺少的节点会按工作流名称列出：

```bash
cd /srv/brmmedia/app
python3 validate_comfy_workflows.py --url http://127.0.0.1:8188
```

预检全部显示 `READY` 后，先完成 Qwen 连续 10 次对话；再分别完成 H3 文生视频、图生视频各 3 次 `preview` 任务；通过后验证 `quality`。最后回归文生图、图片编辑、音乐、语音克隆、首尾帧视频、数字人、历史素材和 REST API。H3 默认 `preview`，时长 4–15 秒，并会输出带原生音频的 MP4。

## 内网访问

- Nginx 是唯一对局域网开放的入口：`http://<Windows-LAN-IP>/`。当前地址由 DHCP 分配；在 Windows 上使用 `ipconfig` 查看物理网卡 IPv4，或在路由器为该主机设置 DHCP 静态租约。不要把旧地址写死到脚本中。
- 创建 `brmadmin` 的 Nginx Basic Auth 凭据；凭据不进入 Git。
- Gradio、ComfyUI、vLLM 仅监听 `127.0.0.1`。
- Windows 与 WSL 防火墙仅允许 `192.168.1.0/24` 访问 TCP 80。

## 当前服务与只读验收

生产模式中，`baorong-backend`、`brmmedia-lan-api`、`nginx`、`qwen-vllm` 都应为 `active`；`baorong-backend-highvram` 必须为 `inactive`。

在 WSL 中运行以下命令可进行不创建任务、不读取凭据的验收：

```bash
sudo brmmedia-verify-runtime
```

该命令检查虚拟环境、服务、Gradio/ComfyUI/Qwen 内部 HTTP、关键 Gradio 组件和 API、八个工作流的 65 个节点类型、ComfyUI 锁定 commit、Qwen 回环绑定以及受控健康检查。应以 `RESULT=PASS` 结束。

## 局域网 API 任务状态

所有 `submit_workflow_1` 到 `submit_workflow_8` API 在任务进入工作台队列后返回 `task_id`，这表示“已受理”而非模型生成已结束。使用同一认证入口调用 `task_status` 并传入该 ID，可获得 `queued`、`running`、`completed`、`cancelled` 或 `failed` 状态、时间和输出文件名；响应不会暴露服务器绝对路径。

仓库的 `call_gradio_api.py` 用于调用 Gradio API 并读取 handler 的 SSE 完成事件。对异步工作流，请读取其返回的 `task_id` 后再轮询 `task_status`，不要把首次 `COMPLETE` 误认为推理完成。
