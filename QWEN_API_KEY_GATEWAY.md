# 本地 Qwen Bearer API Key 网关

## 用途

现有 `http://<服务器>/qwen/v1` 保留 Nginx Basic Auth，供工作台和原有局域网调用继续使用。
新网关为业务系统提供 OpenAI SDK 可直接使用的 Bearer API Key：

```text
Base URL: http://<服务器>/qwen-api/v1
认证: Authorization: Bearer brm_<随机密钥>
```

密钥仅在创建或轮换响应中返回一次。服务端只持久化 SHA-256 哈希、前缀、状态、到期时间和使用记录；无法从数据库还原原始密钥。

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
