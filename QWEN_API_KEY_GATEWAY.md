# 本地 Qwen Bearer API Key 网关

## 用途

现有 `http://<服务器>/qwen/v1` 保留 Nginx Basic Auth，供工作台和原有局域网调用继续使用。
新网关为业务系统提供 OpenAI SDK 可直接使用的 Bearer API Key：

```text
Base URL: http://<服务器>/qwen-api/v1
认证: Authorization: Bearer brm_<随机密钥>
```

认证仍使用 SHA-256 哈希。新增及轮换的密钥额外保存 Fernet 加密副本，管理员可按需查看；列表永不返回明文或密文。此前仅保存哈希的旧密钥不可逆，查看时提示使用原副本或轮换（旧密钥随即失效），不会自动轮换现有业务密钥。

加密文件默认 `/var/lib/brmmedia/qwen-api-keys.sqlite3.fernet`，可通过 `BRM_QWEN_KEY_VAULT` 指定，权限 0600。备份数据库时必须单独安全备份匹配的加密文件，勿提交 Git、下载到公开目录或日志输出。已存在密文时若文件缺失，不生成替代文件；查看/新增/轮换返回 503，已有哈希认证继续工作。数据库迁移仅增加 `key_ciphertext` 列，兼容旧数据。当前 HTTP 入口只应在可信内网/VPN使用，公网接入须 HTTPS。

每行显示查看与删除入口，正常密钥的删除按钮禁用，先禁用再确认物理删除。明文区域可手动隐藏，60 秒自动清空，离开菜单也清空；轮换须二次确认。旧记录的密文随物理删除一起删除，历史备份不受影响。

## 部署

先将新增文件同步至生产运行目录，再以 root 在 WSL 中执行。`BRM_APP_DIR` 必须是生产实际运行目录，`BRM_RUN_AS` 是运行后端的 Linux 用户。

```bash
sudo BRM_APP_DIR=/srv/brmmedia/app BRM_RUN_AS=brm \
  /srv/brmmedia/app/llm-backend-deploy/install_qwen_api_gateway.sh
```

密钥数据库默认位于 `/var/lib/brmmedia/qwen-api-keys.sqlite3`，权限为服务账号可读写、其他用户不可读。网关从当前 Qwen Nginx upstream 配置动态读取模型后端，模型切换后无须重启或重建密钥。

Nginx 必须增加以下位置块。`/qwen-api/v1/` 显式关闭 Basic Auth，仅接受网关验证的 Bearer Key；`/qwen-api/admin/` 继承站点现有 Basic Auth，仅管理员可管理密钥。

```nginx
location = /qwen-api {
    return 308 /qwen-api/v1/;
}

location /qwen-api/v1/ {
    auth_basic off;
    proxy_pass http://127.0.0.1:9300/v1/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 3700s;
    proxy_send_timeout 3700s;
    proxy_buffering off;
    client_max_body_size 32M;
}

location /qwen-api/admin/ {
    proxy_pass http://127.0.0.1:9300/admin/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

修改 Nginx 后始终先执行 `sudo nginx -t`，通过后再 `sudo systemctl reload nginx`。

## 密钥管理接口

管理接口继承站点的 Nginx Basic Auth；不要将 Basic Auth 密码或 Bearer Key 写入 shell 历史、Git、日志或工单。

登录 BRM AI 工作台后，左侧导航的“密钥管理”页面会在每条密钥记录后显示可用操作：正常密钥可禁用或轮换；已禁用密钥可启用或物理删除；已撤销密钥可物理删除。物理删除会从数据库移除该记录，且不可恢复。历史密钥不会显示明文；新建或轮换成功后明文只在页面提示区展示一次，请立即复制到目标软件的密钥存储中。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/qwen-api/admin/api-keys` | 列出密钥元数据，不返回明文或哈希 |
| `POST` | `/qwen-api/admin/api-keys` | 创建密钥；仅本次响应含 `api_key` |
| `POST` | `/qwen-api/admin/api-keys/{id}/disable` | 暂停密钥 |
| `POST` | `/qwen-api/admin/api-keys/{id}/enable` | 恢复未撤销密钥 |
| `POST` | `/qwen-api/admin/api-keys/{id}/revoke` | 永久撤销密钥 |
| `DELETE` | `/qwen-api/admin/api-keys/{id}` | 物理删除已禁用或已撤销的密钥记录 |
| `POST` | `/qwen-api/admin/api-keys/{id}/rotate` | 撤销旧密钥并创建替代密钥 |
| `POST` | `/qwen-api/admin/api-keys/{id}/reveal` | 管理员按需解密查看；响应 no-store；无旧明文副本时 409 |

2026-10-11 已上线查看与明确的删除入口；生产代码及迁移前数据库备份：`/srv/brmmedia/strata/backups/key-reveal-20261011/`。线上验收覆盖旧密钥 409、新建/轮换密钥解密、no-store、未认证/业务 Bearer 无管理权限、活动密钥删除 409、禁用后物理删除；测试记录已清理，已有业务记录保持不变。回滚代码不回滚数据库使用记录或业务变更；加密列为向后兼容的增量列。

创建示例：

```bash
curl --fail --user "$BRM_USER:$BRM_PASSWORD" \
  -H 'Content-Type: application/json' \
  -d '{"name":"baorongwanxiang-production","expires_in_days":365}' \
  http://<服务器>/qwen-api/admin/api-keys
```

返回示例中的 `api_key` 仅保存到目标系统的密钥管理中：

```json
{
  "id": "e5a04d51-4fd0-4d30-b5db-4c8b03b8cdd5",
  "name": "baorongwanxiang-production",
  "prefix": "brm_xxxxxxxx",
  "status": "active",
  "api_key": "brm_..."
}
```

## 业务系统调用

先查询模型 ID，随后将返回的 `data[0].id` 填入对话请求。接口透明转发 Qwen 的 `/models` 和 `/chat/completions`，包括 UTF-8 SSE 流。
为兼容未提供 Qwen 思考开关的第三方“测试连接”表单，网关对未明确指定的聊天请求默认加入 `chat_template_kwargs.enable_thinking=false`，确保有限的 `max_tokens` 优先用于 `message.content`；需要推理内容时可显式传入 `chat_template_kwargs: {"enable_thinking": true}`。

```bash
curl --fail \
  -H "Authorization: Bearer $BRM_QWEN_API_KEY" \
  http://<服务器>/qwen-api/v1/models
```

Python OpenAI SDK：

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://<服务器>/qwen-api/v1",
    api_key="brm_...",
)

reply = client.chat.completions.create(
    model="<GET /models 返回的 ID>",
    messages=[{"role": "user", "content": "请用一句话介绍自己。"}],
    stream=False,
)
print(reply.choices[0].message.content)
```

网关没有开放 Qwen 运行端口；外部只允许经 Nginx TCP 80 访问。密钥泄露、离职或集成下线时应立即撤销对应密钥；常规轮换使用 `rotate`，并在目标系统更新新密钥后验证旧密钥已失效。

## 客户端取消与连接清理（2026-10-11）

网关使用 HTTPX 异步转发，整个 ASGI 响应生命周期监听下游断开；断开发生在等待上游响应头或流式读取期间，均取消上游读取。退出时在 AnyIO 屏蔽取消的清理区关闭响应和客户端，然后释放共享推理锁；请求体读取失败也会释放锁。正常完成、上游异常和下游发送失败使用相同清理路径。

SSE 按可用块直接转发，不再等待累计 8 KiB；返回 `X-Accel-Buffering: no`。上游中途异常直接终止流，不伪造 `[DONE]`。客户端主动关闭连接属于取消，不应继续等待完整输出。

回归测试：`python -m unittest tests.test_qwen_gateway_disconnect`，包含真实 Uvicorn / TCP 客户端关闭连接，以及响应头前断开、读流时断开、发送失败、外部任务取消和异常路径。生产验收还必须检查模型 `/slots` 空闲、共享锁可被排他获取，以及取消后能再次问答/安全切换；单纯看到浏览器停止显示不能证明上游已停止。

生产代码：`/srv/brmmedia/app/ubuntu-backend-deploy/qwen_api_gateway.py`，运行环境为同目录 `.venv`。本次复用已安装的 HTTPX 0.28.1 / AnyIO，不升级框架、不改 Nginx。原代码备份：`/srv/brmmedia/strata/backups/gateway-cancel-20261011/qwen_api_gateway.py`。回滚前确认无在途请求，再恢复该文件并仅重启 `brmmedia-qwen-api-gateway`；无需重启媒体或模型服务。

## 非阻塞鉴权、连接池复用与跨服务统一密钥（2026-11-23）

- **非阻塞 Bearer 鉴权**：`qwen_api_gateway.py`（与 `lan_api.py`）对 SQLite 密钥库启用 `PRAGMA journal_mode=WAL` 与 `PRAGMA synchronous=NORMAL`，引入 **8 秒 TTL 内存缓存**（`BRM_QWEN_KEY_CACHE_TTL=8.0`）与 **5 秒批量使用记账缓冲**（`_USAGE_BUFFER`），并通过 `anyio.to_thread.run_sync` 卸载到工作线程，避免高频鉴权及写入阻塞 FastAPI 主事件循环。执行禁用、启用、撤销、删除或轮换操作时立即失效本地密钥缓存。
- **异步 HTTP 连接池**：网关按当前事件循环复用 `httpx.AsyncClient`（`max_connections=64`、`max_keepalive_connections=16`、`keepalive_expiry=30.0`），自动回收已关闭事件循环的旧连接，降低反复建连延迟。
- **统一 REST API 鉴权**：同一 `brm_...` Bearer API Key 现已同时支持 `/qwen-api/v1/*` 与媒体自动化接口 `/api/v1/*`（`lan_api` 直接读取共享密钥库；密钥创建、轮换、禁用与删除仍由 `/qwen-api/admin/` 统一管理）。
