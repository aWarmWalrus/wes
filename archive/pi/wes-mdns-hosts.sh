#!/bin/sh
# Keep /etc/hosts seeded with the mDNS (.local) names from hosts.yaml.
#
# WHY THIS EXISTS
# Grafana ships as a STATICALLY linked binary ("not a dynamic executable"), so
# it has no cgo resolver, so it cannot use NSS -- which means it never consults
# `mdns4_minimal` in /etc/nsswitch.conf and instead sends `.local` lookups
# straight to the upstream DNS server, which answers NXDOMAIN:
#
#   lookup DESKTOP-R2PFF9T.local on 75.75.75.75:53: no such host
#
# Prometheus resolves the SAME name fine because it is dynamically linked
# against libc and can go through NSS. That difference is the whole bug: the
# dashboard's "Last 15 turns" panel was dead while every Prometheus panel and a
# plain `curl` from the same Pi worked (2026-08-09).
#
# Go's pure resolver DOES honour /etc/hosts, so seeding it fixes Grafana and any
# other static Go binary. Run on a timer rather than written once, because the
# PC's Wi-Fi lease flaps (.168 / .91) -- hosts.yaml switched to the mDNS name
# for exactly that reason on 2026-07-24, and a hardcoded entry here would
# reintroduce the bug it was meant to fix, silently.
#
# Names come from hosts.yaml so this does not become a second source of truth.
#
# NOTE: block matching is awk with fixed strings, NOT sed addresses. The marker
# text contains "/" (a path), which would be read as a sed address delimiter.
set -eu

REPO="${WES_REPO:-$HOME/claude/wes}"
HOSTS_YAML="$REPO/hosts.yaml"
BEGIN="# BEGIN wes-mdns (managed by pi/wes-mdns-hosts.sh - do not edit)"
END="# END wes-mdns"

[ -r "$HOSTS_YAML" ] || { echo "no hosts.yaml at $HOSTS_YAML" >&2; exit 1; }

# Every `ip:` in hosts.yaml whose value is a .local name.
names=$(sed -n 's/^[[:space:]]*ip:[[:space:]]*\([A-Za-z0-9_-]*\.local\)[[:space:]]*$/\1/p' \
        "$HOSTS_YAML")
[ -n "$names" ] || { echo "no .local names in hosts.yaml - nothing to do"; exit 0; }

# Current managed block, if any (used to keep a good entry when a lookup fails).
current_block=$(awk -v b="$BEGIN" -v e="$END" \
    'index($0,b)==1{f=1;next} index($0,e)==1{f=0;next} f' /etc/hosts 2>/dev/null || true)

block=""
for n in $names; do
    # getent goes through NSS, so it DOES use avahi/mdns4_minimal.
    addr=$(getent hosts "$n" 2>/dev/null | awk '{print $1; exit}') || true
    if [ -n "${addr:-}" ]; then
        block="${block}${addr}	${n}
"
    else
        # Resolution failing is normal (PC asleep). Keep whatever is already
        # there rather than deleting a good entry over one bad lookup.
        old=$(printf '%s\n' "$current_block" | awk -v n="$n" '$2==n{print; exit}')
        [ -n "$old" ] && block="${block}${old}
"
        echo "warn: could not resolve $n; kept previous entry" >&2
    fi
done

tmp=$(mktemp)
# Everything outside the managed block, unchanged.
awk -v b="$BEGIN" -v e="$END" \
    'index($0,b)==1{f=1;next} index($0,e)==1{f=0;next} !f' /etc/hosts > "$tmp"
{ printf '%s\n' "$BEGIN"; printf '%s' "$block"; printf '%s\n' "$END"; } >> "$tmp"

if cmp -s "$tmp" /etc/hosts; then
    rm -f "$tmp"
    exit 0            # no change - stay quiet so the timer log isn't noise
fi
cat "$tmp" > /etc/hosts     # preserves ownership/permissions; mv would not
rm -f "$tmp"
echo "updated /etc/hosts:"
printf '%s' "$block"
