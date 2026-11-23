# BRM 工作台升级执行记录 · 2026-11-18

## 生产部署结果

- 已通过部署记录中的 Windows SSH 入口进入 `BRMMedia-Ubuntu`。本地 Ubuntu 的探测结果不代表生产服务器状态。
- 任务历史和派生缓存已迁至 `/var/lib/brmmedia/workbench-nvme`（WSL ext4），原始素材及生成产物仍在 D 盘。切换前的 200 条任务记录已做 SHA-256 校验。上线后实际提交了一次文生图任务，PNG 下载与新历史写入通过。局域网首页、健康接口和能力接口均返回 HTTP 200，Strata 对话通过。
- 已执行脚本为 `upgrade-deploy-workbench-storage.sh`。生产备份为 `/srv/brmmedia/backups/workbench-storage.LzMp2wgg`。人工回滚前先暂停媒体入口并排空队列，再停工作台与 LAN API。将新盘最新 `task-history.json` 原子写回原输出路径，恢复备份的 `webui.py`，移除 `70-workbench-storage.conf`，重新加载 systemd 并启动服务。不要用上线前的历史覆盖上线后的任务记录。
- ComfyUI 0.45.0 候选实际加载 LTX 和 IndexTTS 节点，68 种主媒体工作流节点在 schema 对照中没有发现缺失或已用参数移除。候选补齐 SageAttention 并校验生产 wheel 的 SHA-256，固定 SentencePiece 0.2.0 与 Protobuf 4.25.9 后通过 `pip check`，恢复 Triton `ptxas` 的执行权限。
- 候选生成实测包括 Z-Image 25.0 秒、FLUX.2 56.5 秒和 ACE-Step 35.6 秒。设置 `--reserve-vram 2` 后，LTX 和 H3 视频分别在 114.6 秒和 94.3 秒生成可读取 MP4，显存采样峰值为 23,902 和 23,574 MiB。LTX 小样距显卡上限只有 662 MiB，生产主 ComfyUI 仍为 0.32.0。新版的视频常用尺寸、语音、SeedVR2 和取消恢复验收仍待完成。候选证据位于 `/srv/brmmedia/upgrade-20261118/main-comfy-045`。
- 配合 `--disable-dynamic-vram --lowvram --reserve-vram 2` 复测，LTX 和 H3 视频小样分别为 20,654 MiB / 203.3 秒和 22,316 MiB / 159.9 秒。两条均生成可读取 MP4。与仅预留显存的配置相比，耗时分别增加约 77% 和 70%，因此不将此组合设为全局默认。收尾核验中三个生产媒体队列为空、隔离端口 8196 已关闭，A5000 回到 1,539 MiB。
- 本轮未切换生产模型目录或删除权重。已有 NVMe 对照未完成计划所需的冷启动样本数，旧模型退役也尚未完成调用方迁移验证。

## 本轮已完成

- 保留 2026-11-18 只读硬件、运行时及磁盘对照记录，未删除模型或改变生产服务。
- FLUX 3 的临时 NVMe 运行有效，但测试未满足完整冷启动样本数；H3 Image 拷贝因空间/传输准备中断。C 盘剩余约 32 GiB，E 盘约 70 GiB。暂不创建 64 GiB 测试卷或搬迁权重。
- ComfyUI 0.45.0 候选 `989d93d0d968e704c589ce0146bb18c77f6b50cc` 的静态依赖检查发现：LTX 依赖 LayerStyle 的 `ImageScaleByAspectRatio V2`；TTS 依赖 `ComfyUI-Index-TTS` 的 `IndexTTS2BaseNode`。二者必须在候选启动、工作流回归和真实产物检查中通过后才能发布。
- 初始候选复用 MiniMax H3 Image 的 Torch/CUDA 版本，保留独立 venv。准备脚本校验 LayerStyle 和 IndexTTS 源提交并使用线上实际目录名。`staged-source-only` 仅代表源码准备完成，后续依赖和真实工作流记录以候选目录中的报告为准。

## 发布门槛

1. 确认目标主机的 BRMMedia WSL 发行版、服务、GPU、D/E 盘挂载均与基线一致；Ubuntu 空发行版不作为生产目标。
2. 候选环境通过上游测试、`pip check` 和节点启动检查；记录完整依赖锁及节点提交。
3. 对照生产所有已登记工作流的节点 schema/输入，并至少成功运行 Z-Image、FLUX.2、H3 视频、LTX、SeedVR2、音乐、IndexTTS 和 Qwen 图像；验证取消、重启恢复、队列交接和实际图片/音频/视频产物。
4. 同提示、分辨率和种子对照 NVMe/HDD，至少五个标注缓存状态的样本；不清理系统缓存，不删 HDD 源。冷加载中位改善未达 30% 时不搬迁长期权重。
5. 回归全通过后，先备份服务单元、启动配置、工作流和任务历史；排空 A5000 队列，执行单运行时 canary，验证工作台/API、Strata 两张 A4000、Nginx 与媒体任务健康，再做单项切换。任一检查失败即恢复原服务和模型路径。
6. 退役模型必须先证明没有活跃调用方，再移除 API/UI/自启入口；至少观察七天后才删除权重。ACE-Step、IndexTTS、Qwen 及实验 LoRA 当前不做物理删除。
