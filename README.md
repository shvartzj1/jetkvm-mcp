# jetkvm-mcp

**Give an AI eyes and hands on a physical computer.**

jetkvm-mcp is an [MCP](https://modelcontextprotocol.io) server that turns a
[JetKVM](https://jetkvm.com) — a small open-source KVM-over-IP device — into a machine that
Claude (or any MCP client) can see and operate directly: watch the screen, type, click, mount
boot media, and control power. Because the JetKVM sits on the HDMI and USB ports, the AI drives
the computer **below the OS** — BIOS screens, bootloaders, installers, headless boxes with no
network, machines that are wedged. No agent, no SSH, nothing installed on the target.

```
you:    "Screenshot the machine. It's stuck — what's wrong?"
claude: → screenshot → "It's sitting at a GRUB rescue prompt. The root partition
         UUID changed. Want me to boot it manually?" → type_text → enter → fixed
```

Works against **stock JetKVM firmware** — no modifications to the device.

## The two planes

| Plane | Tools | Nature |
|-------|-------|--------|
| **Screen control** (eyes + hands) | `screenshot`, `click`, `double_click`, `move_mouse`, `type_text`, `press_key`, `scroll` | vision loop — the AI looks, then acts |
| **Device control** | `mount_media_url`, `mount_media_storage`, `upload_media`, `upload_and_mount`, `unmount_media`, `list_storage`, `delete_storage_file`, `storage_space`, `virtual_media_state`, `power`, `power_state`, `dc_power`, `wake_host`, `wol`, `usb_emulation`, `video_state`, `reboot_device` | deterministic RPC |

Full parameter reference: [docs/tools.md](docs/tools.md).

## How it works

One WebRTC peer connection to the device drives everything:

```
Claude ──MCP/stdio──▶ server.py (this repo, runs on your workstation)
                        │
                        └──WebRTC over LAN──▶ JetKVM ──HDMI-in / USB-HID-out──▶ target machine
                             ├─ H.264 video track ─▶ decoded locally (PyAV) ─▶ JPEG screenshots
                             └─ "rpc" data channel ─▶ keyboard / mouse / media / power JSON-RPC
```

- The device already streams its HDMI capture as an H.264 video track — the client decodes it
  locally and hands the AI JPEG snapshots on demand. (JetKVM has no snapshot endpoint; it
  doesn't need one.)
- A reliable `rpc` data channel carries every JSON-RPC method the device's own web UI uses:
  `keyboardReport`, `absMouseReport` (absolute 0–32767, drift-free), `mountWithHTTP`,
  `setATXPowerAction`, and friends.
- The server connects lazily on the first tool call and keeps the one connection alive.

Deep dive — handshake, codec negotiation, the keyframe/PLI story, coordinate mapping:
[docs/architecture.md](docs/architecture.md).

## Requirements

- A JetKVM attached to the target machine, reachable on your network
- Python 3.11+ on the machine that runs Claude
- `aiortc`/`av` wheels bundle FFmpeg on macOS/Linux; if a build from source is triggered,
  install FFmpeg dev libraries first (`brew install ffmpeg` / `apt install libavdevice-dev`)

## Quickstart

```bash
git clone https://github.com/shvartzj1/jetkvm-mcp.git
cd jetkvm-mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # set JETKVM_URL (+ JETKVM_PASSWORD if your device has one)
```

Prove the pipeline before wiring it into anything — this connects, holds the stream open, and
saves four screenshots:

```bash
set -a; source .env; set +a
python smoke_test.py
```

Expected output — sustained ~60 fps, snapshots in single-digit milliseconds after the first:

```
connected. video_state: {'ready': True, 'width': 1280, 'height': 1024, 'fps': 60}
  snapshot 0: 1280x1024   179578 bytes  (grab 3129 ms)  frames_seen=1
  snapshot 1: 1280x1024   178280 bytes  (grab   11 ms)  frames_seen=128
  ...
```

## Wire it into Claude

**Claude Code** (one command, available in every session):

```bash
claude mcp add jetkvm --scope user \
  --env JETKVM_URL=http://192.168.1.50 \
  --env JETKVM_VERIFY_TLS=false \
  -- /abs/path/jetkvm-mcp/.venv/bin/python /abs/path/jetkvm-mcp/server.py
```

**Claude Desktop** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "jetkvm": {
      "command": "/abs/path/jetkvm-mcp/.venv/bin/python",
      "args": ["/abs/path/jetkvm-mcp/server.py"],
      "env": {
        "JETKVM_URL": "http://192.168.1.50",
        "JETKVM_PASSWORD": "",
        "JETKVM_VERIFY_TLS": "false"
      }
    }
  }
}
```

Then just talk to it: *"Screenshot the machine, open a terminal, and check disk usage."*
The AI calls `screenshot` → reasons → `click` / `type_text` → repeats.

## Docker

If you'd rather not set up a local Python environment, you can run the server in a container.

**Build the image:**

```bash
git clone https://github.com/bachajon/jetkvm-mcp.git
cd jetkvm-mcp
docker build -t jetkvm-mcp .
```

**Run (smoke-test / one-shot):**

```bash
docker run --rm \
  -e JETKVM_URL=http://192.168.1.50 \
  -e JETKVM_PASSWORD="" \
  -e JETKVM_VERIFY_TLS=false \
  jetkvm-mcp
```

> **Networking note:** The container only needs to reach the JetKVM outbound — the default
> bridge network is fine, including Docker Desktop on macOS/Windows, where the container sits
> behind the VM's NAT. That works because the client speaks the device's websocket signaling
> and picks up its trickled ICE candidates, so it can dial the device directly instead of
> waiting to be dialed. (On firmware too old for websocket signaling the client falls back to
> the legacy `POST /webrtc/session`, whose answer carries no candidates — that path needs the
> container to be reachable *from* the device, so it wants `--network host` on Linux or a
> non-containerized run.) VPN split-tunnelling can still get in the way.

**Claude Desktop** — Docker-based config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "jetkvm": {
      "command": "docker",
      "args": [
        "run", "--rm", "-i",
        "-e", "JETKVM_URL=http://192.168.1.50",
        "-e", "JETKVM_PASSWORD=",
        "-e", "JETKVM_VERIFY_TLS=false",
        "jetkvm-mcp"
      ]
    }
  }
}
```

The `-i` flag keeps stdin open so Claude Desktop can speak MCP over stdio to the container.
Replace `http://192.168.1.50` with your device's address and set `JETKVM_PASSWORD` if your
device has one.

## The killer workflow: hands-free bare-metal provisioning

Device control and screen control compose into something no in-OS agent can do — installing an
operating system on an empty machine:

```
mount_media_url("https://mirror.lan/rocky-9.iso", "CDROM")   # host the ISO yourself
power("reset")                                               # reboot into the installer
# screenshot → click → type_text … the AI walks through the installer by sight
unmount_media()
```

The device's own storage partition is tiny, so `mount_media_url` (the device streams the image
over HTTP with range requests) is the right path for full-size ISOs; `upload_and_mount` is for
small recovery images.

## Gotchas (read this before filing a bug)

- **First screenshot takes ~3 s; the rest are instant.** The device only emits an H.264
  keyframe when asked via RTCP PLI. Browsers request keyframes automatically; aiortc does not —
  so this client sends PLI on connect and whenever frames go stale (`_request_keyframe` in
  `client.py`). Without that, decode fails on every packet forever
  (`avcodec_send_packet: Invalid data`). If you're building your own client: this is the trap.
- **Keyboard layout**: `type_text` maps ASCII → USB HID usage codes assuming the **US layout**
  on the target OS. On other layouts, shifted symbols swap (on a UK target, `"` arrives as
  `@`). Letters, digits, and `/ - . ; =` are layout-stable; prefer them in critical commands.
- **Coordinates**: `click`/`move_mouse` take pixel coordinates on the most recent
  `screenshot`; the client maps them to the HID absolute range using the live frame
  dimensions, so there is no drift.
- **`getVideoState` may report `streaming: 0`** even while frames flow at 60 fps — cosmetic
  quirk, ignore it.
- **TLS**: stock firmware serves plain HTTP on the LAN. The device supports optional TLS
  (Settings → Advanced) — enable it and set `JETKVM_URL=https://…`, plus
  `JETKVM_VERIFY_TLS=true` if the cert is trusted. WebRTC media/control is DTLS/SRTP-encrypted
  peer-to-peer regardless of how the signaling travelled.
- **`power` needs the ATX extension board** wired to the motherboard header; without it the
  tool is a no-op (`power_state` reads `power: false`).

## Safety

This lets a language model drive a real computer with real consequences. Recommendations:

- Point it at a test box or lab machine first, not your production NAS.
- The destructive tools are `power`, `reboot_device`, `dc_power`, `mount_*`,
  `delete_storage_file`, and any `press_key` of a reboot chord — consider requiring per-call
  confirmation for them in your MCP client's permission settings.
- Set a device password (and TLS) if the JetKVM is reachable by anyone but you.

## Development

```
jetkvm/client.py   WebRTC + JSON-RPC client (connect, snapshot, HID input, uploads)
jetkvm/keymap.py   ASCII / key-combo → USB HID usage codes
server.py          FastMCP server exposing the 24 tools
smoke_test.py      live end-to-end check against a real device
docs/              architecture + tool reference
```

Validated end-to-end against a JetKVM v2 on firmware/app **0.5.8** (Jul 2026): sustained
60 fps decode, keyboard input, HTTP CDROM mount/unmount, ATX/DC state reads — including driving
it from a live Claude session. The RPC surface is verified against the
[jetkvm/kvm](https://github.com/jetkvm/kvm) source.

### Ideas / roadmap

- Keyboard layout profiles for `type_text` (US hardcoded today)
- Gate destructive tools behind an env flag
- Native `getSnapshot` RPC upstream in the firmware would remove the H.264 decode dependency
  entirely (see jetkvm/kvm#1459)

## License

[MIT](LICENSE). Not affiliated with JetKVM/Improve Robotics — this is an independent client
of the device's public API.
