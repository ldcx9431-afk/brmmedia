#!/bin/sh
# Add one same-access Nginx Basic Auth user. Only the BRMMedia service account
# may invoke this helper through the dedicated sudoers rule; values arrive on
# stdin so neither usernames nor passwords become process arguments.
set -eu
LC_ALL=C
export LC_ALL

HTPASSWD_FILE="/etc/nginx/.htpasswd-brmmedia"
HTPASSWD_BIN="/usr/bin/htpasswd"
NGINX_BIN="/usr/sbin/nginx"
LOCK_FILE="/run/lock/brmmedia-htpasswd.lock"

[ "$#" -eq 0 ] || exit 2
IFS= read -r username || exit 2
IFS= read -r new_password || exit 2
unexpected_input=""
if IFS= read -r unexpected_input; then
    exit 2
fi
[ -z "$unexpected_input" ] || exit 2

case "$username" in
    [A-Za-z]*) ;;
    *) exit 2 ;;
esac
case "$username" in
    *[!A-Za-z0-9_.-]*) exit 2 ;;
esac
[ "${#username}" -ge 3 ] && [ "${#username}" -le 32 ] || exit 2
[ "$(printf '%s' "$username" | tr '[:upper:]' '[:lower:]')" != "brmadmin" ] || exit 2
[ "${#new_password}" -ge 8 ] && [ "${#new_password}" -le 72 ] || exit 2
carriage_return=$(printf '\r')
case "$new_password" in
    *"$carriage_return"*) exit 2 ;;
esac
[ -f "$HTPASSWD_FILE" ] || exit 4
[ -x "$HTPASSWD_BIN" ] && [ -x "$NGINX_BIN" ] || exit 4

# Serialize account creation and the existing brmadmin password rotation so
# concurrent updates cannot overwrite one another's .htpasswd changes.
exec 9>"$LOCK_FILE"
/usr/bin/flock -x 9 || exit 4

if /usr/bin/awk -F: -v wanted="$username" '$1 == wanted { found = 1 } END { exit !found }' "$HTPASSWD_FILE"; then
    exit 3
fi

# Validate Nginx before touching its credential file.
"$NGINX_BIN" -t -q >/dev/null 2>&1 || exit 4

temp_file=""
rollback_file=""
installed=0
cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [ "$installed" -eq 1 ] && [ -n "$rollback_file" ] && [ -f "$rollback_file" ]; then
        /bin/mv -f "$rollback_file" "$HTPASSWD_FILE" || status=4
        "$NGINX_BIN" -s reload >/dev/null 2>&1 || true
    fi
    [ -z "$temp_file" ] || /bin/rm -f "$temp_file"
    [ -z "$rollback_file" ] || /bin/rm -f "$rollback_file"
    exit "$status"
}
trap cleanup EXIT

rollback_file=$(/usr/bin/mktemp "${HTPASSWD_FILE}.rollback.XXXXXX")
/bin/cp --preserve=mode,ownership "$HTPASSWD_FILE" "$rollback_file"
temp_file=$(/usr/bin/mktemp "${HTPASSWD_FILE}.XXXXXX")
/bin/cp --preserve=mode,ownership "$HTPASSWD_FILE" "$temp_file"
printf '%s\n' "$new_password" | "$HTPASSWD_BIN" -iB "$temp_file" "$username" >/dev/null 2>&1
/bin/chown root:www-data "$temp_file"
/bin/chmod 640 "$temp_file"
installed=1
/bin/mv -f "$temp_file" "$HTPASSWD_FILE"

# Reload is graceful; it does not stop active requests. The auth file is only
# read by Nginx and is never copied into application logs or responses.
"$NGINX_BIN" -s reload >/dev/null 2>&1
installed=0
/bin/rm -f "$rollback_file"
rollback_file=""
trap - EXIT
