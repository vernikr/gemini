#!/usr/bin/env bash
# harden_sshd.sh — kill password brute-force on :22 WITHOUT firewall rules
# (deployment notes: never block 22/443/20724 in ufw/iptables). Idempotent drop-in.
# SAFETY: run from an OPEN ssh session; requires root key login to already work.
set -euo pipefail

test -s /root/.ssh/authorized_keys || {
  echo "ABORT: /root/.ssh/authorized_keys missing/empty — key login must work first" >&2
  exit 1; }

mkdir -p /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/90-gemini-hardening.conf <<'EOF'
# gemini-mcp M5: key-only root login, no password brute-force surface
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
MaxAuthTries 3
LoginGraceTime 20
EOF
chmod 644 /etc/ssh/sshd_config.d/90-gemini-hardening.conf

sshd -t || { echo "ABORT: sshd -t failed, config left but not applied" >&2; exit 1; }
systemctl reload ssh
echo "OK: sshd hardened (key-only). Verify a NEW ssh login works before closing this session."
