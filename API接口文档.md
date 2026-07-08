# YZY 启动器 API 接口文档

> 文生图 / 图片编辑 / 文生视频 / 图生视频 / 首尾帧视频 / 数字人语音驱动
>
> **版本**：基于 Gradio 6.17 · **更新日期**：2026-06-15

---

## 0. 基础信息

| 项目 | 值 |
|------|----|
| **Base URL** | `http://<服务器IP>:9000` |
| 本机调用 | `http://127.0.0.1:9000` |
| 局域网调用 | `http://192.168.1.118:9000`（按实际 IP 替换） |
| **鉴权** | ❌ 无（局域网内任意可访问，**勿暴露公网**） |
| **协议** | HTTP + SSE（Server-Sent Events） |
| 交互式文档 | `GET /openapi.json`（OpenAPI 3.0 标准） |
| API 元信息 | `GET /gradio_api/info` |

### 核心架构（必读）

本服务采用 **异步任务队列** 模型，**所有生成都不是同步返回结果**：

```
提交任务 ──► 进入队列 ──► 后台 worker 串行执行 ──► 结果落盘到 outputs/
   │
   └─ 立即返回（只代表"已受理"，不含生成结果）
```

因此前端的调用范式是：

1. **（可选）上传素材** → 拿到服务器文件名
2. **提交任务** → 拿到"已受理"确认
3. **轮询队列状态**（`render_queue`）→ 直到任务变成「已完成」
4. **下载产物文件** → 通过文件 URL 拉取

---

## 1. Gradio 6 统一调用协议

所有 `submit_*` / `render_queue` 等命名端点，都遵循 **两步式调用**：

### 第 1 步：发起调用，拿 event_id

```
POST /gradio_api/call/{端点名}
Content-Type: application/json

{
  "data": [ 参数1, 参数2, ... ]     // 按端点参数顺序的数组
}
```

**响应**：
```json
{ "event_id": "abc123def456..." }
```

### 第 2 步：用 event_id 拿结果（SSE 流）

```
GET /gradio_api/call/{端点名}/{event_id}
Accept: text/event-stream
```

**响应**是一个 SSE 流，典型事件：

```
event: generating
data: null

event: complete
data: [返回值1, 返回值2, ...]
```

- `complete` 事件的 `data` 是 JSON 数组，对应该端点的返回值列表。
- 详见 [Gradio 官方 API 文档](https://www.gradio.app/guides/querying-gradio-apps-with-via-api)。

---

## 2. 业务端点一览

| 端点名 | 功能 | 参数 |
|--------|------|------|
| `/submit_workflow_1` | 🖼️ **文生图**（Z-Image） | `prompt`, `size`, `batch` |
| `/submit_workflow_2` | ✏️ **图片编辑**（FLUX.2-klein） | `prompt`, `input_filename`（需先上传图） |
| `/submit_workflow_3` | 🎬 **文生视频**（LTX2.3） | `prompt`, `size`, `seconds` |
| `/submit_workflow_4` | 🎬 **图生视频**（LTX2.3） | `prompt`, `input_filename`, `seconds` |
| `/submit_workflow_5` | 🎬 **首尾帧视频**（LTX2.3） | `prompt`, `input_filename1`, `input_filename2`, `seconds` |
| `/submit_workflow_6` | 🎤 **数字人-语音驱动**（LTX2.3） | `prompt`, `image`, `audio`, `duration`, `size` |
| `/submit_workflow_7` | 🔊 **语音克隆**（IndexTTS2） | `prompt`, `ref_audio`, `temperature` |
| `/submit_workflow_8` | 🎵 **音乐生成**（ACE-Step 1.5） | `tags`, `lyrics`, `duration`, `bpm`, `language`, `model` |
| `/render_queue` | 📋 **查询队列状态** | 无 |
| `/interrupt` | ⏹️ **中断当前任务** | 无 |
| `/clear_pending` | 🧹 **清空排队任务** | 无 |
| `/check_health` | 💓 **健康检查** | 无 |

---

## 3. 素材上传（关键前置步骤）

> 图生视频 / 图片编辑 / 首尾帧 / 数字人 这些端点需要「服务器上的文件名」。
> 必须先上传素材，拿到 ComfyUI 返回的文件名，再用于提交任务。

### 3.1 上传文件到 Gradio 临时区

```
POST /upload
Content-Type: multipart/form-data

字段名: files      （文件二进制）
```

**响应**（数组）：
```json
[
  {
    "path": "C:\\Users\\xxx\\AppData\\Local\\Temp\\gradio\\xxx\\photo.png",
    "url": null,
    "size": 123456,
    "orig_name": "photo.png",
    "mime_type": "image/png",
    "is_stream": false
  }
]
```
取其中的 **`path`** 备用。

### 3.2 注册到 ComfyUI（图片素材）

调用 `/on_ref_upload`，把上一步的临时路径转成 ComfyUI 能识别的文件名：

```
POST /gradio_api/call/on_ref_upload
{ "data": [{ "path": "<上面拿到的 path>", "url": null, "size": ..., "orig_name": "...", "mime_type": "image/png", "is_stream": false }] }
```

**complete 返回**：`["<ComfyUI文件名>"]`，例如 `["photo.png"]`。

### 3.3 注册到 ComfyUI（音频素材）

调用 `/on_audio_upload`，**返回两个值**（文件名 + 时长）：

```
POST /gradio_api/call/on_audio_upload
{ "data": [{ "path": "...", "url": null, "orig_name": "voice.wav", "mime_type": "audio/wav", ... }] }
```

**complete 返回**：`["<ComfyUI文件名>", <时长秒>]`，例如 `["voice.wav", 3.5]`。

---

## 4. 各生成端点详解

### 4.1 🖼️ 文生图 `/submit_workflow_1`

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 提示词 |
| `size` | string（枚举） | 分辨率，见下表 |
| `batch` | integer | 生成数量（1–4） |

**size 枚举值**（注意是中文「×」或字母 x）：
```
512 × 512, 768 × 768, 1024 × 1024, 2048 × 2048,
1024 × 768, 768 × 1024, 1344 × 768, 768 × 1344,
1920 × 1080, 1080 × 1920, 2048 × 1536, 1536 × 2048,
2560 × 1440, 1440 × 2560, 3840 × 2160, 2160 × 3840
```

**请求示例**：
```bash
curl -X POST http://192.168.1.118:9000/gradio_api/call/submit_workflow_1 \
  -H "Content-Type: application/json" \
  -d '{"data": ["一个漂亮的女生在校园散步", "1024 × 1024", 1]}'
# → {"event_id": "xxx"}
```

---

### 4.2 ✏️ 图片编辑 `/submit_workflow_2`

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 编辑指令 |
| `input_filename` | string | ComfyUI 文件名（**需先 3.2 上传**） |

```json
{ "data": ["把背景换成海边", "photo.png"] }
```
> prompt 或 input_filename 为空会报错。

---

### 4.3 🎬 文生视频 `/submit_workflow_3`

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 提示词 |
| `size` | string（枚举） | 同 4.1 |
| `seconds` | integer | 视频时长（秒，2–360） |

```json
{ "data": ["一个亚洲女孩在花丛中散步", "768 × 1024", 5] }
```

---

### 4.4 🎬 图生视频 `/submit_workflow_4`

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 提示词 |
| `input_filename` | string | 参考图（**需先 3.2 上传**） |
| `seconds` | integer | 时长（2–360） |

```json
{ "data": ["让人物微笑并转头", "photo.png", 5] }
```

---

### 4.5 🎬 首尾帧视频 `/submit_workflow_5`

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 提示词 |
| `input_filename1` | string | **首帧**图片（先上传） |
| `input_filename2` | string | **尾帧**图片（先上传） |
| `seconds` | integer | 时长（2–360） |

```json
{ "data": ["平滑过渡", "start.png", "end.png", 5] }
```

---

### 4.6 🎤 数字人-语音驱动 `/submit_workflow_6`

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 口型/动作提示词 |
| `image` | string | 人物图片文件名（先 3.2 上传） |
| `audio` | string | 音频文件名（先 3.3 上传） |
| `duration` | number | 音频时长秒（由 3.3 返回） |
| `size` | string（枚举） | 同 4.1 |

```json
{ "data": ["自然说话", "person.png", "voice.wav", 3.5, "768 × 1024"] }
```

---

### 4.7 🔊 语音克隆 `/submit_workflow_7`

| 参数 | 类型 | 说明 |
|------|------|------|
| `prompt` | string | 要合成的文本 |
| `ref_audio` | string | 参考音频文件名（**需先 3.3 上传**） |
| `temperature` | number | 采样温度，默认 `0.8` |

```json
{ "data": ["你好，这是一段语音克隆测试。", "voice.wav", 0.8] }
```

---

### 4.8 🎵 音乐生成 `/submit_workflow_8`

| 参数 | 类型 | 说明 |
|------|------|------|
| `tags` | string | 音乐风格标签，如 `pop, upbeat, electronic` |
| `lyrics` | string | 歌词，可留空生成纯音乐 |
| `duration` | number | 时长秒，默认 `30` |
| `bpm` | integer | BPM，默认 `120` |
| `language` | string | 歌词语言，如 `zh` / `en` / `ja` / `unknown` |
| `model` | string | ACE-Step 模型版本：`turbo` / `base` / `sft` |

```json
{ "data": ["pop, upbeat, electronic", "今晚我们追着星光奔跑", 30, 120, "zh", "turbo"] }
```

> `turbo` 使用 8 步、速度较快；`base` / `sft` 使用 50 步、质量更高但耗时更久。

---

## 5. 查询与控制

### 5.1 📋 查询队列状态 `/render_queue`

```
POST /gradio_api/call/render_queue
{ "data": [] }
```

**complete 返回**（4 个值）：

```json
[
  "ComfyUI:在线　｜　排队 1　｜　处理中 0　｜　完成 2　｜　失败 0",   // 概览文本
  "| 任务名称 | 状态 | 提交时间 | 耗时 | 备注 |\n|...|",             // Markdown 表格
  [                                                                  // 已完成产物的 FileData 数组
    { "path": "...\\outputs\\任务_xxx.png", "url": "/file=...", "orig_name": "...", ... }
  ],
  [                                                                  // 已完成音频的 FileData 数组
    { "path": "...\\outputs\\任务_xxx.mp3", "url": "/file=...", "orig_name": "...", ... }
  ]
]
```

**状态枚举**：`排队中` / `处理中` / `已完成` / `失败`（表格中带圆点图标 ⚪🔵🟢🔴）

> ⚠️ 这个端点返回的是 **Markdown 文本**（为前端表格设计）。如需结构化数据，需自行解析该 Markdown。
> **建议前端定时轮询**（如每 1.5–2 秒一次），直到目标任务状态变为「已完成」。

### 5.2 ⏹️ 中断当前任务 `/interrupt`

```
POST /gradio_api/call/interrupt
{ "data": [] }
```
返回 `["已发送中断信号。"]`。

### 5.3 🧹 清空排队任务 `/clear_pending`

```
POST /gradio_api/call/clear_pending
{ "data": [] }
```
返回 `["已清空排队任务 N 个。"]`（不影响正在执行的）。

### 5.4 💓 健康检查 `/check_health`

```
POST /gradio_api/call/check_health
{ "data": [] }
```
ComfyUI 在线时无异常；掉线时返回错误事件。

---

## 6. 产物文件下载

生成的图片/视频/音频保存在服务器 `outputs/` 目录。获取方式有两种：

### 方式 A：通过 render_queue 返回的 url

`render_queue` 第 3 个返回值是图片/视频数组，第 4 个返回值是音频数组。每个文件对象含 `url` 字段，形如：

```
/file=C%3A%5C...%5Coutputs%5C任务_xxx.png
```

直接拼接 Base URL 即可下载：

```
http://192.168.1.118:9000/file=<url编码后的完整路径>
```

### 方式 B：直按文件名访问（Gradio 6）

```
http://192.168.1.118:9000/gradio_api/file=outputs/任务_xxx.png
```

> 产物文件名格式：`任务_<工作流中文名>_<时间戳>_<随机数>.<ext>`
> 多产物时追加 `_1`、`_2`……

---

## 7. 前端完整调用示例（JavaScript）

```javascript
const BASE = 'http://192.168.1.118:9000';

// 工具：发起 Gradio 调用并等 complete 事件
async function callGradio(endpoint, data, { timeout = 3600000 } = {}) {
  // 1. 提交，拿 event_id
  const res = await fetch(`${BASE}/gradio_api/call/${endpoint}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ data }),
  }).then(r => r.json());

  const { event_id } = res;
  if (!event_id) throw new Error('未拿到 event_id');

  // 2. 读 SSE 流，等 complete
  const evtRes = await fetch(`${BASE}/gradio_api/call/${endpoint}/${event_id}`, {
    headers: { Accept: 'text/event-stream' },
  });
  const reader = evtRes.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const start = Date.now();

  while (Date.now() - start < timeout) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // 按空行拆分事件块
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop();
    for (const block of blocks) {
      const lines = block.split('\n');
      const ev = (lines.find(l => l.startsWith('event:')) || '').slice(6).trim();
      const ds = (lines.find(l => l.startsWith('data:')) || '').slice(5).trim();
      if (ev === 'complete') return JSON.parse(ds);   // 返回值数组
      if (ev === 'error')   throw new Error(ds);
    }
  }
  throw new Error('超时');
}

// ===== 场景 1：文生图 =====
const result = await callGradio('submit_workflow_1', [
  '一个漂亮的女生在校园散步', '1024 × 1024', 1,
]);
console.log('已提交', result);

// ===== 场景 2：图生视频（含上传）=====
// (1) 上传图片到 Gradio
const fd = new FormData();
fd.append('files', fileInput.files[0]);
const uploaded = await fetch(`${BASE}/upload`, { method: 'POST', body: fd }).then(r => r.json());
// (2) 注册到 ComfyUI
const [comfyName] = await callGradio('on_ref_upload', [uploaded[0]]);
// (3) 提交任务
await callGradio('submit_workflow_4', ['让人物微笑并转头', comfyName, 5]);

// ===== 场景 3：轮询结果 =====
async function waitForResult() {
  while (true) {
    const [summary, table, gallery, audios] = await callGradio('render_queue', []);
    console.log(summary);
    const files = [...gallery, ...audios];
    if (files.length > 0) {
      return files.map(f => BASE + f.url);   // 产物 URL 列表
    }
    await new Promise(r => setTimeout(r, 2000));
  }
}
```

---

## 8. 限制与注意事项

| 项 | 说明 |
|----|------|
| **串行执行** | 后台只有 1 个 worker，任务**串行**处理，多个任务会排队。 |
| **无任务 ID 返回** | submit 端点不返回任务 ID，只能靠 `render_queue` 的任务名（含时间戳）匹配。 |
| **结果非结构化** | `render_queue` 返回 Markdown 文本，需前端自行解析（建议按「已完成」行匹配文件名）。 |
| **无鉴权** | 任何能访问该 IP:端口的人都可调用。**生产环境务必**加反向代理 + 鉴权 + 网络隔离。 |
| **大文件/长视频** | 高分辨率 + 长时长会消耗大量显存，可能 OOM。建议从低参数试起。 |
| **CORS** | 默认 Gradio 不限制跨域，前端可直接 fetch。 |
| **中断** | `/interrupt` 只中断当前正在跑的任务，不清空队列。 |

---

## 9. 快速验证（curl）

```bash
# 健康检查
curl -X POST http://192.168.1.118:9000/gradio_api/call/check_health \
  -H "Content-Type: application/json" -d '{"data":[]}'

# 查看所有可用端点
curl http://192.168.1.118:9000/gradio_api/info | jq

# 文生图（提交）
curl -X POST http://192.168.1.118:9000/gradio_api/call/submit_workflow_1 \
  -H "Content-Type: application/json" \
  -d '{"data":["一只猫","512 × 512",1]}'

# 音乐生成（提交）
curl -X POST http://192.168.1.118:9000/gradio_api/call/submit_workflow_8 \
  -H "Content-Type: application/json; charset=utf-8" \
  -d '{"data":["pop, upbeat, electronic","今晚我们追着星光奔跑",30,120,"zh","turbo"]}'
```

## 10. 实测验证记录（2026-06-15）

以下路径已在运行中的服务上实测通过：

- ✅ `POST /gradio_api/call/check_health` → 返回 `event_id`，SSE `complete: []`（ComfyUI 在线）
- ✅ `POST /gradio_api/call/submit_workflow_1`（文生图）→ 返回 `event_id`，任务进入队列
- ✅ `POST /gradio_api/call/render_queue` → 返回概览文本 + Markdown 表格 + 画廊数组 + 音频数组
- ✅ `GET /gradio_api/call/{endpoint}/{event_id}` → SSE 流返回 `complete` 事件
- ✅ `POST /gradio_api/call/submit_workflow_8`（音乐生成）→ 端点已接入任务队列，产物保存为 `.mp3`

> ⚠️ 注意：POST 中文提示词时，**请求体必须用 UTF-8 编码字节**发送（`Content-Type: application/json; charset=utf-8`），否则会报 `error parsing the body`。前端 fetch 默认即 UTF-8，无需特殊处理。

---

*文档结束 · 如有疑问，对照 `H:\yzylauncher-win-ltx23\win-unpacked\python\webui.py` 源码确认。*
