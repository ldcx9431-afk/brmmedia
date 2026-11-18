# BRM 工作台升级执行记录 · 2026-11-18

## 本轮已完成

- 保留 2026-11-18 只读硬件、运行时及磁盘对照记录，未删除模型或改变生产服务。
- FLUX 3 的临时 NVMe 运行有效，但测试未满足完整冷启动样本数；H3 Image 拷贝因空间/传输准备中断。C 盘剩余约 32 GiB，E 盘约 70 GiB。暂不创建 64 GiB 测试卷或搬迁权重。
- ComfyUI 0.45.0 候选 `989d93d0d968e704c589ce0146bb18c77f6b50cc` 的静态依赖检查发现：LTX 依赖 LayerStyle 的 `ImageScaleByAspectRatio V2`；TTS 依赖 `ComfyUI-Index-TTS` 的 `IndexTTS2BaseNode`。二者必须在候选启动、工作流回归和真实产物检查中通过后才能发布。
- 固定 LayerStyle 到生产记录的提交 `02acdc50affb84cd24f341d1fc2d3a9134b2ad3d`。候选使用 Qwen Image 2.1 的 Torch/CUDA 运行环境；不复制 MiniMax H3 专用环境。检查生产节点和 Qwen venv 不存在时立即停止。

## 发布门槛

1. 确认目标主机的 BRMMedia WSL 发行版、服务、GPU、D/E 盘挂载均与基线一致；Ubuntu 空发行版不作为生产目标。
2. 候选环境通过上游测试、`pip check` 和节点启动检查；记录完整依赖锁及节点提交。
3. 对照生产所有已登记工作流的节点 schema/输入，并至少成功运行 Z-Image、FLUX.2、H3 视频、LTX、SeedVR2、音乐、IndexTTS 和 Qwen 图像；验证取消、重启恢复、队列交接和实际图片/音频/视频产物。
4. 同提示、分辨率和种子对照 NVMe/HDD，至少五个标注缓存状态的样本；不清理系统缓存，不删 HDD 源。冷加载中位改善未达 30% 时不搬迁长期权重。
5. 回归全通过后，先备份服务单元、启动配置、工作流和任务历史；排空 A5000 队列，执行单运行时 canary，验证工作台/API、Strata 两张 A4000、Nginx 与媒体任务健康，再做单项切换。任一检查失败即恢复原服务和模型路径。
6. 退役模型必须先证明没有活跃调用方，再移除 API/UI/自启入口；至少观察七天后才删除权重。ACE-Step、IndexTTS、Qwen 及实验 LoRA 当前不做物理删除。

## 目标主机阻断项

本地环境现可见的 `Ubuntu` WSL 没有 `/srv/brmmedia`、媒体服务、GPU 设备或模型挂载；预期的 `BRMMedia-Ubuntu` 发行版不在 `wsl --list --all` 中。线上阶段未执行。需要先恢复/接入正确部署主机，再从只读预检开始，不能在空发行版中安装替代生产环境。
