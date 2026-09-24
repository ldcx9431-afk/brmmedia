#!/bin/sh
# Install the narrowly-scoped account helper and sudoers rule. Run as root.
set -eu

if [ "$(/usr/bin/id -u)" -ne 0 ]; then
    echo "Run this installer as root (for example: sudo $0)." >&2
    exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HELPER_SOURCE="$SCRIPT_DIR/brmmedia-add-lan-user.sh"
SUDOERS_SOURCE="$SCRIPT_DIR/brmmedia-add-lan-user.sudoers"
HELPER_TARGET="/usr/local/sbin/brmmedia-add-lan-user"
SUDOERS_TARGET="/etc/sudoers.d/brmmedia-lan-user"
SUDOERS_TEMP=$(/usr/bin/mktemp /etc/sudoers.d/.brmmedia-lan-user.XXXXXX)

cleanup() {
    /bin/rm -f "$SUDOERS_TEMP"
}
trap cleanup EXIT HUP INT TERM

/usr/bin/install -o root -g root -m 0750 "$HELPER_SOURCE" "$HELPER_TARGET"
/usr/bin/install -o root -g root -m 0440 "$SUDOERS_SOURCE" "$SUDOERS_TEMP"
/usr/sbin/visudo -cf "$SUDOERS_TEMP"
/bin/mv -f "$SUDOERS_TEMP" "$SUDOERS_TARGET"
/usr/sbin/visudo -cf "$SUDOERS_TARGET"
trap - EXIT HUP INT TERM

echo "Installed $HELPER_TARGET and validated $SUDOERS_TARGET."
