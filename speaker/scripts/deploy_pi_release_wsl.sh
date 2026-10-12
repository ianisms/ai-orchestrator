#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOT_MOUNT="${PI_BOOT_MOUNT:-/mnt/e}"
PI_USER="${PI_USER:-pi}"
SSH_PUB="${SSH_PUB:-$HOME/.ssh/id_rsa.pub}"
PI_AUDIO_DRIVER="${PI_AUDIO_DRIVER:-alsa}"
PI_MIC_DEVICE="${PI_MIC_DEVICE:-default}"
PI_PLAYBACK_DEVICE="${PI_PLAYBACK_DEVICE:-default}"
PI_DEV_MODE="${PI_DEV_MODE:-false}"

if [ ! -d "${BOOT_MOUNT}" ] || [ ! -f "${BOOT_MOUNT}/cmdline.txt" ]; then
  echo "Boot partition not found at ${BOOT_MOUNT} (missing cmdline.txt)." >&2
  exit 1
fi

"${ROOT}/scripts/build_pi_release.sh"

mkdir -p "${BOOT_MOUNT}/ai"
rm -rf "${BOOT_MOUNT}/ai/speaker"
mkdir -p "${BOOT_MOUNT}/ai/speaker"
# FAT boot partitions don't support symlinks; dereference if any remain.
cp -aL "${ROOT}/pi-release/ai/speaker/." "${BOOT_MOUNT}/ai/speaker/"

mkdir -p "${BOOT_MOUNT}/ai/speaker/docker"
cat > "${BOOT_MOUNT}/ai/speaker/docker/.env" <<EOF
SPEAKER_AUDIO_DRIVER=${PI_AUDIO_DRIVER}
SPEAKER_MIC_DEVICE=${PI_MIC_DEVICE}
SPEAKER_PLAYBACK_DEVICE=${PI_PLAYBACK_DEVICE}
SPEAKER_DEV_MODE=${PI_DEV_MODE}
EOF

if [ -f "${SSH_PUB}" ]; then
  cp "${SSH_PUB}" "${BOOT_MOUNT}/authorized_keys"
else
  echo "Warning: SSH public key not found at ${SSH_PUB}" >&2
fi

cat > "${BOOT_MOUNT}/firstrun.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail

LOG="/var/log/ai-speaker-firstboot.log"
exec > >(tee -a "\${LOG}") 2>&1

BOOT_DIR="/boot/firmware"
if [ ! -d "\${BOOT_DIR}" ]; then
  BOOT_DIR="/boot"
fi

USER_NAME="${PI_USER}"
SRC_DIR="\${BOOT_DIR}/ai/speaker"
DST_DIR="/ai/speaker"

if [ -d "\${SRC_DIR}" ]; then
  mkdir -p "/ai"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "\${SRC_DIR}/" "\${DST_DIR}/"
  else
    cp -a "\${SRC_DIR}/." "\${DST_DIR}/"
  fi
  if id "\${USER_NAME}" >/dev/null 2>&1; then
    chown -R "\${USER_NAME}:\${USER_NAME}" "\${DST_DIR}" || true
  fi
fi

if [ -f "\${BOOT_DIR}/authorized_keys" ] && id "\${USER_NAME}" >/dev/null 2>&1; then
  install -d -m 700 -o "\${USER_NAME}" -g "\${USER_NAME}" "/home/\${USER_NAME}/.ssh"
  cat "\${BOOT_DIR}/authorized_keys" > "/home/\${USER_NAME}/.ssh/authorized_keys"
  chown "\${USER_NAME}:\${USER_NAME}" "/home/\${USER_NAME}/.ssh/authorized_keys"
  chmod 600 "/home/\${USER_NAME}/.ssh/authorized_keys"
fi

if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_expand_rootfs || true
fi

if command -v apt-get >/dev/null 2>&1; then
  apt-get update || true
  apt-get install -y docker.io rsync alsa-utils usbutils || true
  systemctl enable docker || true
  if id "\${USER_NAME}" >/dev/null 2>&1; then
    usermod -aG docker,audio "\${USER_NAME}" || true
  fi
fi

modprobe snd_usb_audio >/dev/null 2>&1 || true

if command -v arecord >/dev/null 2>&1; then
  if arecord -l 2>/dev/null | grep -qi 'ReSpeaker\\|XVF3800'; then
    card_id="\$(arecord -l 2>/dev/null | awk -F'[: ]+' '/ReSpeaker|XVF3800/ {print \$3; exit}')"
    if [ -n "\${card_id}" ]; then
      cat > /etc/asound.conf <<CONF
defaults.pcm.card \${card_id}
defaults.ctl.card \${card_id}
CONF
    fi
  fi
fi

if command -v lsusb >/dev/null 2>&1; then
  vidpid="\$(lsusb | awk '/ReSpeaker|XVF3800/ {print \$6; exit}')"
  if [ -n "\${vidpid}" ]; then
    vid="\${vidpid%:*}"
    pid="\${vidpid#*:}"
    cat > /etc/udev/rules.d/99-respeaker-usb-pm.rules <<CONF
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="\${vid}", ATTR{idProduct}=="\${pid}", TEST=="power/control", ATTR{power/control}="on"
CONF
    udevadm control --reload-rules || true
  fi
fi

if command -v nmcli >/dev/null 2>&1; then
  cat > /etc/NetworkManager/conf.d/wifi-powersave-off.conf <<'CONF'
[connection]
wifi.powersave = 2
CONF
  systemctl restart NetworkManager || true
fi

if [ -f "\${BOOT_DIR}/cmdline.txt" ]; then
  sed -i 's@ systemd.run=/boot/firmware/firstrun.sh@@' "\${BOOT_DIR}/cmdline.txt" || true
  sed -i 's@ systemd.run_success_action=reboot@@' "\${BOOT_DIR}/cmdline.txt" || true
  sed -i 's@ systemd.run_failure_action=reboot@@' "\${BOOT_DIR}/cmdline.txt" || true
  sed -i 's@ systemd.run=/boot/firstrun.sh@@' "\${BOOT_DIR}/cmdline.txt" || true
fi

rm -f "\${BOOT_DIR}/firstrun.sh" "\${BOOT_DIR}/authorized_keys" || true
rm -rf "\${BOOT_DIR}/ai" || true
sync
EOF

chmod +x "${BOOT_MOUNT}/firstrun.sh"

CMDLINE="$(cat "${BOOT_MOUNT}/cmdline.txt")"
if [[ "${CMDLINE}" != *"systemd.run="* ]]; then
  CMDLINE="${CMDLINE} systemd.run=/boot/firmware/firstrun.sh systemd.run_success_action=reboot systemd.run_failure_action=reboot"
else
  CMDLINE="$(printf '%s' "${CMDLINE}" | sed -E 's@systemd\\.run=[^ ]+@systemd.run=/boot/firmware/firstrun.sh@')"
  if [[ "${CMDLINE}" != *"systemd.run_success_action="* ]]; then
    CMDLINE="${CMDLINE} systemd.run_success_action=reboot"
  fi
  if [[ "${CMDLINE}" != *"systemd.run_failure_action="* ]]; then
    CMDLINE="${CMDLINE} systemd.run_failure_action=reboot"
  fi
fi
printf '%s' "${CMDLINE}" > "${BOOT_MOUNT}/cmdline.txt"

touch "${BOOT_MOUNT}/ssh"

echo "Staged release to ${BOOT_MOUNT}/ai/speaker and wrote firstrun.sh."
