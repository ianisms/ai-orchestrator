#!/usr/bin/env bash
set -euo pipefail

DTBO_DIR="/boot/firmware/overlays"

# Overlay selection:
#   XVF_OVERLAY_MODE=exact     -> uses the prompt's original overlay (targets &sound, &rp1_i2s0)
#   XVF_OVERLAY_MODE=clean-i2s1-> creates a separate sound card node and targets &rp1_i2s1 + pinctrl
#
# Default is the clean overlay because it avoids overlay overlap with /soc.../sound and
# targets rp1_i2s1 (i2s@a4000), which may support slave mode better than rp1_i2s0 on some kernels.
XVF_OVERLAY_MODE="${XVF_OVERLAY_MODE:-clean-i2s1}"

OVERLAY_NAME="xvf3800-i2s-capture"
CLEAN_OVERLAY_NAME="xvf3800-i2s-capture-clean-i2s1"

DTS_PATH="/tmp/xvf3800-i2s-capture-overlay.dts"
CLEAN_DTS_PATH="/tmp/xvf3800-i2s-capture-clean-i2s1-overlay.dts"

DTBO_PATH="${DTBO_DIR}/${OVERLAY_NAME}.dtbo"
CLEAN_DTBO_PATH="${DTBO_DIR}/${CLEAN_OVERLAY_NAME}.dtbo"
WAV_PATH="/tmp/xvf3800.wav"
ALT_OVERLAY_NAME="xvf3800-i2s-capture-i2s1"
ALT_DTBO_PATH="${DTBO_DIR}/${ALT_OVERLAY_NAME}.dtbo"

# Optional troubleshooting mode:
#   XVF_TRY_I2S1=1 /ai/speaker/xvf3800/xvf3800_i2s_capture_setup.sh
# When enabled, if the rp1_i2s0-based overlay fails to create an ALSA card and dmesg
# shows set_fmt -22 on the DesignWare I2S DAI, the script will try an rp1_i2s1 variant.
XVF_TRY_I2S1="${XVF_TRY_I2S1:-0}"

log() { printf '\n[%s] %s\n' "$(date -Is)" "$*" >&2; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    return 1
  }
}

try_modprobe() {
  local mod="$1"
  if command -v modprobe >/dev/null 2>&1; then
    sudo modprobe "${mod}" >/dev/null 2>&1 || true
  fi
}

print_relevant_lsmod() {
  log "lsmod (filtered: snd|asoc|soc|spdif|i2s|designware)"
  if command -v lsmod >/dev/null 2>&1; then
    lsmod | egrep -i '(^Module|snd|asoc|soc|spdif|i2s|designware)' || true
  fi
}

udev_settle() {
  if command -v udevadm >/dev/null 2>&1; then
    sudo udevadm settle || true
  fi
}

ensure_debugfs() {
  if ! mountpoint -q /sys/kernel/debug; then
    log "Mounting debugfs at /sys/kernel/debug"
    sudo mount -t debugfs none /sys/kernel/debug
  fi
}

detect_config_path() {
  if [[ -f /boot/firmware/config.txt ]]; then
    echo /boot/firmware/config.txt
  elif [[ -f /boot/config.txt ]]; then
    echo /boot/config.txt
  else
    return 1
  fi
}

write_dts_exact() {
  log "Writing overlay DTS to ${DTS_PATH}"
  cat >"${DTS_PATH}" <<'DTS'
/dts-v1/;
/plugin/;

/ {
  compatible = "brcm,bcm2712", "brcm,bcm2835";

  fragment@0 {
    target = <&sound>;
    __overlay__ {
      compatible = "simple-audio-card";
      simple-audio-card,name = "XVF3800-I2S-Capture";
      status = "okay";

      simple-audio-card,dai-link@0 {
        format = "i2s";
        bitclock-master = <&codec_dai>;
        frame-master    = <&codec_dai>;

        cpu {
          sound-dai = <&rp1_i2s0>;
          dai-tdm-slot-num   = <2>;
          dai-tdm-slot-width = <32>;
        };

        codec_dai: codec {
          sound-dai = <&codec_in>;
        };
      };
    };
  };

  fragment@1 {
    target-path = "/";
    __overlay__ {
      codec_in: spdif-receiver {
        #sound-dai-cells = <0>;
        compatible = "linux,spdif-dir";
        status = "okay";
      };
    };
  };

  fragment@2 {
    target = <&rp1_i2s0>;
    __overlay__ { status = "okay"; };
  };
};
DTS
}

write_dts_clean_i2s1() {
  # Clean overlay variant:
  # - Does NOT target &sound (avoids overlay overlap with existing sound nodes)
  # - Creates its own simple-audio-card node under '/'
  # - Uses rp1_i2s1 (i2s@a4000) and sets the rp1_i2s1_18_21 pinctrl group
  log "Writing CLEAN overlay DTS (separate sound node, rp1_i2s1 + pinctrl) to ${CLEAN_DTS_PATH}"
  cat >"${CLEAN_DTS_PATH}" <<'DTS'
/dts-v1/;
/plugin/;

/ {
  compatible = "brcm,bcm2712", "brcm,bcm2835";

  fragment@0 {
    target = <&rp1_i2s0>;
    __overlay__ { status = "disabled"; };
  };

  fragment@1 {
    target = <&rp1_i2s1>;
    __overlay__ {
      status = "okay";
      pinctrl-names = "default";
      pinctrl-0 = <&rp1_i2s1_18_21>;
    };
  };

  fragment@2 {
    target-path = "/";
    __overlay__ {
      xvf_codec: spdif-receiver {
        #sound-dai-cells = <0>;
        compatible = "linux,spdif-dir";
        status = "okay";
      };

      xvf3800_card: xvf3800-i2s-capture {
        compatible = "simple-audio-card";
        simple-audio-card,name = "XVF3800-I2S-Capture";
        status = "okay";

        simple-audio-card,dai-link@0 {
          format = "i2s";
          bitclock-master = <&codec_dai>;
          frame-master    = <&codec_dai>;

          cpu {
            sound-dai = <&rp1_i2s1>;
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
}

write_dts_i2s1_variant() {
  # For troubleshooting only: identical overlay except rp1_i2s1 is used as the CPU DAI.
  # (The prompt's canonical overlay remains write_dts_exact above.)
  local dts_path="/tmp/${ALT_OVERLAY_NAME}-overlay.dts"
  log "Writing troubleshooting overlay DTS (rp1_i2s1) to ${dts_path}"
  cat >"${dts_path}" <<'DTS'
/dts-v1/;
/plugin/;

/ {
  compatible = "brcm,bcm2712", "brcm,bcm2835";

  fragment@0 {
    target = <&sound>;
    __overlay__ {
      compatible = "simple-audio-card";
      simple-audio-card,name = "XVF3800-I2S-Capture";
      status = "okay";

      simple-audio-card,dai-link@0 {
        format = "i2s";
        bitclock-master = <&codec_dai>;
        frame-master    = <&codec_dai>;

        cpu {
          sound-dai = <&rp1_i2s1>;
          dai-tdm-slot-num   = <2>;
          dai-tdm-slot-width = <32>;
        };

        codec_dai: codec {
          sound-dai = <&codec_in>;
        };
      };
    };
  };

  fragment@1 {
    target-path = "/";
    __overlay__ {
      codec_in: spdif-receiver {
        #sound-dai-cells = <0>;
        compatible = "linux,spdif-dir";
        status = "okay";
      };
    };
  };

  fragment@2 {
    target = <&rp1_i2s1>;
    __overlay__ { status = "okay"; };
  };
};
DTS
  echo "${dts_path}"
}

compile_overlay() {
  need_cmd dtc
  sudo mkdir -p "${DTBO_DIR}"

  case "${XVF_OVERLAY_MODE}" in
    exact)
      log "Compiling DTBO (exact) to ${DTBO_PATH}"
      sudo dtc -@ -I dts -O dtb -o "${DTBO_PATH}" "${DTS_PATH}"
      ;;
    clean-i2s1)
      log "Compiling DTBO (clean-i2s1) to ${CLEAN_DTBO_PATH}"
      sudo dtc -@ -I dts -O dtb -o "${CLEAN_DTBO_PATH}" "${CLEAN_DTS_PATH}"
      ;;
    *)
      echo "Unknown XVF_OVERLAY_MODE='${XVF_OVERLAY_MODE}' (use 'exact' or 'clean-i2s1')" >&2
      return 1
      ;;
  esac
}

active_overlay_name() {
  case "${XVF_OVERLAY_MODE}" in
    exact) echo "${OVERLAY_NAME}" ;;
    clean-i2s1) echo "${CLEAN_OVERLAY_NAME}" ;;
    *) echo "${OVERLAY_NAME}" ;;
  esac
}

overlay_loaded() {
  need_cmd dtoverlay
  local name
  name="$(active_overlay_name)"
  dtoverlay -l 2>/dev/null | awk '{print $2}' | grep -qx "${name}" || return 1
}

load_overlay() {
  need_cmd dtoverlay

  local name dtbo
  name="$(active_overlay_name)"
  case "${XVF_OVERLAY_MODE}" in
    exact) dtbo="${DTBO_PATH}" ;;
    clean-i2s1) dtbo="${CLEAN_DTBO_PATH}" ;;
    *) dtbo="${DTBO_PATH}" ;;
  esac

  log "(Re)loading overlay ${name} (mode=${XVF_OVERLAY_MODE})"

  # Runtime pinmux conflict note:
  # Pins 18–21 are owned by rp1_i2s0 on many CM5/Pi5 DTs when it is probed.
  # If we want to use rp1_i2s1 on the same pins, we must unbind rp1_i2s0 first
  # so the pinctrl driver can re-assign the pins to rp1_i2s1.
  if [[ "${XVF_OVERLAY_MODE}" == "clean-i2s1" ]]; then
    if [[ -w /sys/bus/platform/drivers/designware-i2s/unbind ]] && [[ -L /sys/bus/platform/drivers/designware-i2s/1f000a0000.i2s ]]; then
      log "Unbinding rp1_i2s0 (1f000a0000.i2s) to free GPIO18-21 for rp1_i2s1"
      echo 1f000a0000.i2s | sudo tee /sys/bus/platform/drivers/designware-i2s/unbind >/dev/null || true
      sleep 0.1
    fi
  fi

  if overlay_loaded; then
    log "Overlay already loaded; removing first"
    sudo dtoverlay -r "${name}" || true
    sleep 0.2
  fi

  # Prefer overlay name (searches overlays dir); fallback to explicit path.
  if ! sudo dtoverlay -v "${name}"; then
    log "dtoverlay by name failed; trying explicit path"
    sudo dtoverlay -v "${dtbo}"
  fi

  # Encourage driver binding in minimal installs where autoloading may not happen.
  try_modprobe snd-soc-simple-card
  try_modprobe snd-soc-simple-card-utils
  try_modprobe snd-soc-spdif-rx
  try_modprobe snd-soc-spdif-tx
  try_modprobe designware_i2s
  udev_settle
  sleep 0.2
}

ensure_config_overlay_line() {
  local cfg name
  name="$(active_overlay_name)"
  cfg="$(detect_config_path)" || {
    echo "Could not find config.txt in /boot/firmware or /boot" >&2
    return 1
  }

  if sudo grep -Eq "^dtoverlay=${name}(\s|$)" "${cfg}"; then
    log "config.txt already contains dtoverlay=${name}"
    return 0
  fi

  log "Adding dtoverlay=${name} to ${cfg}"
  printf "\n# XVF3800 I2S capture overlay\ndtoverlay=%s\n" "${name}" | sudo tee -a "${cfg}" >/dev/null
}

print_reboot_instructions() {
  local name
  name="$(active_overlay_name)"
  cat <<TXT

=== Reboot required ===
Overlay ${name} has been installed to ${DTBO_DIR} and added to config.txt.
Please reboot, then re-run this script to validate:
  - dtoverlay -l shows ${name}
  - /proc/asound/cards contains XVF3800-I2S-Capture
  - arecord can start without immediate I/O errors
TXT
}

load_alt_overlay_i2s1() {
  need_cmd dtoverlay
  need_cmd dtc

  local alt_dts
  alt_dts="$(write_dts_i2s1_variant)"
  log "Compiling troubleshooting DTBO to ${ALT_DTBO_PATH}"
  sudo mkdir -p "${DTBO_DIR}"
  sudo dtc -@ -I dts -O dtb -o "${ALT_DTBO_PATH}" "${alt_dts}"

  log "Loading troubleshooting overlay (rp1_i2s1): ${ALT_OVERLAY_NAME}"
  if dtoverlay -l 2>/dev/null | awk '{print $2}' | grep -qx "${ALT_OVERLAY_NAME}"; then
    sudo dtoverlay -r "${ALT_OVERLAY_NAME}" || true
    sleep 0.2
  fi

  if ! sudo dtoverlay -v "${ALT_OVERLAY_NAME}"; then
    sudo dtoverlay -v "${ALT_DTBO_PATH}"
  fi

  try_modprobe snd-soc-simple-card
  try_modprobe snd-soc-spdif-rx
  try_modprobe designware_i2s
  udev_settle
  sleep 0.2
}

dt_sym_path() {
  local sym="$1"
  local f="/proc/device-tree/__symbols__/${sym}"
  [[ -r "${f}" ]] || return 1
  tr -d '\0' <"${f}"
}

dt_prop() {
  local node_path="$1"
  local prop="$2"
  local f="/proc/device-tree${node_path}/${prop}"
  [[ -r "${f}" ]] || return 1
  tr -d '\0' <"${f}"
}

print_diagnostics() {
  log "dtoverlay -l"
  dtoverlay -l || true

  local sound_path rp1_i2s0_path rp1_i2s1_path
  sound_path="$(dt_sym_path sound 2>/dev/null || true)"
  rp1_i2s0_path="$(dt_sym_path rp1_i2s0 2>/dev/null || true)"
  rp1_i2s1_path="$(dt_sym_path rp1_i2s1 2>/dev/null || true)"

  if [[ -n "${sound_path}" ]]; then
    log "DT symbol sound -> /proc/device-tree${sound_path}"
    log "DT sound status: $(dt_prop "${sound_path}" status 2>/dev/null || echo '(missing)')"
    log "DT sound compatible: $(dt_prop "${sound_path}" compatible 2>/dev/null || echo '(missing)')"
    log "DT sound name: $(dt_prop "${sound_path}" name 2>/dev/null || echo '(missing)')"
  else
    log "DT symbol sound not found in /proc/device-tree/__symbols__"
  fi

  if [[ -n "${rp1_i2s0_path}" ]]; then
    log "DT symbol rp1_i2s0 -> /proc/device-tree${rp1_i2s0_path}"
    log "DT rp1_i2s0 status: $(dt_prop "${rp1_i2s0_path}" status 2>/dev/null || echo '(missing)')"
    log "DT rp1_i2s0 compatible: $(dt_prop "${rp1_i2s0_path}" compatible 2>/dev/null || echo '(missing)')"
  else
    log "DT symbol rp1_i2s0 not found in /proc/device-tree/__symbols__"
  fi

  if [[ -n "${rp1_i2s1_path}" ]]; then
    log "DT symbol rp1_i2s1 -> /proc/device-tree${rp1_i2s1_path}"
    log "DT rp1_i2s1 status: $(dt_prop "${rp1_i2s1_path}" status 2>/dev/null || echo '(missing)')"
    log "DT rp1_i2s1 compatible: $(dt_prop "${rp1_i2s1_path}" compatible 2>/dev/null || echo '(missing)')"
  else
    log "DT symbol rp1_i2s1 not found in /proc/device-tree/__symbols__"
  fi

  log "/proc/asound/cards"
  cat /proc/asound/cards || true

  log "arecord -l"
  arecord -l || true

  ensure_debugfs
  if [[ -r /sys/kernel/debug/clk/clk_summary ]]; then
    log "clk_summary grep: clk_i2s | i2s@a4000"
    # Keep output deterministic and bounded.
    sudo sh -c "cat /sys/kernel/debug/clk/clk_summary | egrep -n 'clk_i2s|i2s@a4000' || true"
  else
    log "clk_summary not readable at /sys/kernel/debug/clk/clk_summary"
  fi
}

find_xvf_card_index() {
  awk '
    /^[[:space:]]*[0-9]+[[:space:]]+\[/ { card=$1 }
    /XVF3800-I2S-Capture/ { print card; exit }
  ' /proc/asound/cards | tr -d ' '
}

collect_relevant_dmesg() {
  log "Relevant dmesg (last ~250 lines, filtered)"
  local out
  out="$(dmesg --color=never | tail -n 250 | egrep -i 'rp1|i2s|asoc|snd|simple-audio-card|spdif|xvf|clk' || true)"
  echo "${out}" >&2

  if echo "${out}" | grep -qE 'designware-i2s .*snd_soc_dai_set_fmt.*: -22'; then
    log "HINT: designware-i2s rejected the requested I2S slave fmt (-22)."
    log "This usually means the selected RP1 I2S instance cannot be configured as BCLK/LRCLK slave with this driver/hardware."
    log "Next steps (if rp1_i2s1 also fails with -22):"
    log "  - Check kernel config/driver support for I2S slave mode on RP1 designware I2S"
    log "  - Try rp1_i2s2 (if pins can be remapped and available)"
    log "  - Consider a kernel patch enabling slave mode or an alternate I2S driver"
  fi
}

run_arecord_test() {
  need_cmd arecord

  local card_idx
  card_idx="$(find_xvf_card_index || true)"
  if [[ -z "${card_idx}" ]]; then
    echo "Could not find ALSA card index for 'XVF3800-I2S-Capture' in /proc/asound/cards" >&2
    collect_relevant_dmesg
    print_relevant_lsmod

    # If requested, try rp1_i2s1 as a one-shot workaround when rp1_i2s0 rejects slave fmt.
    if [[ "${XVF_TRY_I2S1}" == "1" ]]; then
      if dmesg --color=never | tail -n 400 | grep -qE 'designware-i2s .*: ASoC: error at snd_soc_dai_set_fmt .*: -22'; then
        log "Detected snd_soc_dai_set_fmt -22; trying troubleshooting overlay using rp1_i2s1"
        load_alt_overlay_i2s1
        print_diagnostics
        card_idx="$(find_xvf_card_index || true)"
        if [[ -n "${card_idx}" ]]; then
          log "Card appeared after rp1_i2s1 overlay; continuing stream-start test"
        else
          echo "Still no ALSA card after rp1_i2s1 attempt" >&2
          collect_relevant_dmesg
          return 2
        fi
      fi
    fi
    return 2
  fi

  log "Found 'XVF3800-I2S-Capture' at card ${card_idx}"

  # Always attempt a WAV capture too (even if silent / unwired DOUT).
  : >"${WAV_PATH}"

  log "WAV capture attempt: ${WAV_PATH} (2s, S32_LE, 48kHz, 2ch)"
  set +e
  arecord -D "hw:${card_idx},0" -c 2 -r 48000 -f S32_LE -d 2 -vv "${WAV_PATH}" >/tmp/xvf3800_arecord_wav.out 2>&1
  local wav_rc=$?
  set -e

  if [[ -e "${WAV_PATH}" ]]; then
    log "WAV file size: $(stat -c %s "${WAV_PATH}") bytes"
  else
    log "WAV file not created"
  fi

  log "Deterministic stream-start test: /dev/null (2s)"
  set +e
  arecord -D "hw:${card_idx},0" -c 2 -r 48000 -f S32_LE -d 2 -vv /dev/null >/tmp/xvf3800_arecord_null.out 2>&1
  local null_rc=$?
  set -e

  local combined_out
  combined_out="$(cat /tmp/xvf3800_arecord_null.out /tmp/xvf3800_arecord_wav.out 2>/dev/null || true)"

  if [[ ${null_rc} -ne 0 ]] && echo "${combined_out}" | grep -qiE 'Input/output error|pcm_read'; then
    log "arecord failed immediately with I/O error (rc=${null_rc}); collecting dmesg and exiting nonzero"
    collect_relevant_dmesg
    echo "\narecord output (tail):" >&2
    echo "${combined_out}" | tail -n 80 >&2
    return 10
  fi

  # If /dev/null test succeeded, treat as success regardless of audio content.
  if [[ ${null_rc} -eq 0 ]]; then
    log "SUCCESS: stream started and ran for 2s"
    return 0
  fi

  # Nonzero but not the classic immediate EIO.
  log "WARNING: arecord returned rc=${null_rc} but did not match immediate EIO signature"
  echo "\narecord output (tail):" >&2
  echo "${combined_out}" | tail -n 80 >&2
  return ${null_rc}
}

print_persistence_plan() {
  local name
  name="$(active_overlay_name)"
  cat <<TXT

=== Persistence plan (boot-time) ===

1) Add this line to /boot/firmware/config.txt:
   dtoverlay=${name}

2) Keep conflicting/alternative audio overlays disabled while debugging.
   In particular, leave genericstereoaudiocodec commented out unless you intentionally want it.

3) Reboot, then re-run this script to confirm:
   - dtoverlay -l shows xvf3800-i2s-capture
   - /proc/asound/cards contains XVF3800-I2S-Capture
   - arecord can start without immediate I/O error

=== Hardware gate checklist (when DOUT is wired) ===

- Confirm XVF DOUT (XVF -> Pi) is connected to Pi GPIO20 (pin 38).
- Confirm BCLK (GPIO18) and LRCLK/FS (GPIO19) are toggling (logic analyzer optional).
- Re-run capture to file and validate:
  - /tmp/xvf3800.wav size > 44 bytes (WAV header + audio)
  - Optionally inspect samples are non-zero (e.g., hexdump/sox).
TXT
}

main() {
  log "XVF3800 I2S capture bring-up (boot-time overlay + validation)"
  log "This script compiles an overlay (mode=${XVF_OVERLAY_MODE}), installs it, updates config.txt,"
  log "and after reboot validates ALSA capture with arecord."

  for c in sudo; do need_cmd "$c"; done

  write_dts_exact
  write_dts_clean_i2s1
  compile_overlay
  ensure_config_overlay_line

  if ! overlay_loaded; then
    print_reboot_instructions
    return 0
  fi

  print_diagnostics

  if run_arecord_test; then
    print_persistence_plan
  else
    log "Bring-up test failed"
    print_persistence_plan
    exit 1
  fi
}

main "$@"
