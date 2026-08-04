#!/bin/sh
# 仅供 gradio 服务账户 brm 通过受限 sudo 调用。
# 从标准输入读取“当前密码\n新密码\n”，避免把任何密码暴露在进程参数或日志中。
set -eu

HTPASSWD_FILE="/etc/nginx/.htpasswd-brmmedia"
HTPASSWD_BIN="/usr/bin/htpasswd"
NGINX_BIN="/usr/sbin/nginx"
USERNAME="brmadmin"

IFS= read -r current_password || exit 2
IFS= read -r new_password || exit 2

[ -n "$current_password" ] || exit 2
[ "${#new_password}" -ge 8 ] || exit 2

# 先校验旧密码，避免已登录浏览器或其他已授权会话意外更改入口凭据。
"$HTPASSWD_BIN" -vb "$HTPASSWD_FILE" "$USERNAME" "$current_password" >/dev/null 2>&1 || exit 3

temp_file=$(mktemp "${HTPASSWD_FILE}.XXXXXX")
cleanup() {
    rm -f "$temp_file"
}
trap cleanup EXIT HUP INT TERM

cp --preserve=mode,ownership "$HTPASSWD_FILE" "$temp_file"
printf '%s\n' "$new_password" | "$HTPASSWD_BIN" -iB "$temp_file" "$USERNAME" >/dev/null
chown root:www-data "$temp_file"
chmod 640 "$temp_file"
mv -f "$temp_file" "$HTPASSWD_FILE"
trap - EXIT HUP INT TERM

# 配置无需重启服务；reload 可立即重新读取认证文件且不中断现有请求。
"$NGINX_BIN" -s reload
