#!/usr/bin/env bash
#
# XVF3800 Capture + HiFiBerry DAC+ Lite Playback - Boot-Time Overlay Installer
#
# This script installs a device-tree overlay that exposes:
# - XVF3800 capture (spdif-dir codec)
# - DAC+ Lite playback (pcm512x codec)
#
# Usage:
#   /ai/speaker/xvf3800/xvf3800_dacplus_duplex_overlay_install.sh install
#   /ai/speaker/xvf3800/xvf3800_dacplus_duplex_overlay_install.sh validate
#   /ai/speaker/xvf3800/xvf3800_dacplus_duplex_overlay_install.sh remove
#
set -euo pipefail

OVERLAY_NAME="xvf3800-dacplus-duplex"
DTS_PATH="/tmp/${OVERLAY_NAME}.dts"
DTBO_DIR="/boot/firmware/overlays"
DTBO_PATH="${DTBO_DIR}/${OVERLAY_NAME}.dtbo"
CONFIG_TXT="/boot/firmware/config.txt"
WAV_PATH="/tmp/xvf3800_dacplus.wav"
I2S_FORMAT="${I2S_FORMAT:-i2s}"
I2S_MASTER="${I2S_MASTER:-cpu}"
I2S_CPU_DAI="${I2S_CPU_DAI:-i2s_clk_producer}"
CAPTURE_TDM_SLOT_NUM="${CAPTURE_TDM_SLOT_NUM:-2}"
CAPTURE_TDM_SLOT_WIDTH="${CAPTURE_TDM_SLOT_WIDTH:-32}"
PLAYBACK_TDM_SLOT_NUM="${PLAYBACK_TDM_SLOT_NUM:-2}"
PLAYBACK_TDM_SLOT_WIDTH="${PLAYBACK_TDM_SLOT_WIDTH:-32}"

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
err() { printf '\n[ERROR] %s\n' "$*" >&2; }
ok()  { printf '[OK] %s\n' "$*"; }

need_root() {
  if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root (use sudo)"
    exit 1
  fi
}

write_overlay_dts() {
  log "Writing overlay DTS to ${DTS_PATH}"
  local master_lines_capture=""
  local master_lines_playback=""
  local extra_fragment=""
  local audio_fragment="3"
  local i2s_disable_fragment=""
  local i2s_enable_fragment=""

  case "${I2S_MASTER}" in
    cpu)
      master_lines_capture=$'          bitclock-master = <&cpu_dai>;\n          frame-master    = <&cpu_dai>;\n'
      master_lines_playback=$'          bitclock-master = <&cpu_dai_p>;\n          frame-master    = <&cpu_dai_p>;\n'
      ;;
    none)
      master_lines_capture=""
      master_lines_playback=""
      ;;
    *)
      err "Invalid I2S_MASTER=${I2S_MASTER}. Use: cpu | none"
      exit 1
      ;;
  esac

  case "${I2S_CPU_DAI}" in
    rp1_i2s1)
      i2s_disable_fragment=$'  /* Disable rp1_i2s0 so it does NOT claim GPIO 18-21 */\n  fragment@0 {\n    target = <&rp1_i2s0>;\n    __overlay__ { status = "disabled"; };\n  };\n\n'
      i2s_enable_fragment=$'  /* Enable rp1_i2s1 with pinctrl for GPIO 18-21 */\n  fragment@1 {\n    target = <&rp1_i2s1>;\n    __overlay__ {\n      status = "okay";\n      pinctrl-names = "default";\n      pinctrl-0 = <&rp1_i2s1_18_21>;\n    };\n  };\n\n'
      ;;
    rp1_i2s0|i2s_clk_producer|i2s_clk_consumer)
      i2s_disable_fragment=$'  /* Disable rp1_i2s1 so it does NOT claim GPIO 18-21 */\n  fragment@0 {\n    target = <&rp1_i2s1>;\n    __overlay__ { status = "disabled"; };\n  };\n\n'
      i2s_enable_fragment=$'  /* Enable rp1_i2s0 with pinctrl for GPIO 18-21 */\n  fragment@1 {\n    target = <&rp1_i2s0>;\n    __overlay__ {\n      status = "okay";\n      pinctrl-names = "default";\n      pinctrl-0 = <&rp1_i2s0_18_21>;\n    };\n  };\n\n'
      ;;
    *)
      err "Invalid I2S_CPU_DAI=${I2S_CPU_DAI}. Use: rp1_i2s0 | rp1_i2s1 | i2s_clk_producer | i2s_clk_consumer"
      exit 1
      ;;
  esac

  case "${I2S_CPU_DAI}" in
    rp1_i2s0|rp1_i2s1)
      ;; # already enabled in fragment@1
    i2s_clk_producer|i2s_clk_consumer)
      printf -v extra_fragment '  fragment@2 {\n    target = <&%s>;\n    __overlay__ { status = "okay"; };\n  };\n\n' "${I2S_CPU_DAI}"
      audio_fragment="4"
      ;;
  esac

  cat >"${DTS_PATH}" <<DTS
/dts-v1/;
/plugin/;

/ {
  compatible = "brcm,bcm2712", "brcm,bcm2835";

${i2s_disable_fragment}${i2s_enable_fragment}

${extra_fragment}  /* DAC+ Lite codec on I2C1 */
  fragment@${audio_fragment} {
    target = <&i2c1>;
    __overlay__ {
      status = "okay";
      #address-cells = <1>;
      #size-cells = <0>;

      dac_codec: pcm512x@4d {
        compatible = "ti,pcm5122";
        reg = <0x4d>;
        #sound-dai-cells = <0>;
        status = "okay";
      };
    };
  };

  /* XVF3800 capture + DAC+ Lite playback */
  fragment@$((${audio_fragment} + 1)) {
    target-path = "/";
    __overlay__ {
      xvf_codec: spdif-receiver {
        #sound-dai-cells = <0>;
        compatible = "linux,spdif-dir";
        status = "okay";
      };

      xvf3800_dac_card: xvf3800-dacplus-duplex {
        compatible = "simple-audio-card";
        simple-audio-card,name = "XVF3800-DACplus";
        status = "okay";

        simple-audio-card,dai-link@0 {
          format = "${I2S_FORMAT}";
${master_lines_capture}
          cpu_dai: cpu {
            sound-dai = <&${I2S_CPU_DAI}>;
            dai-tdm-slot-num   = <${CAPTURE_TDM_SLOT_NUM}>;
            dai-tdm-slot-width = <${CAPTURE_TDM_SLOT_WIDTH}>;
          };

          codec_dai: codec {
            sound-dai = <&xvf_codec>;
          };
        };

        simple-audio-card,dai-link@1 {
          format = "${I2S_FORMAT}";
${master_lines_playback}
          cpu_dai_p: cpu {
            sound-dai = <&${I2S_CPU_DAI}>;
            dai-tdm-slot-num   = <${PLAYBACK_TDM_SLOT_NUM}>;
            dai-tdm-slot-width = <${PLAYBACK_TDM_SLOT_WIDTH}>;
          };

          codec_dai_p: codec {
            sound-dai = <&dac_codec>;
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
  local errfile
  errfile="$(mktemp)"

  dtc -@ -I dts -O dtb -o "${DTBO_PATH}" "${DTS_PATH}" 2>"${errfile}"
  local rc=$?

  # Print non-trivial warnings, if any.
  grep -v 'Warning.*unit_address' "${errfile}" >&2 || true
  rm -f "${errfile}"

  if [[ ${rc} -ne 0 ]]; then
    err "DTBO compile failed. Fix DTS errors and rerun install."
    exit 1
  fi

  ok "DTBO compiled: ${DTBO_PATH}"
}

ensure_i2c_arm_enabled() {
  local line="dtparam=i2c_arm=on"

  if [[ ! -f "${CONFIG_TXT}" ]]; then
    err "Config file not found: ${CONFIG_TXT}"
    return 1
  fi

  if grep -qxF "${line}" "${CONFIG_TXT}" 2>/dev/null; then
    ok "i2c_arm already enabled"
    return 0
  fi

  log "Enabling I2C in ${CONFIG_TXT}: ${line}"
  echo "" >> "${CONFIG_TXT}"
  echo "# Enable I2C for DAC+ Lite" >> "${CONFIG_TXT}"
  echo "${line}" >> "${CONFIG_TXT}"
  ok "Config updated: ${line}"
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

  sed -i "s/^#*${line}$/${line}/" "${CONFIG_TXT}" 2>/dev/null || true

  if ! grep -qxF "${line}" "${CONFIG_TXT}" 2>/dev/null; then
    log "Adding to ${CONFIG_TXT}: ${line}"
    echo "" >> "${CONFIG_TXT}"
    echo "# XVF3800 capture + DAC+ Lite playback" >> "${CONFIG_TXT}"
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

find_card_index() {
  awk -v name="$1" '
    /^[[:space:]]*[0-9]+[[:space:]]+\[/ { card=$1 }
    $0 ~ name { print card; exit }
  ' /proc/asound/cards | tr -d ' '
}

print_diagnostics() {
  log "=== Diagnostics ==="

  echo ""
  echo "--- /proc/asound/cards ---"
  cat /proc/asound/cards

  echo ""
  echo "--- aplay -l ---"
  aplay -l 2>&1 || true

  echo ""
  echo "--- arecord -l ---"
  arecord -l 2>&1 || true
}

run_capture_test() {
  local card_idx
  card_idx="$(find_card_index "XVF3800-DACplus")"

  if [[ -z "${card_idx}" ]]; then
    err "ALSA card 'XVF3800-DACplus' not found"
    return 1
  fi

  ok "Found XVF3800-DACplus at card ${card_idx}"

  log "Running arecord test (2 seconds to ${WAV_PATH})"
  rm -f "${WAV_PATH}"

  set +e
  arecord -D "hw:${card_idx},0" -c 2 -r 48000 -f S32_LE -d 2 -vv "${WAV_PATH}" 2>&1
  local rc=$?
  set -e

  if [[ ${rc} -ne 0 ]]; then
    err "arecord failed with exit code ${rc}"
    return 1
  fi

  if [[ -f "${WAV_PATH}" ]]; then
    local size
    size=$(stat -c %s "${WAV_PATH}")
    ok "WAV file created: ${WAV_PATH} (${size} bytes)"
    return 0
  fi

  err "WAV file was not created"
  return 1
}

cmd_install() {
  need_root

  log "Installing XVF3800 capture + DAC+ Lite playback overlay"
  echo ""
  echo "This overlay:"
  echo "  - Enables selected RP1 I2S controller on GPIO 18-21"
  echo "  - Disables the unused RP1 I2S controller on GPIO 18-21"
  echo "  - Enables I2C1 and adds PCM512x codec"
  echo "  - Creates ALSA card 'XVF3800-DACplus'"
  echo "  - I2S format: ${I2S_FORMAT}"
  echo "  - Master: ${I2S_MASTER} (cpu | none)"
  echo "  - CPU DAI: ${I2S_CPU_DAI}"
  echo "  - Capture TDM slots: ${CAPTURE_TDM_SLOT_NUM} x ${CAPTURE_TDM_SLOT_WIDTH}"
  echo "  - Playback TDM slots: ${PLAYBACK_TDM_SLOT_NUM} x ${PLAYBACK_TDM_SLOT_WIDTH}"
  echo ""

  write_overlay_dts
  compile_overlay
  ensure_i2c_arm_enabled
  add_to_config

  echo ""
  log "Installation complete!"
  echo ""
  echo "Reboot required: sudo reboot"
}

cmd_validate() {
  log "Post-reboot validation"
  print_diagnostics
  echo ""
  if run_capture_test; then
    ok "Capture test passed"
  else
    err "Capture test failed"
    exit 1
  fi
}

cmd_remove() {
  need_root
  log "Removing overlay from boot config"
  remove_from_config
  echo ""
  echo "Reboot to apply changes: sudo reboot"
}

show_help() {
  cat <<EOF
XVF3800 Capture + HiFiBerry DAC+ Lite Playback - Boot-Time Overlay Installer

Usage: $0 <command>

Optional environment variables:
  I2S_FORMAT=i2s|left_j|dsp_a|dsp_b (default: i2s)
  I2S_MASTER=cpu|none               (default: cpu)
  I2S_CPU_DAI=rp1_i2s0|rp1_i2s1|i2s_clk_producer|i2s_clk_consumer (default: i2s_clk_producer)
  CAPTURE_TDM_SLOT_NUM=2|4|8        (default: 2)
  CAPTURE_TDM_SLOT_WIDTH=16|24|32   (default: 32)
  PLAYBACK_TDM_SLOT_NUM=2           (default: 2)
  PLAYBACK_TDM_SLOT_WIDTH=16|24|32  (default: 32)

Commands:
  install   Install overlay and add to config.txt (requires reboot)
  validate  Post-reboot validation and arecord test
  remove    Remove overlay from config.txt (requires reboot)

Wiring (Pi as master):
  BCLK     GPIO18 (Pin 12)
  LRCLK    GPIO19 (Pin 35)
  DOUT     GPIO21 (Pin 40) -> DAC+ Lite DIN
  DOUT     GPIO21 (Pin 40) -> XVF3800 DIN (optional AEC)
  DOUT     GPIO20 (Pin 38) <- XVF3800 DOUT
  GND      Pin 6
EOF
}

case "${1:-}" in
  install)  cmd_install ;;
  validate) cmd_validate ;;
  remove)   cmd_remove ;;
  -h|--help|help) show_help ;;
  *) show_help; exit 1 ;;
esac
