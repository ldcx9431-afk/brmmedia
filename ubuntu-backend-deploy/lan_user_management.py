"""Same-access Nginx Basic Auth user creation for the BRMMedia workbench."""

from __future__ import annotations

import os
import re
import subprocess


LAN_USER_HELPER = os.environ.get(
    "BRM_LAN_USER_HELPER", "/usr/local/sbin/brmmedia-add-lan-user"
)


def add_lan_user(username, new_password, confirm_password):
    """Create a user through the constrained root helper; never pass secrets in argv."""
    username = (username or "").strip()
    new_password = new_password or ""
    confirm_password = confirm_password or ""

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,31}", username):
        return "", "", "", "❌ 用户名需为 3–32 位，以英文字母开头，只能包含字母、数字、点、下划线和连字符。"
    if username.casefold() == "brmadmin":
        return "", "", "", "❌ brmadmin 是现有主账号，请使用其他用户名。"
    if len(new_password) < 8:
        return "", "", "", "❌ 新密码至少需要 8 个字符。"
    try:
        password_bytes = new_password.encode("utf-8")
    except UnicodeEncodeError:
        return "", "", "", "❌ 密码包含不支持的字符。"
    if len(password_bytes) > 72:
        return "", "", "", "❌ 新密码最多 72 个 UTF-8 字节。"
    if new_password != confirm_password:
        return "", "", "", "❌ 两次输入的新密码不一致。"
    if any(char in value for value in (username, new_password) for char in ("\n", "\r", "\x00")):
        return "", "", "", "❌ 用户名或密码包含不支持的控制字符。"

    try:
        result = subprocess.run(
            ["sudo", "-n", LAN_USER_HELPER],
            input=f"{username}\n{new_password}\n",
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except FileNotFoundError:
        return "", "", "", "❌ 用户管理组件尚未安装，请联系管理员部署。"
    except subprocess.TimeoutExpired:
        return "", "", "", "❌ 新增用户超时；请先确认用户是否已创建，再决定是否重试。"
    except OSError:
        return "", "", "", "❌ 无法执行用户创建服务，请联系管理员检查。"

    if result.returncode == 0:
        return "", "", "", f"✅ 用户 `{username}` 已新增，访问权限与 brmadmin 相同。"
    if result.returncode == 3:
        return "", "", "", "❌ 该用户名已存在，请换一个用户名。"
    if result.returncode == 2:
        return "", "", "", "❌ 用户名或密码不符合要求。"
    return "", "", "", "❌ 新增用户失败，认证文件未能安全更新；请联系管理员检查。"
