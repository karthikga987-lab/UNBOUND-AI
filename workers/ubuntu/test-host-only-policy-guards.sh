#!/bin/sh
# Safe mocked guard tests for install-host-only-policy.sh. No routes, nftables
# tables, services, or host networking are changed by this test.
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
installer="$source_dir/install-host-only-policy.sh"
tmp=$(mktemp -d "${TMPDIR:-/tmp}/unbound-policy-guards.XXXXXX")
cleanup() {
    rm -f "$tmp"/install.sh "$tmp"/host-only-review.nft "$tmp"/nft.log \
        "$tmp"/route4.out "$tmp"/route6.out "$tmp"/altered.out \
        "$tmp"/mockbin/id "$tmp"/mockbin/ip "$tmp"/mockbin/nft
    rmdir "$tmp"/mockbin 2>/dev/null || true
    rmdir "$tmp" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

mkdir "$tmp/mockbin"
sed "s|^policy=/opt/unbound-gate-a/workers/ubuntu/host-only-review.nft$|policy=$tmp/host-only-review.nft|" \
    "$installer" > "$tmp/install.sh"
touch "$tmp/host-only-review.nft"

cat > "$tmp/mockbin/id" <<'MOCK_ID'
#!/bin/sh
[ "${1:-}" = -u ] && { printf '0\n'; exit 0; }
exit 99
MOCK_ID

cat > "$tmp/mockbin/ip" <<'MOCK_IP'
#!/bin/sh
case "$*" in
    "-4 -o addr show dev enp0s3")
        printf '2: enp0s3 inet 192.168.56.10/24 brd 192.168.56.255 scope global enp0s3\n'
        ;;
    "-4 route show default")
        [ "${MOCK_IPV4_DEFAULT:-0}" = 1 ] && printf 'default via 192.168.56.254 dev enp0s3\n'
        ;;
    "-6 route show default")
        [ "${MOCK_IPV6_DEFAULT:-0}" = 1 ] && printf 'default via fe80::1 dev enp0s3\n'
        ;;
    *) exit 98 ;;
esac
MOCK_IP

cat > "$tmp/mockbin/nft" <<'MOCK_NFT'
#!/bin/sh
printf '%s\n' "$*" >> "$MOCK_NFT_LOG"
case "$*" in
    "list table inet unbound_guest") exit 0 ;;
    "-a list table inet unbound_guest")
        printf 'table inet unbound_guest { chain output { type filter hook output priority filter; policy accept; } }\n'
        exit 0
        ;;
    *) exit 97 ;;
esac
MOCK_NFT
chmod 700 "$tmp/mockbin/id" "$tmp/mockbin/ip" "$tmp/mockbin/nft"

expect_rejected() {
    label=$1
    shift
    if env PATH="$tmp/mockbin:/usr/bin:/bin" MOCK_NFT_LOG="$tmp/nft.log" "$@" \
        /bin/sh "$tmp/install.sh" > "$tmp/$label.out" 2>&1; then
        echo "$label: unexpected success" >&2
        exit 1
    fi
}

: > "$tmp/nft.log"
expect_rejected route4 MOCK_IPV4_DEFAULT=1
grep -q 'guest IPv4 default route is present' "$tmp/route4.out"
[ ! -s "$tmp/nft.log" ] || { echo 'route4: nft was invoked' >&2; exit 1; }
echo 'unexpected IPv4 default route: rejected before nftables access'

: > "$tmp/nft.log"
expect_rejected route6 MOCK_IPV6_DEFAULT=1
grep -q 'guest IPv6 default route is present' "$tmp/route6.out"
[ ! -s "$tmp/nft.log" ] || { echo 'route6: nft was invoked' >&2; exit 1; }
echo 'unexpected IPv6 default route: rejected before nftables access'

: > "$tmp/nft.log"
expect_rejected altered MOCK_IPV4_DEFAULT=0 MOCK_IPV6_DEFAULT=0
grep -q 'existing UNBOUND nft table does not match the reviewed policy' "$tmp/altered.out"
! grep -q -- ' -f ' "$tmp/nft.log" || { echo 'altered table: installer attempted nft apply' >&2; exit 1; }
[ "$(wc -l < "$tmp/nft.log")" -eq 2 ] || { echo 'altered table: unexpected nft operation' >&2; exit 1; }
echo 'altered existing nftables table: rejected without replacement'
