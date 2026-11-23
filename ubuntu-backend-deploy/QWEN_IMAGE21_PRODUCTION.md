# Qwen Image 2.1 非商业研究部署

用途已由用户于 2026-10-11 确认为非商业研究；不是已部署 Qwen3.8 LLM 的视觉组件。
遵守 [Qwen Research License](https://huggingface.co/Qwen/Qwen-Image-2.1/blob/main/LICENSE)，商业使用前必须取得独立授权。

## 固定模型与运行时

- 权重：Comfy-Org/Qwen-Image-2.1，revision `df94239739eef7205973a145ffa6f441f7b64e84`。
- 基础模型 INT8（非 Turbo）7,256,783,064 bytes；编码器 INT8 9,350,798,360 bytes；VAE BF16 675,509,688 bytes。
- 精确 SHA-256 及可恢复下载器见 `download_qwen_image21.py`；校验通过前只保留 `.partial`，不启用。
- 隔离 ComfyUI commit `7f7fd918ed0815c5d5ef2ccfe44bfc8e72cfd217`，目录 `/srv/brmmedia/qwen-image21/ComfyUI`。
- 新 venv `/srv/brmmedia/qwen-image21/venv` 通过 `production-readonly.pth` 只读引用生产 venv 的已验证依赖（Torch 2.11.0+cu130）；升级包仅安装到新 venv，生产 venv 不做 pip 修改。实际包版本保存在 `/srv/brmmedia/qwen-image21/requirements.lock`。将来迁移生产 venv 前需同步评估此只读依赖路径。
- 服务 `brmmedia-qwen-image21`，仅绑定 `127.0.0.1:8190`；不开放额外局域网端口，不装自定义节点。
- A5000 GPU0；A4000 两卡和 Qwen LLM 服务保持不动。

## 工作台集成

可复现的模型/运行时安装入口：`sudo bash install_qwen_image21_runtime.sh`。此脚本只准备独立运行时、下载/校验模型，不替用户自动启用工作台；服务模板为 `brmmedia-qwen-image21.service.example`。默认路径匹配当前机房部署。首次在其它机器上安装时需显式配置 `BRMMEDIA_BACKEND_VENV` 并检查对应 Python/CUDA/A5000 兼容性。

由 `BRM_QWEN_IMAGE21_ENABLED=1` 显式启用“图像创作 → Qwen Image 2.1 文生图”。共享现有单 worker 媒体队列、任务记录与素材库。
切换到研究模型前检查两个 Comfy 队列为空并释放生产模型缓存；任务结束释放研究模型缓存。其他媒体任务开始前也检查研究运行时并释放缓存，避免同时占用显存。
新运行时故障时，显存隔离检查会明确失败；禁止旁路并发。若需要关闭研究运行时，先等待任务结束，再移除两个后端的启用变量并重启，再停止研究服务。

首版仅文生图：单张、256–1536px、32倍数、最多1,048,576像素；默认1024×1024、40步、CFG1、Euler/simple、seed0。支持可复现种子。任务预算1小时，工作台中断操作会中断隔离运行时并等待队列释放。进度事件按批次排空，避免界面落后；prompt_id 在提交后落盘，工作台重启可重新附着隔离运行时的原任务，不保存业务提示词、不自动重新推理。若隔离运行时也已重启丢失任务，明确失败而非重复提交。RGBA可通过提示词指定；编辑、多参考图和Turbo不在首版接口范围内。

## REST

继续使用现有80端口、认证和 `/api/v1/tasks`，workflow 为 `qwen-image21`：

```json
{"workflow":"qwen-image21","params":{"prompt":"一只橘猫，柔和光线","width":1024,"height":1024,"steps":40,"seed":42}}
```

查询和下载沿用现有任务接口；能力接口仅在启用变量开启时公布该工作流。

## 验收与回滚

上线前校验三个权重、隔离节点和实际 PNG 生图；核对尺寸、耗时和显存；再备份工作台代码及Nginx配置后启用，验证现有健康检查与任务API。Nginx不需新增代理。
回滚：先等共享媒体队列为空，恢复工作台代码备份和启用配置，重启 `baorong-backend` / `brmmedia-lan-api`；停止研究运行时。保留模型和隔离运行时，不删除既有模型。

## 状态

2026-10-11：已在 `172.16.28.8` 完成部署，研究运行时设为开机自启；工作台入口和能力接口均已启用。

- 三个权重大小及 SHA-256 全部通过；生产源文件与本地 SHA-256 一致。
- 1024×1024 / 40步 / seed42：独立运行时首次出图32.49秒；正式LAN API链路35.41秒和37.34秒。PNG可解码为1024×1024 RGBA；“研究测试”四字经视觉检查正确。普通出图的RGBA文件不等于已验收透明背景效果。
- 两次正式生图期间采集14个资源/健康样本：A5000采样峰值15,921MiB（约15.5GiB），Qwen健康检查全部通过；两张A4000分配保持不变。显存为离散采样，不代表硬件瞬时绝对峰值。
- 取消实测6.23秒停止，队列清空；原prompt恢复实测成功，Comfy历史计数未增加，没有重复推理。
- 最终API任务 `f86a4be52d2e479d896a7e518bdcdf1f`，进度40/40、prompt_id已持久化，超尺寸请求返回422。
- 切回原Z-Image通过正式API成功出图（32.83秒）。既有SeedVR2 7B/3B文件均保留。
- 本地专项58项测试通过。Windows全量测试有1项Bash/WSL环境失败及5项跳过（既有H3分块下载脚本测试，未改动该脚本）；未将全量测试报告为全通过。
- 备份：`/srv/brmmedia/qwen-image21/backups/20261011-100718`，包含原工作台代码及Nginx配置副本；Nginx未改动。新增启用配置在两个后端的 `20-qwen-image21.conf` drop-in。回滚需移除此研究启用drop-in、恢复备份代码并重启两个后端；最后停止研究服务，模型保留。
- 研究许可证副本：`/srv/brmmedia/qwen-image21/LICENSE.qwen-research`；验收图片及资源采样：`/srv/brmmedia/qwen-image21/acceptance/`。
- Antigravity代理当前无监听，前次实际调用连接失败；本次实施和验收由Codex执行，未将代理建议或调用失败冒充部署结果。
