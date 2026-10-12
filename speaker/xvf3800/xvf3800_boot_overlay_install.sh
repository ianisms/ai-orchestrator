#!/usr/bin/env bash
#
# XVF3800 I2S Capture - Boot-Time Overlay Installer
#
# This script installs a device-tree overlay for boot-time application.
# Runtime overlay application fails due to pin conflicts; a reboot is required.
#
# Usage:
#   /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh install   # Install overlay + update config.txt
#   /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh validate  # Post-reboot validation + arecord test
#   /ai/speaker/xvf3800/xvf3800_boot_overlay_install.sh remove    # Remove overlay from config.txt
#
set -euo pipefail

OVERLAY_NAME="xvf3800-i2s-capture"
DTS_PATH="/tmp/${OVERLAY_NAME}.dts"
DTBO_DIR="/boot/firmware/overlays"
DTBO_PATH="${DTBO_DIR}/${OVERLAY_NAME}.dtbo"
CONFIG_TXT="/boot/firmware/config.txt"
WAV_PATH="/tmp/xvf3800.wav"
I2S_FORMAT="${I2S_FORMAT:-i2s}"
I2S_MASTER="${I2S_MASTER:-codec}"
I2S_CPU_DAI="${I2S_CPU_DAI:-rp1_i2s1}"
I2S_CODEC="${I2S_CODEC:-spdif-dir}"

log() { printf '\n\033[1;34m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
err() { printf '\n\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }
ok()  { printf '\033[1;32m✓\033[0m %s\n' "$*"; }

need_root() {
  if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (use sudo)"
    exit 1
  fi
}

write_overlay_dts() {
  log "Writing overlay DTS to ${DTS_PATH}"
  local master_lines=""
  local extra_fragment=""
  local audio_fragment="2"
  local codec_node=""
  case "${I2S_MASTER}" in
    cpu)
      master_lines=$'          bitclock-master = <&cpu_dai>;\n          frame-master    = <&cpu_dai>;\n'
      ;;
    codec)
      master_lines=$'          bitclock-master = <&codec_dai>;\n          frame-master    = <&codec_dai>;\n'
      ;;
    none)
      master_lines=""
      ;;
    *)
      err "Invalid I2S_MASTER=${I2S_MASTER}. Use: cpu | codec | none"
      exit 1
      ;;
  esac

  case "${I2S_CODEC}" in
    spdif-dir)
      codec_node=$'      xvf_codec: spdif-receiver {\n        #sound-dai-cells = <0>;\n        compatible = "linux,spdif-dir";\n        status = "okay";\n      };\n'
      ;;
    dummy)
      codec_node=$'      xvf_codec: dummy-codec {\n        #sound-dai-cells = <0>;\n        compatible = "linux,snd-soc-dummy";\n        status = "okay";\n      };\n'
      ;;
    *)
      err "Invalid I2S_CODEC=${I2S_CODEC}. Use: spdif-dir | dummy"
      exit 1
      ;;
  esac

  case "${I2S_CPU_DAI}" in
    rp1_i2s1)
      ;; # already enabled in fragment@1
    i2s_clk_producer|i2s_clk_consumer)
      printf -v extra_fragment '  fragment@2 {\n    target = <&%s>;\n    __overlay__ { status = "okay"; };\n  };\n\n' "${I2S_CPU_DAI}"
      audio_fragment="3"
      ;;
    *)
      err "Invalid I2S_CPU_DAI=${I2S_CPU_DAI}. Use: rp1_i2s1 | i2s_clk_producer | i2s_clk_consumer"
      exit 1
      ;;
  esac

  cat >"${DTS_PATH}" <<DTS
/dts-v1/;
/plugin/;

/ {
  compatible = "brcm,bcm2712", "brcm,bcm2835";

  /* Disable rp1_i2s0 so it does NOT claim GPIO 18-21 at boot */
  fragment@0 {
    target = <&rp1_i2s0>;
    __overlay__ { status = "disabled"; };
  };

  /* Enable rp1_i2s1 with pinctrl for GPIO 18-21 */
  fragment@1 {
    target = <&rp1_i2s1>;
    __overlay__ {
      status = "okay";
      pinctrl-names = "default";
      pinctrl-0 = <&rp1_i2s1_18_21>;
    };
  };

${extra_fragment}  /* Dummy codec (spdif-dir) + simple-audio-card */
  fragment@${audio_fragment} {
    target-path = "/";
    __overlay__ {
${codec_node}

      xvf3800_card: xvf3800-i2s-capture {
        compatible = "simple-audio-card";
        simple-audio-card,name = "XVF3800-I2S-Capture";
        status = "okay";

        simple-audio-card,dai-link@0 {
          format = "${I2S_FORMAT}";
${master_lines}

          cpu_dai: cpu {
            sound-dai = <&${I2S_CPU_DAI}>;
            dai-tdm-slot-num   = <2>;
            dai-tdm-slot-width = <32>;
          };

          codec_dai: codec {
            sound-dai = <&xvf_codec>;
          };
        };
      };
    };
  };
};
DTS
  ok "DTS written"
}

compile_overlay() {
  log "Compiling DTBO to ${DTBO_PATH}"
  mkdir -p "${DTBO_DIR}"
  dtc -@ -I dts -O dtb -o "${DTBO_PATH}" "${DTS_PATH}" 2>&1 | grep -v 'Warning.*unit_address' || true
  ok "DTBO compiled: ${DTBO_PATH}"
}

add_to_config() {
  local line="dtoverlay=${OVERLAY_NAME}"

  if [[ ! -f "${CONFIG_TXT}" ]]; then
    err "Config file not found: ${CONFIG_TXT}"
    return 1
  fi

  if grep -qxF "${line}" "${CONFIG_TXT}" 2>/dev/null; then
    ok "Already in config.txt: ${line}"
    return 0
  fi

  # Remove any commented version first
  sed -i "s/^#*${line}$/${line}/" "${CONFIG_TXT}" 2>/dev/null || true

  if ! grep -qxF "${line}" "${CONFIG_TXT}" 2>/dev/null; then
    log "Adding to ${CONFIG_TXT}: ${line}"
    echo "" >> "${CONFIG_TXT}"
    echo "# XVF3800 I2S capture (Pi as slave, XVF as master)" >> "${CONFIG_TXT}"
    echo "${line}" >> "${CONFIG_TXT}"
  fi

  ok "Config updated: ${line}"
}

remove_from_config() {
  local line="dtoverlay=${OVERLAY_NAME}"

  if [[ ! -f "${CONFIG_TXT}" ]]; then
    err "Config file not found: ${CONFIG_TXT}"
    return 1
  fi

  if grep -qE "^${line}$" "${CONFIG_TXT}" 2>/dev/null; then
    log "Commenting out in ${CONFIG_TXT}: ${line}"
    sed -i "s/^${line}$/#${line}/" "${CONFIG_TXT}"
    ok "Overlay disabled in config.txt"
  else
    ok "Overlay not found in config.txt (already removed)"
  fi
}

check_overlay_loaded() {
  if dtoverlay -l 2>/dev/null | grep -q "${OVERLAY_NAME}"; then
    return 0
  fi
  # Also check if it was applied at boot (won't show in dtoverlay -l)
  if [[ -d "/sys/firmware/devicetree/base/xvf3800-i2s-capture" ]] || \
     [[ -d "/proc/device-tree/xvf3800-i2s-capture" ]]; then
    return 0
  fi
  return 1
}

find_card_index() {
  awk '
    /^[[:space:]]*[0-9]+[[:space:]]+\[/ { card=$1 }
    /XVF3800-I2S-Capture/ { print card; exit }
  ' /proc/asound/cards | tr -d ' '
}

print_diagnostics() {
  log "=== Diagnostics ==="

  echo ""
  echo "--- dtoverlay -l ---"
  dtoverlay -l 2>/dev/null || echo "(none loaded at runtime)"

  echo ""
  echo "--- DT nodes ---"
  for node in rp1_i2s0 rp1_i2s1; do
    local sym="/proc/device-tree/__symbols__/${node}"
    if [[ -r "${sym}" ]]; then
      local path=$(tr -d '\0' < "${sym}")
      local status=$(tr -d '\0' < "/proc/device-tree${path}/status" 2>/dev/null || echo "(missing)")
      echo "${node}: status=${status}"
    fi
  done

  if [[ -d "/proc/device-tree/xvf3800-i2s-capture" ]]; then
    ok "xvf3800-i2s-capture node exists in device-tree"
  else
    echo "xvf3800-i2s-capture node: NOT found"
  fi

  echo ""
  echo "--- /proc/asound/cards ---"
  cat /proc/asound/cards

  echo ""
  echo "--- arecord -l ---"
  arecord -l 2>&1 || true
}

collect_dmesg() {
  log "=== Relevant dmesg ==="
  dmesg --color=never | grep -iE 'rp1.*i2s|designware-i2s|asoc|simple.*card|spdif|xvf|set_fmt' | tail -n 40 || true
}

run_arecord_test() {
  local card_idx
  card_idx="$(find_card_index)"

  if [[ -z "${card_idx}" ]]; then
    err "ALSA card 'XVF3800-I2S-Capture' not found"
    collect_dmesg
    return 1
  fi

  ok "Found XVF3800-I2S-Capture at card ${card_idx}"

  log "Running arecord test (2 seconds to ${WAV_PATH})"
  rm -f "${WAV_PATH}"

  set +e
  arecord -D "hw:${card_idx},0" -c 2 -r 48000 -f S32_LE -d 2 -vv "${WAV_PATH}" 2>&1
  local rc=$?
  set -e

  if [[ ${rc} -ne 0 ]]; then
    err "arecord failed with exit code ${rc}"
    collect_dmesg
    return 1
  fi

  if [[ -f "${WAV_PATH}" ]]; then
    local size=$(stat -c %s "${WAV_PATH}")
    echo ""
    ok "WAV file created: ${WAV_PATH} (${size} bytes)"

    if [[ ${size} -gt 100 ]]; then
      ok "SUCCESS: Stream started and captured data"
      echo ""
      echo "To check if audio contains actual signal (not just zeros):"
      echo "  hexdump -C ${WAV_PATH} | head -n 20"
      echo "  aplay ${WAV_PATH}  # (if you have speakers connected)"
      return 0
    else
      err "WAV file too small (${size} bytes) - likely no audio data"
      return 1
    fi
  else
    err "WAV file was not created"
    return 1
  fi
}

cmd_install() {
  need_root

  log "Installing XVF3800 I2S capture overlay for boot-time application"
  echo ""
  echo "This overlay:"
  echo "  • Disables rp1_i2s0 (frees GPIO 18-21)"
  echo "  • Enables rp1_i2s1 on GPIO 18-21"
  echo "  • Creates ALSA card 'XVF3800-I2S-Capture'"
  echo "  • I2S format: ${I2S_FORMAT}"
  echo "  • Master: ${I2S_MASTER} (cpu | codec | none)"
  echo "  • Codec: ${I2S_CODEC} (spdif-dir=capture-only | dummy=full-duplex)"
  echo ""

  write_overlay_dts
  compile_overlay
  add_to_config

  echo ""
  log "Installation complete!"
  echo ""
  echo "┌─────────────────────────────────────────────────────────────┐"
  echo "│  REBOOT REQUIRED                                            │"
  echo "│                                                             │"
  echo "│  The overlay will be applied at boot time.                  │"
  echo "│  Run: sudo reboot                                           │"
  echo "│                                                             │"
  echo "│  After reboot, validate with:                               │"
  echo "│  sudo $0 validate                              │"
  echo "└─────────────────────────────────────────────────────────────┘"
}

cmd_validate() {
  log "Post-reboot validation"

  print_diagnostics

  echo ""
  if run_arecord_test; then
    echo ""
    echo "┌─────────────────────────────────────────────────────────────┐"
    echo "│  SUCCESS!                                                   │"
    echo "│                                                             │"
    echo "│  XVF3800 I2S capture is working.                            │"
    echo "│  ALSA device: hw:$(find_card_index),0                                      │"
    echo "└─────────────────────────────────────────────────────────────┘"
  else
    echo ""
    echo "┌─────────────────────────────────────────────────────────────┐"
    echo "│  FAILED                                                     │"
    echo "│                                                             │"
    echo "│  Check dmesg output above for errors.                       │"
    echo "│  Common issue: designware-i2s rejecting slave mode (-22)    │"
    echo "└─────────────────────────────────────────────────────────────┘"
    exit 1
  fi
}

cmd_remove() {
  need_root
  log "Removing XVF3800 overlay from boot config"
  remove_from_config
  echo ""
  echo "Reboot to apply changes: sudo reboot"
}

cmd_status() {
  log "Current status"
  print_diagnostics
}

show_help() {
  cat <<EOF
XVF3800 I2S Capture - Boot-Time Overlay Installer

Usage: $0 <command>

Optional environment variables:
  I2S_FORMAT=i2s|left_j|dsp_a|dsp_b (default: i2s)
  I2S_MASTER=cpu|codec|none         (default: codec)
  I2S_CODEC=spdif-dir|dummy         (default: spdif-dir)

Commands:
  install   Install overlay and add to config.txt (requires reboot)
  validate  Post-reboot validation and arecord test
  remove    Remove overlay from config.txt (requires reboot)
  status    Show current DT and ALSA status

Wiring (XVF3800 I2S master firmware):
  BCLK     GPIO18 (Pin 12) ← X0D37
  LRCLK    GPIO19 (Pin 35) ← X0D38
  DOUT     GPIO20 (Pin 38) ← X1D00 (XVF→Pi)
  GND      Pin 6           ← GND

EOF
}

# Main
case "${1:-}" in
  install)  cmd_install ;;
  validate) cmd_validate ;;
  remove)   cmd_remove ;;
  status)   cmd_status ;;
  -h|--help|help) show_help ;;
  *)
    show_help
    exit 1
    ;;
esac
