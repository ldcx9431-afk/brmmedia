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
release=/mnt/d/brmmedia/releases/h3-<commit>
runtime=/srv/brmmedia/releases/h3-<commit>
cd "$release"
sudo ./start_minimax_h3_download.sh
# Optional but recommended: wait, import and byte-verify in a background unit.
sudo systemd-run --unit=brmmedia-h3-import --collect --property=User=brm \
  /usr/bin/env bash "$release/wait_import_minimax_h3_models.sh"
# After the import unit reports success, drain the media queue. The candidate
# code is refreshed from the reviewed D: release while no service points to it.
sudo "$release/refresh_h3_runtime_release.sh" "$release" "$runtime"
# Keep the public backend on its existing LTX release while the H3 canary is
# tested on private loopback ports. It shares A5000, so production must be
# drained/stopped, but Nginx is never repointed to the canary.
sudo systemctl stop baorong-backend
sudo "$runtime/prepare_minimax_h3_canary.sh"
sudo "$runtime/start_minimax_h3_canary.sh"
sudo BRMMEDIA_LAN_API_BASE=http://127.0.0.1:9101/api/v1 \
  BRMMEDIA_BACKEND_UNIT=baorong-backend-h3-canary \
  BRMMEDIA_RESTART_GRADIO_HEALTH_URL=http://127.0.0.1:9001/gradio_api/info \
  "$runtime/accept_minimax_h3_video.sh" --full --restart-recovery
sudo "$runtime/stop_minimax_h3_canary.sh"
# Only after this canary acceptance succeeds, install the candidate unit files
# and switch the production endpoint to H3.
sudo "$runtime/install_ubuntu_systemd_services.sh" brm "$runtime"
sudo BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE=1 "$runtime/activate_minimax_h3.sh"
```

若 Canary 任一准备、启动或验收步骤失败，立即运行 `sudo "$runtime/stop_minimax_h3_canary.sh"`，然后执行 `sudo systemctl start baorong-backend` 恢复原 LTX 生产后端；**不要**执行候选 `install_ubuntu_systemd_services.sh` 或 `activate_minimax_h3.sh`。Canary 的 checkout、配置和隔离输出会保留，便于检查 `journalctl -u baorong-backend-h3-canary -u brmmedia-lan-api-h3-canary` 后重试。

预检要求 GPU0=A5000、GPU1=A4000、WSL 可见内存不少于 64 GB、Windows E: 页面文件不少于 64 GB，并在 D: 模型源与 E: WSL 运行目录都保留至少 50 GB 空间。下载脚本固定 Hugging Face revision，并不会把 Token 写入脚本或仓库。如经运维确认主机容量足够但页面文件检查不可读，可一次性显式设置 `BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE=1`；这会在预检日志中留下记录，不能作为常规默认配置。

受控下载器使用 `curl` 对同一 Hugging Face revision 做可续传 HTTP Range 传输；运行 `sudo ./start_minimax_h3_download.sh` 会为四个固定组件建立独立的 transient systemd 服务。每段落盘前都会核对 `Content-Range` 与长度；SSH 断开或部分响应关闭不会覆盖已完成数据。默认段为 1MiB，用 `sudo ./start_minimax_h3_download.sh --status` 查看精确进度。不要删除 `D:\model\MiniMax-H3` 中的未完成文件，后续运行会从已有字节继续。默认源为 `https://huggingface.co`；如已对同一固定 revision 做过 Range 与 SHA-256 验证，可通过 systemd 环境变量 `BRMMEDIA_H3_BASE_URL` 临时指定镜像或代理源，下载完成前不得改变 revision、文件清单或校验值。

若已验证代理可稳定返回更大且完整的范围响应，可在 `brmmedia-h3-stage.service` 的 systemd drop-in 中显式设置 `BRMMEDIA_H3_CHUNK_BYTES`（例如 `8388608`）；该值会传给每个 transient worker。不要在没有范围响应验证时提高默认 1MiB 值。

每个 worker 对同一范围只做一次短重试（可用 `BRMMEDIA_H3_CURL_RETRIES` / `BRMMEDIA_H3_CURL_RETRY_DELAY` 调整），大范围响应仍被拒绝时会自动把该组件后续分段减半，最低至 `BRMMEDIA_H3_MIN_CHUNK_BYTES`（默认 1MiB）；已校验前缀不会被重新下载或覆盖。

下载段始终先写入同目录的临时 `.chunk.*` 文件；只有范围与字节数通过检查才会 append 到模型文件。worker 重启时会删除该**组件**遗留的临时段（不会删除已校验前缀），并在收到停止信号时清理当前临时段，避免反复调整网络时占用 D: 空间。

若需要在 SSH 断开后继续下载，优先执行 `sudo ./start_minimax_h3_download.sh`。它为四个文件创建低优先级临时 systemd 下载单元；用 `sudo ./start_minimax_h3_download.sh --status` 查询每个文件的状态与字节数，失败后直接再次运行同一命令即可续传。

`wait_import_minimax_h3_models.sh` 只会等待四个精确字节大小、导入到 E: 的 ComfyUI 模型目录并执行内容校验；它**不会**改 `BRMMEDIA_VIDEO_ENGINE`、升级 ComfyUI 或重启服务。因此它可以在当前 LTX 生产服务继续运行时安全执行。

若要在 WSL 或宿主机重启后自动恢复此准备过程，安装服务后启用 `sudo systemctl enable --now brmmedia-h3-stage`。它以 root 身份重新创建下载单元，再以 `brm` 身份等待并导入；所有权重已完成时会直接跳过下载。该服务只准备模型，不会自动激活 H3。

H3 未验收时保持 `BRMMEDIA_VIDEO_ENGINE=ltx23`（默认）；需回退 ComfyUI 时，在停止 `baorong-backend` 后执行 `./rollback_minimax_h3_comfyui.sh`，再启动服务并运行 `sudo brmmedia-verify-runtime`。

确认没有媒体任务运行、H3 权重导入完成后，先停止 `baorong-backend`，以候选目录运行
`prepare_minimax_h3_canary.sh` 与 `start_minimax_h3_canary.sh`。Canary 使用独立的
ComfyUI 工作副本、Gradio `127.0.0.1:9001` 和 REST `127.0.0.1:9101`，不会被 Nginx
公开；其输出也与生产历史素材隔离。先在 Canary 上完成文生视频、图生视频各三次
preview、各一次 quality，以及 `--restart-recovery` 的任务恢复校验，再停止 Canary。
只有全部通过，才以候选目录运行 `install_ubuntu_systemd_services.sh brm <candidate-root>`
和 `activate_minimax_h3.sh` 执行生产切换。激活脚本会拒绝 systemd 仍指向旧
`/srv/brmmedia/app` 的切换，并重做 H3 资源预检、A4000 Qwen `/v1/models` 检查和所有
ComfyUI 工作流/自定义节点校验。若需要沿用已授权的页面文件例外，必须在本次命令前显式
设置 `BRMMEDIA_H3_ALLOW_PAGEFILE_OVERRIDE=1`。

`activate_minimax_h3.sh` 不仅要求 systemd 已启动，还会在 420 秒内等待候选 Gradio 与
ComfyUI 回环健康端点均返回 HTTP 200；超时、后端退出或其中任一端点未就绪时会自动还原
候选引擎配置和 ComfyUI 快照。

候选运行目录包含 `accept_minimax_h3_video.sh`：不带参数时执行一组 T2V/I2V preview 冒烟和 MP4 音视频流校验；`sudo ./accept_minimax_h3_video.sh --full --restart-recovery` 会执行每类 3 个 preview 与两个 quality 任务，并在首个完成任务后重启指定 backend，再通过 REST 查询、下载和 `ffprobe` 验证该任务仍可恢复。Canary 必须设置其回环 API、backend unit 和 Gradio 健康地址环境变量，如本文开头示例；普通生产端点不应在 H3 未验收时承担 burn-in。

受控升级会将旧 ComfyUI commit、工作区状态和后端 Python 依赖版本冻结到 `runtime-locks/comfyui-h3-backups/`；升级过程中失败会自动恢复旧 commit 与已冻结的 Python 包版本。

服务器上已有旧工作区出现未提交修改时，不要强行 `git pull` 或覆盖 `/srv/brmmedia/app`。先从干净 Git clone 执行 `sudo ./stage_h3_runtime_release.sh /mnt/d/brmmedia/releases/h3-<commit>`，它会在 E: 创建独立运行副本并复用已验证的 venv、配置、任务历史与素材输出；旧运行目录保留为回退目标。

恢复服务后，先在 WSL 内执行以下无推理预检；它会逐一检查项目工作流需要的 ComfyUI 节点，缺少的节点会按工作流名称列出：

```bash
cd /srv/brmmedia/app
python3 validate_comfy_workflows.py --url http://127.0.0.1:8188
```

预检全部显示 `READY` 后，先完成 Qwen 连续 10 次对话；候选运行目录中的 `accept_qwen_vllm.sh` 会调用仅回环可见的 `/v1/models` 和 `/v1/chat/completions`，并在当前候选运行目录共享的 `ubuntu-backend-deploy/outputs/acceptance/` 留下不含凭据的结果文件：

```bash
sudo -u brm ./accept_qwen_vllm.sh
```

再分别完成 H3 文生视频、图生视频各 3 次 `preview` 任务；通过后验证 `quality`。最后在**已切换的生产候选运行目录**执行下面的完整 A5000 媒体回归；它先运行 Z-Image、ACE-Step 与 IndexTTS2 冒烟，再通过仅回环可见 REST API 验证 FLUX 图片编辑、LTX 首尾帧和数字人任务可完成、下载，且视频含视频流。该脚本会创建验收素材和不含凭据的报告，不能在 Canary 期间执行：

```bash
sudo ./accept_media_regression.sh
```

H3 默认 `preview`，时长 4–15 秒，并会输出带原生音频的 MP4。

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

该命令检查虚拟环境、服务、Gradio/ComfyUI/Qwen 内部 HTTP、关键 Gradio 组件和 API、当前工作流目录中全部流程的节点类型、ComfyUI 锁定 commit、Qwen 回环绑定以及受控健康检查。应以 `RESULT=PASS` 结束；它是**结构与服务就绪检查**，不是 H3 推理生产验收的替代品。H3 切换还必须保留 Qwen 10 次对话、H3 `accept_minimax_h3_video.sh --full` 和其余媒体回归的实测记录。

## 局域网 API 任务状态

所有 `submit_workflow_1` 到 `submit_workflow_8` API 在任务进入工作台队列后返回 `task_id`，这表示“已受理”而非模型生成已结束。使用同一认证入口调用 `task_status` 并传入该 ID，可获得 `queued`、`running`、`completed`、`cancelled` 或 `failed` 状态、时间和输出文件名；响应不会暴露服务器绝对路径。

仓库的 `call_gradio_api.py` 用于调用 Gradio API 并读取 handler 的 SSE 完成事件。对异步工作流，请读取其返回的 `task_id` 后再轮询 `task_status`，不要把首次 `COMPLETE` 误认为推理完成。
