#!/usr/bin/env bash
# Host firewall for the Docker sandbox driver. Read it before running it as root.
#
# Sandboxes run researcher-controlled code. The driver already keeps them apart
# (a private bridge network per project, or a shared one with inter-container
# traffic disabled) and drops all capabilities, but a container can still reach
# anything the host can route to. These rules stop sandbox traffic to:
#
#   * cloud metadata endpoints and other link-local addresses
#   * private networks: your VPC, databases, other hosts, the Docker bridges
#   * the host itself, except the control plane port in "port" access mode
#
# while public internet egress keeps working (package installs, literature APIs,
# LLM providers for bring-your-own-key setups go through the control plane anyway).
#
# Sandbox networks are recognised by their bridge interface names: the driver
# names every one "sbx-..." (CP_DOCKER_ACCESS=network) or "sbx-shared" (port), so
# these static rules cover projects created later too. Re-apply after a reboot
# (iptables-save > /etc/iptables/rules.v4 with iptables-persistent, or a unit file).
#
#   sudo ./harden-host.sh                 # control plane in a container (compose)
#   sudo CP_PORT=8000 ./harden-host.sh    # control plane on the host (port access)
set -euo pipefail

CHAIN=AGENTSOCIETY-SANDBOX
CP_PORT="${CP_PORT:-}"

if ! iptables -L DOCKER-USER -n >/dev/null 2>&1; then
  echo "DOCKER-USER chain not found: start Docker first" >&2
  exit 1
fi

# ---- forwarded traffic (sandbox -> other networks) -------------------------------
iptables -N "$CHAIN" 2>/dev/null || iptables -F "$CHAIN"
# Replies to connections the control plane opened into the sandbox.
iptables -A "$CHAIN" -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN
# Traffic that stays on the sandbox's own bridge (the control plane joins it).
# Docker's own isolation rules, evaluated after DOCKER-USER, still separate bridges.
iptables -A "$CHAIN" -o sbx+ -j RETURN
for cidr in 169.254.0.0/16 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 127.0.0.0/8 0.0.0.0/8 224.0.0.0/4 240.0.0.0/4; do
  iptables -A "$CHAIN" -d "$cidr" -j DROP
done
iptables -A "$CHAIN" -j RETURN
iptables -C DOCKER-USER -i sbx+ -j "$CHAIN" 2>/dev/null || iptables -I DOCKER-USER 1 -i sbx+ -j "$CHAIN"

# ---- traffic to the host itself ---------------------------------------------------
iptables -N "$CHAIN-IN" 2>/dev/null || iptables -F "$CHAIN-IN"
iptables -A "$CHAIN-IN" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
if [ -n "$CP_PORT" ]; then
  iptables -A "$CHAIN-IN" -p tcp --dport "$CP_PORT" -j ACCEPT
fi
iptables -A "$CHAIN-IN" -j DROP
iptables -C INPUT -i sbx+ -j "$CHAIN-IN" 2>/dev/null || iptables -I INPUT 1 -i sbx+ -j "$CHAIN-IN"

# ---- IPv6 -------------------------------------------------------------------------
# Sandbox networks are IPv4-only; refuse anything else from them outright.
if command -v ip6tables >/dev/null 2>&1; then
  ip6tables -C FORWARD -i sbx+ -j DROP 2>/dev/null || ip6tables -I FORWARD 1 -i sbx+ -j DROP
  ip6tables -C INPUT -i sbx+ -j DROP 2>/dev/null || ip6tables -I INPUT 1 -i sbx+ -j DROP
fi

echo "sandbox egress rules installed:"
iptables -S "$CHAIN"
iptables -S "$CHAIN-IN"
