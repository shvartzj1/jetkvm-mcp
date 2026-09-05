# Architecture

How jetkvm-mcp drives a JetKVM, end to end. Everything here is verified against the
[jetkvm/kvm](https://github.com/jetkvm/kvm) firmware source and tested live against a
JetKVM v2 on app 0.5.8.

## Big picture

```
Claude / MCP client
      │  stdio (MCP protocol)
      ▼
server.py (FastMCP, 24 tools)
      │  one shared JetKVMClient, connected lazily on first tool call
      ▼
jetkvm/client.py
      │
      ├── HTTPS/HTTP ──▶ /auth/login-local, /webrtc/session, /storage/upload
      │
      └── WebRTC peer connection
            ├── video track (H.264, recvonly) ─▶ PyAV decode ─▶ latest frame ─▶ JPEG on demand
            └── data channel "rpc" ─▶ JSON-RPC 2.0, every method the web UI uses
                  │
                  ▼
              JetKVM device ──▶ HDMI capture in, USB HID gadget out ──▶ target machine
```

The server process runs on your workstation, not on the device. The JetKVM runs stock
firmware and treats this client exactly like its own web UI.

## Connection lifecycle

1. **Auth** — if a password is configured: `POST /auth/login-local` with
   `{"password": …}`; the device sets an `authToken` cookie that the HTTP client carries
   into the signaling call. In `noPassword` mode this step is skipped.
2. **Signaling** — the client creates an `RTCPeerConnection` with one `recvonly` video
   transceiver and a data channel labelled **`rpc`** (reliable + ordered defaults), waits for
   ICE gathering, then sends the offer as
   `POST /webrtc/session` with body `{"sd": base64(json({"type": …, "sdp": …}))}`.
   The response carries the answer in the same base64-JSON envelope.
3. **Session** — once the data channel opens, everything (input, media, power, state) is
   JSON-RPC 2.0 over that channel. Requests carry incrementing ids; responses resolve pending
   futures; id-less messages are server-initiated events (e.g. `otherSessionConnected`) and
   are ignored.

### Reconnect / device restarts

The server keeps one cached connection, and every tool call health-checks it first:
`kvm()` sends an RPC `ping` with a 3 s timeout (any response — even a JSON-RPC error —
proves the channel round-trips; `JetKVMDisconnected` or silence means dead). On failure it
closes the old client and builds a fresh connection, so a JetKVM reboot or network blip
heals transparently on the next tool call — no server restart needed. When the peer
connection dies, all in-flight RPCs are failed immediately with `JetKVMDisconnected`
instead of waiting out their 30 s timeouts.

Measured against a real device reboot: the first tool call issued mid-reboot detects the
dead session in ~3 s, reconnects as soon as the device is back, and returns a fresh frame
— ~17 s end to end, dominated by the device's own boot time.

## Video: the keyframe/PLI story (the trap)

The device streams H.264 continuously, **but only emits an IDR (keyframe) when the viewer
requests one via RTCP PLI** (Picture Loss Indication). Browsers send PLI automatically as part
of their WebRTC stack, which is why the web UI "just works". aiortc does **not** — it will sit
on an undecodable stream forever, logging
`H264Decoder() failed to decode … avcodec_send_packet: Invalid data` for every packet.

The fix (`client.py`):

- `_request_keyframe()` sends RTCP PLI to every known video SSRC — parsed from the answer
  SDP's `a=ssrc:` lines, plus whatever the receiver has seen live.
- On connect, `_kick_until_first_frame()` fires PLI every 0.5 s until the first frame decodes
  (typically ~3 s total to first frame).
- `snapshot()` re-kicks PLI whenever the newest decoded frame is older than 2 s, which
  recovers from mid-session stalls (resolution changes, stream restarts). If no fresh frame
  arrives in 10 s it falls back to the last good frame rather than failing.

After the first keyframe, the decode loop simply keeps the most recent frame in memory
(`_drain_video`), so `snapshot()` is a JPEG encode of an already-decoded frame —
single-digit milliseconds at 1280×1024.

### Codec negotiation

Firmware `resolveCodec()` picks H.265 **if the offer SDP contains the string "H265"** and the
device preference (`getVideoCodecPreference`, default `auto`) allows it. aiortc (≤ 1.14) has
no H.265 codec, so its offer never mentions it and the device always answers H.264. If you
swap in a client stack that advertises H.265, be ready to decode it.

`getVideoState` sometimes reports `streaming: 0` while frames flow at 60 fps — cosmetic;
don't gate on it.

## Input: the hands

All input is USB HID reports over RPC — the target sees a real USB keyboard/mouse.

- **Mouse** — `absMouseReport(x, y, buttons)` with x/y in the HID absolute range 0–32767.
  The client converts pixel coordinates (on the latest snapshot) to that range using the live
  frame dimensions, so clicks land exactly and there is no relative-mouse drift. Buttons:
  left `0x01`, right `0x02`, middle `0x04`. A click is press → 40 ms → release.
- **Wheel** — `wheelReport(wheelY=±1)` per detent, stepped with small sleeps so the target
  registers each notch.
- **Keyboard** — `keyboardReport(modifier, keys=[usage])` press followed by an empty report
  (release). A USB keyboard transmits *key positions*, never characters: what appears is
  whatever the target OS's active layout puts on that position. `keymap.py` therefore models
  layouts explicitly. Physical keys are named by the character US puts on them, and each
  layout (`us`, `uk`, `de`, `fr`) declares what its keys emit at each of the four levels —
  plain, Shift, AltGr (`0x40`, RightAlt), Shift+AltGr. A layout table is the flattening of
  that: character → `(modifier, usage)`, walked cheapest-level-first so a character on two
  keys is reached with the fewest modifiers.

  Three details the naive US-only mapping gets wrong:

  - **AltGr.** `\ | { } [ ] @ €` live on the third level of most European layouts. Without
    RightAlt in the modifier byte they are simply unreachable — on German, `|` is
    AltGr + the 102nd key and `\` is AltGr + the `ß` key.
  - **The 102nd key** (usage `0x64`), the extra key ISO keyboards have between LeftShift and
    Z. ANSI keyboards don't have it, so a US-derived table never emits it — but German puts
    `< > |` there and UK puts `\ |` there. The emulated gadget can send it regardless of what
    the operator's own keyboard looks like.
  - **Dead keys.** German `^ ´ \`` and French `^ ¨ ~ \`` emit nothing on their own; they arm an
    accent for the next keystroke. `char_to_strokes` returns those as two strokes — the key,
    then space — which is how the standalone character is produced.

  Chords resolve their character key through the same table, so `ctrl+z` on a German target
  presses the key that really is Z there (usage `0x1C`), not the US one. Characters the active
  layout cannot produce are returned to the caller by `type_text` instead of being dropped
  with only a log line — a silently truncated command is worse than a reported one.

  `keymap_test.py` checks the tables offline (no device): the German expectations are the
  ones measured on real hardware in issue #2.

## Virtual media

- `mountWithHTTP(url, mode)` — the **device** fetches the image over HTTP(S) with range
  requests and presents it to the target as USB mass storage. `mode` is `CDROM` (read-only
  optical) or `Disk` (writable). This is the path for full-size ISOs.
- `mountWithStorage(filename, mode)` — mount an image previously uploaded to the device's
  small local partition.
- Uploads are two-step: RPC `startStorageFileUpload(filename, size)` returns an `uploadId`,
  then the bytes stream to `POST /storage/upload?uploadId=…`.
- `getVirtualMediaState` returns `null` when nothing is mounted, else
  `{source, mode, url|filename, size}`. `unmountImage` ejects.

## Power & platform control

- `setATXPowerAction("power-short" | "power-long" | "reset")` — front-panel signals; requires
  the ATX extension board. `getATXState` reads the power/HDD LED lines.
- `setDCPowerState(enabled)` / `getDCPowerState` — the DC power extension (voltage/current
  readings included).
- `wakeHost` (USB wake), `sendWOLMagicPacket(macAddress)` (Wake-on-LAN from the device's NIC).
- `setUsbEmulationState(enabled)` — detach/attach the whole USB gadget.
- `reboot` — reboots the **JetKVM itself**, not the target.

## Security posture

- Signaling travels over the device's HTTP server — plain HTTP on stock LAN config, HTTPS if
  you enable TLS on the device. The WebRTC session itself (video + rpc channel) is always
  DTLS/SRTP-encrypted peer-to-peer.
- The client honors `JETKVM_VERIFY_TLS`; leave it `false` only for self-signed setups you
  control.
- Treat the rpc channel as root on the target: anything that can reach the device and knows
  the password can type into the machine's console.
