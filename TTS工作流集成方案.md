# TTS（文字转语音 / 语音克隆）工作流集成方案

> 在 yzylauncher 现有架构上新增"文字转语音 + 声音克隆"能力。
> 推荐模型：**IndexTTS2**（B站）｜ 推荐集成路线：**ComfyUI 原生节点**
> 编写日期：2026-06-15

---

## 1. 环境评估结论

| 项 | 现状 | 是否满足 |
|----|------|---------|
| GPU | RTX A5000 24GB（空闲 ~19GB） | ✅ IndexTTS2 需 6-8GB，充裕 |
| ComfyUI Python | python_embeded，torch 2.12 + torchaudio 2.11 + transformers 5.9 | ✅ |
| 框架扩展性 | webui.py 的 `WORKFLOW_BUILDERS` 插件式设计，新增工作流只需 4 步 | ✅ 极友好 |
| 已有 TTS 能力 | 无（MelBandRoFormer 只做音频分离/降噪） | ❌ 需新增 |

**结论：硬件与框架均已就绪，缺的只是 TTS 模型 + 对应 ComfyUI 节点。**

---

## 2. 模型选型对比

| 模型 | 声音克隆 | 中文质量 | 显存 | 情感控制 | ComfyUI 节点 | 推荐度 |
|------|---------|---------|------|---------|-------------|--------|
| **IndexTTS2**（B站）✅选定 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | 6GB | ✅ 情感权重 | 3 个可选节点 | ⭐⭐⭐⭐⭐ |
| CosyVoice 2（阿里） | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ | ~6GB | ✅ 情感标签 | 单一社区节点 | ⭐⭐⭐⭐ |
| F5-TTS | ✅ 零样本 | ⭐⭐⭐⭐ | ~5GB | ❌ | 有 | ⭐⭐⭐⭐ |
| GPT-SoVITS | ✅ 克隆 | ⭐⭐⭐⭐⭐ | ~4GB | ❌ | 有 | ⭐⭐⭐⭐ |
| Fish Speech 1.5 | ✅ 克隆 | ⭐⭐⭐⭐ | ~7GB | ❌ | 有 | ⭐⭐⭐⭐ |
| ChatTTS | ❌ 无克隆 | ⭐⭐⭐⭐⭐ | ~3GB | ❌ | 有 | ⭐⭐⭐ |

### 为什么选 IndexTTS2
- **当前开源声音克隆最强之一**，中文自然度顶级
- **情感权重参数**（V2 新增）：可精细调节克隆音色相似度与情感表达，做内容（数字人、视频配音）价值极大
- **显存友好**：优化版节点 6GB 可用；官方推荐 8GB 起步。你的 24GB 绰绰有余
- **多人对话 / 长文本**原生支持
- ComfyUI **有 3 个可选节点**，容错空间大（chenpipi0807 版 / 优化版 / TTS Audio Suite）
- 与现有的"单图数字人-语音驱动"工作流（workflow 6）天然衔接，可形成
  **文本 → 语音克隆 → 数字人视频** 完整链路

---

## 3. 集成路线对比

| 维度 | A. ComfyUI 原生节点（推荐） | B. 独立 TTS 服务 |
|------|---------------------------|-----------------|
| 架构一致性 | ✅ 完美复用任务队列/产物/轮询 | ❌ 需新建通信层 |
| 改动量 | 中 | 大 |
| 效果可控性 | 取决于节点质量 | 自己完全可控 |
| 前端 API | 自动暴露 `/submit_workflow_7` | 需手写新端点 |
| 维护成本 | 低 | 中 |

**选定：路线 A**。原因：本项目所有模态（图/视频/数字人）都走 ComfyUI，TTS 走同一路径能保持架构统一，且前端 API 自动获得，几乎零成本。

---

## 4. 详细实现步骤

### 第 1 步：安装 IndexTTS ComfyUI 节点

IndexTTS2 有多个 ComfyUI 节点可选，**按优先级**：

| 节点 | 特点 | 安装 |
|------|------|------|
| **chenpipi0807/ComfyUI-Index-TTS**（首选） | 官方对接，已支持 IndexTTS-2，4 个核心节点，带 `TTS2.json` 基础工作流 | git clone |
| 优化版（6G显存/不限人数） | 显存优化，支持 transformers 4.56.1 | 见对应仓库 |
| TTS Audio Suite | 含情感控制等高级功能，可通过 ComfyUI Manager 安装 | Manager 搜索 |

推荐先装首个：

```bash
cd "H:\yzylauncher-win-ltx23\win-unpacked\python\ComfyUI_windows_portable\ComfyUI\custom_nodes"
git clone https://github.com/chenpipi0807/ComfyUI-Index-TTS.git
```

> 若无 git，可直接下载 zip 解压到此目录。
> 安装后通过 ComfyUI Manager 检查依赖更新。

### 第 2 步：安装节点依赖（在 python_embeded 环境内）

ComfyUI 用的是独立的 `python_embeded`，必须用它装依赖：

```bash
EMB="H:\yzylauncher-win-ltx23\win-unpacked\python\ComfyUI_windows_portable\python_embeded\python.exe"
$EMB -m pip install -r "ComfyUI-Index-TTS\requirements.txt"
# 通常需要追加：
$EMB -m pip install soundfile librosa einops   # 音频读写与张量操作
```

> ⚠️ 兼容性预检：装完先确认与 torch 2.12 / transformers 5.9 / CUDA 13 无冲突。
> 若 transformers 版本要求不符，优先以 ComfyUI 现有版本为准，测试节点能否跑通。

### 第 3 步：下载 IndexTTS2 模型权重

模型放到 ComfyUI 的 models 目录。IndexTTS2 权重约 2-4GB：

```
ComfyUI_windows_portable\ComfyUI\models\
  └── tts\                         ← 新建此目录（或按节点 README 指定位置）
      └── IndexTTS-2\              ← 模型文件
```

模型来源：HuggingFace（`HF_HOME` 已配 hf-mirror 镜像，会自动加速）
```
https://huggingface.co/IndexTeam/IndexTTS2
```

> 具体文件清单和放置路径，以所装节点 README 为准。

### 第 4 步：构建 ComfyUI 工作流 JSON

在 ComfyUI 界面（http://127.0.0.1:8188）里：
1. 添加 IndexTTS2 的 4 个核心节点（加载模型 / 文本 / 参考音频 / 合成保存）
2. 搭建：
```
[加载 IndexTTS2 模型]
        │
[文本输入]──►[IndexTTS2 合成]──►[保存音频]
                  ▲
   [参考音频上传]──┘  （克隆模式；可加情感权重参数）
```
3. 在设置里开启 **Enable API format**，导出为 API 格式 JSON
4. 保存为：
```
win-unpacked\python\workflows\TTS-语音克隆.json
```

> 参考 webui.py 现有的 build_workflow_2~6，每个节点在 JSON 里有一个 ID（如 "14"、"76"），
> 你只需在 builder 里把对应 ID 的 inputs 替换成运行时参数。
> 节点自带的 `./workflow/TTS2.json` 可作为起点。

### 第 5 步：在 webui.py 新增 builder（照抄现有模式）

在 `webui.py` 的 `build_workflow_6` 后面加：

```python
def build_workflow_7(workflow_name: str, args: dict) -> dict:
    """
    TTS / 语音克隆工作流构建（IndexTTS2）。
    args:
      prompt:        要合成的文本
      ref_audio:     克隆参考音频文件名（克隆模式必填）
      ref_text:      参考音频对应的文本（提升克隆质量，可选）
      emotion_weight: 情感权重 0~1（V2 特性，可选）
    """
    import json
    path = WORKFLOW_DIR / (workflow_name + ".json")
    if not path.exists():
        raise gr.Error(f"找不到工作流文件:{path}")
    wf = json.loads(path.read_text(encoding="utf-8"))

    wf["<文本节点ID>"]["inputs"]["text"] = args["prompt"]

    if args.get("ref_audio"):
        wf["<参考音频节点ID>"]["inputs"]["audio"] = args["ref_audio"]
        wf["<参考文本节点ID>"]["inputs"]["text"] = args.get("ref_text", "")

    if "emotion_weight" in args:
        wf["<情感权重节点ID>"]["inputs"]["value"] = args["emotion_weight"]

    return wf
```

> `<节点ID>` 需在导出的 JSON 里查实际值替换。

### 第 6 步：在 WORKFLOW_BUILDERS 登记一行

```python
WORKFLOW_BUILDERS = {
    ...
    "image_z_image_turbo": (build_workflow_1, "文生图"),
    ...
    "LTX23-单图数字人-语音驱动": (build_workflow_6, "单图数字人-语音驱动"),
    "TTS-语音克隆": (build_workflow_7, "语音克隆"),   # ← 新增这一行
}
```

### 第 7 步：新增 submit 函数（照抄 submit_workflow_1）

```python
def submit_workflow_7(prompt, ref_audio=None, ref_text="", emotion_weight=0.5):
    """TTS / 语音克隆提交（IndexTTS2）。"""
    if not prompt:
        raise gr.Error("请输入要合成的文本")
    args = {
        "prompt": prompt,
        "ref_audio": ref_audio,
        "ref_text": ref_text,
        "emotion_weight": emotion_weight,
    }
    task = make_task_and_enqueue("TTS-语音克隆", args)
    return task
```

> `make_task_and_enqueue` 是现有通用函数，无需改动。

### 第 8 步：在 build_ui() 加一个 Tab（照抄现有 Tab）

```python
with gr.Tab("语音克隆"):
    with gr.Row():
        tts_prompt = gr.Textbox(label="合成文本", lines=4)
    with gr.Row():
        ref_audio_in = gr.Audio(label="参考音频（克隆用）", type="filepath")
        ref_text_in  = gr.Textbox(label="参考音频对应文本（提升克隆质量）")
    emotion_in = gr.Slider(0, 1, value=0.5, label="情感权重（IndexTTS2 V2）")
    tts_btn   = gr.Button("生成语音", variant="primary")
    tts_btn.click(
        submit_workflow_7,
        inputs=[tts_prompt, ref_audio_in, ref_text_in, emotion_in],
        outputs=[...],   # 同现有 Tab 的输出绑定
    )
```

> 参考音频上传需走现有的 `on_ref_upload`（webui.py 里已有），拿到 ComfyUI 文件名后传给 ref_audio。

### 第 9 步：重启 webui 验证

```bash
# 重启 start_browser.bat
# Gradio 会自动暴露新的 /submit_workflow_7 端点
```

前端 API 文档（API接口文档.md）自动新增该端点，无需手写。

---

## 5. 前端 API（自动暴露，预计形态）

```
POST /gradio_api/call/submit_workflow_7
{
  "data": [
    "你好，这是一段测试语音。",   // prompt: 合成文本
    "ref.wav",                    // ref_audio: 克隆参考音频文件名（可空）
    "这是参考音频对应的文字",        // ref_text: 参考音频文本（可空）
    0.5                           // emotion_weight: 情感权重 0~1（可空）
  ]
}
```

调用流程与现有端点完全一致（两步式 + 轮询 render_queue 取结果）。
产物为 `.wav` 文件，落盘到 `outputs/`，通过 render_queue 的画廊或文件 URL 获取。

---

## 6. 进阶玩法：TTS + 数字人联动

接入后可形成完整内容链路：

```
文本 ──► IndexTTS2 语音克隆 ──► 音频文件 ──► workflow 6 数字人 ──► 说话视频
```

即：先用 TTS 把文案转成目标音色的语音，再用这段语音驱动单图数字人，生成会说话的人像视频。
（前端/调用方串联两个 submit_workflow 即可，本服务串行队列会依次执行。）

---

## 7. 工作量估算

| 步骤 | 耗时 | 难度 |
|------|------|------|
| 安装节点 + 依赖 | 30-60 分钟 | 易 |
| 下载模型（2-4GB） | 取决于网速 | 易 |
| 在 ComfyUI 搭工作流并导出 JSON | 1-2 小时 | 中（需熟悉 ComfyUI） |
| 写 build_workflow_7 + submit + Tab | 1 小时 | 易（照抄现有模式） |
| 联调测试 | 1-2 小时 | 中 |
| **合计** | **约半天到 1 天** | |

---

## 8. 风险与注意事项

| 风险 | 应对 |
|------|------|
| IndexTTS2 节点对 torch 2.12 / CUDA 13 / transformers 5.9 的兼容性 | 装完先在 ComfyUI 界面单独跑通节点，确认无报错再集成 |
| IndexTTS2 与数字人工作流抢显存 | 串行队列天然规避（同一时刻只跑一个） |
| 音频文件体积（长文本 wav 较大） | 产物统一 wav，前端如需压缩可后处理 |
| 克隆质量依赖参考音频质量 | 建议 10-30 秒清晰人声，采样率 ≥16kHz，无背景噪音 |
| HuggingFace 下载慢 | 已配 hf-mirror 镜像，正常可加速 |
| 节点选择多导致决策成本 | 优先 chenpipi0807 版；不满足再换优化版 / TTS Audio Suite |

---

## 9. 备选方案：若 ComfyUI 节点不稳定

如果路线 A 的节点质量不行，可退回**路线 B：独立 TTS 服务**：

- 用 FastAPI 包一层 IndexTTS2，独立端口（如 9100）暴露 `/tts` 接口
- 在 webui.py 的 submit_workflow_7 里不调 ComfyUI，直接 HTTP 调本地 TTS 服务
- 产物同样落盘到 outputs/，复用轮询机制
- 代价：多一个进程要守护、多一个端口要管、前端 API 需手写

路线 B 更可控但偏离现有架构，**仅作 Plan B**。

---

## 10. 模型选型参考链接

- IndexTTS2 官方模型：https://huggingface.co/IndexTeam/IndexTTS2
- ComfyUI-Index-TTS 节点：https://github.com/chenpipi0807/ComfyUI-Index-TTS
- CosyVoice 2 备选模型：https://huggingface.co/FunAudioLLM/CosyVoice2-0.5B

---

*方案结束 · 核心参考源码：`webui.py`（build_workflow_1~6 模式）· `comfyui_server.py`（run_workflow/upload_image）*
