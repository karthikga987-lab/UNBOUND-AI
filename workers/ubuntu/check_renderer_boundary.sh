#!/bin/sh
# Read-only administrator acceptance checks on the disconnected guest console.
set -eu
fail() { echo "$1" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail "Run on the guest console as administrator"
pid=$(systemctl show --value -p MainPID unbound-renderer)
[ "$pid" -gt 1 ] || fail "Renderer is stopped"
broker_uid=$(id -u unbound-worker)
renderer_uid=$(id -u unbound-renderer)
[ "$broker_uid" -ne 0 ] && [ "$renderer_uid" -ne 0 ] && [ "$broker_uid" -ne "$renderer_uid" ] || fail "UID separation failed"
[ "$(stat -c %u "/proc/$pid")" = "$renderer_uid" ] || fail "Unexpected renderer UID"
[ "$(readlink "/proc/$pid/ns/net")" != "$(readlink /proc/1/ns/net)" ] || fail "Renderer shares guest network namespace"
[ "$(nsenter -t "$pid" -n ip -o link show | wc -l)" -eq 1 ] || fail "Unexpected interface in renderer namespace"
nsenter -t "$pid" -n ip link show lo >/dev/null
[ -z "$(nsenter -t "$pid" -n ip -4 route show default)" ] || fail "Unexpected renderer IPv4 route"
[ -z "$(nsenter -t "$pid" -n ip -6 route show default)" ] || fail "Unexpected renderer IPv6 route"
[ "$(stat -c '%U:%G:%a' /run/unbound-renderer/control.sock)" = unbound-renderer:unbound-ipc:660 ] || fail "Unsafe socket"
[ "$(stat -c '%U:%G:%a' /run/unbound-renderer)" = unbound-renderer:unbound-ipc:750 ] || fail "Unsafe socket directory"
for path in /etc/unbound-browser-worker/app.key /etc/unbound-browser-worker/server.key /etc/unbound-browser-worker/proxy.password /var/lib/unbound-browser-worker/replay.sqlite3
do
    [ -f "$path" ] && [ ! -L "$path" ] || fail "Provision all synthetic fixtures before the check"
    if nsenter -t "$pid" -m -- runuser -u unbound-renderer -- test -r "$path"; then
        fail "Renderer can read a broker fixture in its systemd mount namespace"
    fi
done
cd /opt/unbound-gate-a
runuser -u unbound-worker -- .venv-worker/bin/python -Es -m workers.ubuntu.check_disconnected_renderer &
fixture_pid=$!
trap 'kill "$fixture_pid" 2>/dev/null || true; wait "$fixture_pid" 2>/dev/null || true' EXIT HUP INT TERM
seen_chromium=0
attempt=0
while kill -0 "$fixture_pid" 2>/dev/null && [ "$attempt" -lt 40 ]
do
    for chromium_pid in $(pgrep -u "$renderer_uid" -f '/chrome(-headless-shell)?( |$)' || true)
    do
        seen_chromium=1
        [ "$(awk '/^Uid:/{print $2}' "/proc/$chromium_pid/status")" = "$renderer_uid" ] || fail "Chromium runs under unexpected UID"
        grep -q '^NoNewPrivs:[[:space:]]*1$' "/proc/$chromium_pid/status" || fail "Chromium NoNewPrivs is not active"
        grep -q '^Seccomp:[[:space:]]*2$' "/proc/$chromium_pid/status" || fail "Chromium seccomp filter is not active"
        cmdline=$(tr '\000' ' ' < "/proc/$chromium_pid/cmdline")
        case "$cmdline" in *--no-sandbox*) fail "Chromium sandbox is disabled";; esac
    done
    attempt=$((attempt + 1))
    sleep 0.25
done
wait "$fixture_pid"
trap - EXIT HUP INT TERM
[ "$seen_chromium" -eq 1 ] || fail "No Chromium child process observed"
echo "Disconnected renderer checks passed; host egress and guest firewall were not exercised."
