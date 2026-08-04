# BRMMedia 局域网 API

> **更新日期：2026-08-04**
> 本文以运行中服务的 `GET /gradio_api/info` 为准。运行时验收会确保公开 API 恰好为八个工作流提交端点和 `task_status`。

## 1. 入口与认证

| 项目 | 当前规则 |
| --- | --- |
| 局域网 Base URL | `http://<Windows-LAN-IP>/` |
| 认证 | Nginx HTTP Basic Auth；账号与密码见私有 `DEPLOYMENT.md`，不得写入 Git 或客户端源码 |
| API 元信息 | `GET /gradio_api/info` |
| Qwen OpenAI 兼容接口 | `http://<Windows-LAN-IP>/qwen/v1` |
| 内部端口 | Gradio 9000、ComfyUI 8188、Qwen 8000 仅 WSL 回环监听，**不可直接访问或开放** |

Windows 的 DHCP 地址可能变化。以 `ipconfig` 物理网卡 IPv4 或路由器的 DHCP 固定租约为准，不要把旧地址写进程序。

所有 API 请求均经过局域网入口的 Basic Auth。例如：

```bash
export BRM_BASE='http://<Windows-LAN-IP>'
export BRM_USER='brmadmin'
# 从私有 DEPLOYMENT.md 或密码管理器读取，不要把密码写进 shell 历史或仓库。
read -rs BRM_PASSWORD
echo

curl --user "$BRM_USER:$BRM_PASSWORD" "$BRM_BASE/gradio_api/info"
```

## 2. 调用模型：受理与完成是两件事

工作流端点是异步的。处理器 SSE 的 `COMPLETE` 只代表任务已经进入工作台队列，**不代表模型已生成完成**。响应中包含：

```json
{
  "task_id": "<任务 ID>",
  "task_name": "任务_文生图_…",
  "workflow": "image_z_image_turbo",
  "state": "accepted",
  "queue_position": 1
}
```

随后使用同一个 `task_id` 查询 `/task_status`，直到状态成为 `completed`、`cancelled` 或 `failed`。

为避免手工处理 Gradio SSE 细节，推荐使用仓库工具 [`call_gradio_api.py`](call_gradio_api.py)。它会先读取运行中 `/gradio_api/info`，按真实参数名称调用端点，并输出 `COMPLETE=…`。

```bash
# 提交一个轻量文生图任务（仅表示已受理）
python3 call_gradio_api.py submit_workflow_1 \
  '["一只坐在窗边的橘猫", "512 × 512", 1]' \
  --base-url "$BRM_BASE" \
  --timeout 60

# 将上一步 COMPLETE JSON 中的 task_id 替换进来，轮询真实状态
python3 call_gradio_api.py task_status '["<task_id>"]' \
  --base-url "$BRM_BASE" \
  --timeout 60
```

`call_gradio_api.py` 是内部/服务器维护工具；从局域网运行时，调用方仍须为 HTTP 客户端提供 Basic Auth。浏览器用户直接使用工作台即可。

## 3. 公开端点

下表是稳定的局域网自动化接口。未列出的 UI 事件（全局设置、密码修改、队列清空/中断、素材预览、上传状态等）均为浏览器私有事件，不能作为外部 API 依赖。

| 端点 | 参数 | 说明 |
| --- | --- | --- |
| `/submit_workflow_1` | `prompt`, `size`, `batch` | Z-Image 文生图 |
| `/submit_workflow_2` | `prompt`, `input_filename` | FLUX.2-klein 图片编辑 |
| `/submit_workflow_3` | `prompt`, `size`, `seconds` | LTX2.3 文生视频 |
| `/submit_workflow_4` | `prompt`, `input_filename`, `seconds` | LTX2.3 图生视频 |
| `/submit_workflow_5` | `prompt`, `input_filename1`, `input_filename2`, `seconds` | LTX2.3 首尾帧视频 |
| `/submit_workflow_6` | `prompt`, `image`, `audio`, `duration`, `size` | LTX2.3 单图数字人-语音驱动 |
| `/submit_workflow_7` | `prompt`, `ref_audio`, `temperature` | IndexTTS2 语音克隆 |
| `/submit_workflow_8` | `tags`, `lyrics`, `duration`, `bpm`, `language`, `model` | ACE-Step 音乐生成 |
| `/task_status` | `task_id` | 查询一个工作台任务的安全状态 |

### 参数约束

- `size` 使用界面下拉值，例如 `512 × 512`、`768 × 1024`、`1024 × 1024`。以 `/gradio_api/info` 或工作台当前下拉框为准。
- `batch` 为 `1`–`4`；视频 `seconds` 为 `2`–`360`。长视频和数字人任务建议常规模式下并发保持为 `1`。
- 音乐 `language` 可用 `zh`、`en`、`ja`、`ko`、`fr`、`de`、`es`、`ru`、`unknown`；`model` 为 `turbo`、`base`、`sft`。
- 图片/音频相关工作流的文件名必须已经在 ComfyUI 输入目录注册。稳定的外部文件摄取 API 尚未发布；请通过工作台浏览器上传素材后再提交，或由受控的服务器维护流程预置输入文件。不要向接口传入宿主机绝对路径。

## 4. 任务状态

`/task_status` 返回的 `state`：

| state | 含义 |
| --- | --- |
| `queued` | 已受理，等待工作台 worker |
| `running` | 正在由工作台处理 |
| `completed` | 已生成，`output_files` 只给出安全的文件名 |
| `cancelled` | 已中断；若操作员清空排队任务，错误说明为“队列已清空” |
| `failed` | 处理失败，`error` 给出简要原因 |
| `not_found` | ID 不存在，或超出已完成任务保留上限 |

返回不会包含服务器绝对路径。已完成素材请从工作台的图片/视频大预览、音频试听和下载控件获取；不要拼接或猜测内部文件路径。

## 5. Qwen API

Qwen 使用单独的 OpenAI 兼容入口：

```bash
curl --user "$BRM_USER:$BRM_PASSWORD" "$BRM_BASE/qwen/v1/models"

curl --user "$BRM_USER:$BRM_PASSWORD" \
  "$BRM_BASE/qwen/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "qwen35-4b-awq",
    "messages": [{"role": "user", "content": "请用一句话介绍自己。"}],
    "temperature": 0.7
  }'
```

常规模式下 Qwen 使用 GPU0；切换至高显存视频模式时 Qwen 会停止，这是预期行为。工作台的“Qwen 大模型”标签页可用于浏览器内流式验证。

## 6. 运维边界

- 公开 API 不提供“中断某个正在执行任务”：ComfyUI 的中断为全局动作，必须在工作台中由操作员确认，避免误伤其他请求。
- 清空排队任务会把未开始的任务保留为 `cancelled`，因此调用方可继续用原 `task_id` 查询终态。
- `brmmedia-verify-runtime` 会检查公开端点集合、服务、内部 API、工作流节点/静态资产、健康 timer 和运行 Profile。部署或恢复后应以 `RESULT=PASS` 作为验收依据。

## 7. 变更约定

新公开端点必须同时满足：

1. 在 `webui.py` 显式通过 `gr.api()` 声明；
2. 纳入运行时 API 合约检查；
3. 更新本文、`WSL_TEST_DEPLOY.md` 和私有 `DEPLOYMENT.md`；
4. 通过本地回归、GitHub Actions 和服务器运行时验收。
