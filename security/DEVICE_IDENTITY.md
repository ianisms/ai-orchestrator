# Device identity for the speaker: today, this change, target

How the orchestrator decides that the thing on the other end of the gRPC stream
is the kitchen speaker and not some other host on the same network. This page
is the written version of the "attestation today vs target" picture; the
diagrams in `DIAGRAMS.md` predate this change and will be reconciled after the
open diagrams PR merges.

Status key, same as elsewhere in `security/`: built = runs with tests; partial =
runs but with a documented gap; not built = design only.

## Today (before this change)

The speaker is trusted by where it is and who is talking, not by what it is.

- Network origin: the orchestrator reads the gRPC peer address (never a header)
  and checks it against `VOICE_GATE_TRUSTED_SPEAKER_NETS`
  (`server/modules/orchestrator/core/voice_gate.py`, `_host_trusted`). Built.
- Voice: the speaker is identified from the turn's own audio by a tool hidden
  from the model (`audio_tap.py`, `identify_speaker`). Built, threshold not yet
  calibrated, gate in monitor mode.
- No attestation of any kind. Any host that reaches a trusted address, or a
  compromised speaker, passes the origin check (threat 18).

One line: location plus voice, not device identity.

## This change: a hardware-held device key (partial)

A YubiKey 5 plugged into the CM5 holds a P-256 key in PIV slot 9a. The key is
generated on the YubiKey and cannot be exported. At boot the speaker proves it
holds that key, and the orchestrator issues a short-lived device credential.

```
speaker (YubiKey PIV 9a)                 orchestrator (status server :9111)
  POST /device/challenge {device_id} --> issue nonce, 30 s, bound to device_id
  <-- {nonce, exp}
  sign "ai-orchestrator device challenge v1\n" + nonce  (ECDSA P-256 / SHA-256)
  POST /device/attest {device_id, nonce, signature} -->
                                         nonce known, unexpired, unused, same device_id
                                         device enrolled and not revoked
                                         signature verifies against the enrolled key
  <-- {credential, exp}                  HS256 token, 10 min, cnf.jkt = key thumbprint
```

Files:

| Piece | Where | Status |
|---|---|---|
| Signer interface, `YubiKeyPivSigner`, `FakeSigner` | `speaker/modules/speaker/device_identity.py` | built; YubiKey path reviewed against the yubikit API, not exercised in CI |
| Boot-time attest client, credential cache | same file, `DeviceAttestClient`; called from `orchestrator.py` `_attest_device` | built, off by default (`device_identity_enable`) |
| Enrollment: generate key on the YubiKey, export public key, write request | `speaker/scripts/enroll_device_key.py` | built |
| Registry (JSON, `device_id`, `room`, `household`, public key PEM, thumbprint, `enrolled_at`, `revoked_at`) | `server/modules/orchestrator/core/device_identity.py`, file at `DEVICE_REGISTRY_PATH` | built |
| Admin: add, list, revoke, remove | `server/scripts/enroll_device.py` | built |
| Challenge, verify, credential mint and verify | `DeviceIdentityService`, routes on the status server | built |
| Credential read from gRPC metadata at connect | `server/modules/aiserver/server.py`, `device_from_metadata` | partial: verified and logged, not enforced |
| Speaker attaches the credential to the Converse stream | `DeviceAttestClient.grpc_metadata()` | not wired: `grpc_client.py` does not send it yet |
| Voice gate reads the device credential | | not built |

Key policy: ECCP256, touch policy never (nobody is there to touch it), PIN
policy once with the PIN in a mode 600 file the client reads at boot. The
management key is used only at enrollment and is not on the device at run
time. `--pin-policy never` is allowed for a sealed enclosure; then possession
of the key is the whole secret.

Fail closed. No key, unknown device, revoked device, unknown nonce, expired
nonce, replayed nonce, nonce issued to another device, and bad signature all
deny with a logged reason (`device_identity.audit`). A failed attempt consumes
the nonce, so there is no second guess. The credential dies on expiry, on a
registry revocation, and on an orchestrator restart (per-process HMAC key).

Tests: `server/test/test_device_identity.py` (both sides through the HTTP glue
in process, with `FakeSigner`) and `speaker/test/test_device_identity.py`. CI
runs both without hardware.

What this proves: the device holds the enrolled key. What it does not prove:
what software is running on it. There is no measured boot, no PCR quote, no
attestation key chained to a manufacturer. A CM5 whose root filesystem has been
replaced still attests fine if the YubiKey is plugged in. This is device
identity, not attestation.

## Theft of a pluggable key

A YubiKey can be pulled from the speaker and plugged into a laptop. With the
PIN policy on, the attacker also needs the PIN file from the Pi; without it,
possession is enough. Either way, treat the credential as "this key is
present", not "this is the trustworthy kitchen speaker". Mitigations, in the
order they apply:

1. Key-bound tokens are limited to low-risk scopes. The device credential
   carries `cnf.jkt`; anything minted against it (not built yet) stays at the
   read tier of the voice gate.
2. High-risk actions still need voice plus challenge. A credential never
   replaces the speaker match, the liveness check (not built) or the phone
   approval (not built) for writes and high-risk calls.
3. Enrollment is revocable in one step (`enroll_device.py revoke`). Revocation
   stops every future challenge and every credential check for that key the
   moment the registry file is written; the credential's own lifetime is at
   most 15 minutes anyway.
4. The nonce is bound to the `device_id` that asked for it and the registry is
   never enumerated by the challenge route, so a stolen key cannot be probed
   for which device it belongs to without already knowing.

## Target: measured boot and a real attestation chain

A header-mounted TPM 2.0 module on the CM5 (GPIO/SPI) would also work as a
stopgap: it gives an attestation key and PCR measurements without a board
change. I am not taking that path.

Next step: revise the carrier board (`speaker/hardware/pcb/theboard/`, rev 3.7
today) to add a secure element (ATECC608 over I2C) or an SPI TPM footprint.

- ATECC608 over I2C: a non-extractable P-256 key with a manufacturer
  certificate chain, the same challenge flow as above with the YubiKey removed,
  and no pluggable part to steal. No measured boot.
- SPI TPM: everything the ATECC608 gives, plus PCR measurements, so the
  orchestrator can require a quote over the boot chain before issuing the
  credential. That is the full attestation path the target diagram describes.

Either way the orchestrator side here does not change shape: a signer behind
the `Signer` interface, an enrollment registry keyed by thumbprint, a challenge
with a short nonce, a short-lived key-bound credential. The TPM variant adds a
quote to the attest request and a PCR policy to the verifier.

After the key is in silicon the remaining pieces land in order: the speaker
presents the credential on the stream (or the stream moves to mTLS with the
device key), the voice gate takes the device identity as a signal in place of
the address check, and tokens minted for the device are bound to its key.
