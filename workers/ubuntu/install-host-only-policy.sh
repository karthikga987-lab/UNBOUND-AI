#!/bin/sh
# Invoked by systemd as root. Never changes routes or network adapters.
set -eu
policy=/opt/unbound-gate-a/workers/ubuntu/host-only-review.nft
[ "$(id -u)" -eq 0 ] || { echo 'root required' >&2; exit 1; }
[ -r "$policy" ] || { echo 'UNBOUND nft policy is unavailable' >&2; exit 1; }
attempt=0
until ip -4 -o addr show dev enp0s3 | grep -q 'inet 192\.168\.56\.10/24 '; do
    [ "$attempt" -lt 30 ] || { echo 'expected guest host-only address did not appear' >&2; exit 1; }
    attempt=$((attempt + 1))
    sleep 1
done
[ -z "$(ip -4 route show default)" ] || { echo 'guest IPv4 default route is present' >&2; exit 1; }
[ -z "$(ip -6 route show default)" ] || { echo 'guest IPv6 default route is present' >&2; exit 1; }
if nft list table inet unbound_guest >/dev/null 2>&1; then
    actual=$(nft -a list table inet unbound_guest) || { echo 'cannot inspect existing UNBOUND nft table' >&2; exit 1; }
    normalized=$(printf '%s\n' "$actual" | sed -E \
        -e 's/counter packets [0-9]+ bytes [0-9]+/counter/g' \
        -e 's/[[:space:]]+# handle [0-9]+//' \
        -e 's/^[[:space:]]+//' \
        -e '/^$/d')
    expected=$(cat <<'POLICY'
table inet unbound_guest {
chain input {
type filter hook input priority filter; policy drop;
iifname "lo" counter accept
iifname "enp0s3" ip saddr 192.168.56.1 ip daddr 192.168.56.10 tcp dport 9443 ct state established,new counter accept
iifname "enp0s3" ip saddr 192.168.56.1 ip daddr 192.168.56.10 tcp sport 18765 ct state established ct direction reply counter accept
counter drop
}
chain output {
type filter hook output priority filter; policy drop;
oifname "lo" counter accept
oifname "enp0s3" ip saddr 192.168.56.10 ip daddr 192.168.56.1 tcp dport 18765 ct state established,new counter accept
oifname "enp0s3" ip saddr 192.168.56.10 ip daddr 192.168.56.1 tcp sport 9443 ct state established ct direction reply counter accept
counter drop
}
chain forward {
type filter hook forward priority filter; policy drop;
counter drop
}
}
POLICY
)
    if [ "$normalized" != "$expected" ]; then
        echo 'existing UNBOUND nft table does not match the reviewed policy; refusing to replace it' >&2
        exit 1
    fi
    echo 'existing UNBOUND nft table matches the reviewed host-only policy'
    exit 0
fi
nft -c -f "$policy"
nft -f "$policy"
nft list table inet unbound_guest >/dev/null
