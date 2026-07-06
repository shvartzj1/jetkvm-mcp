# Tool reference

All 24 MCP tools exposed by `server.py`. The first tool call establishes the device
connection (expect ~3 s extra for the first `screenshot`; see
[architecture.md](architecture.md#video-the-keyframepli-story-the-trap)).

## Screen control — the vision loop

| Tool | Parameters | Behavior |
|------|------------|----------|
| `screenshot` | — | Current screen as JPEG. All click/move coordinates refer to pixels on the **most recent** screenshot. |
| `click` | `x`, `y`, `button="left"` | Press+release at pixel (x, y). `button`: `left` \| `right` \| `middle`. |
| `double_click` | `x`, `y` | Two clicks, 80 ms apart. |
| `move_mouse` | `x`, `y` | Move pointer without clicking (hover). |
| `type_text` | `text` | Type a string on the emulated USB keyboard. US-layout mapping — see gotchas in the README. Unmapped characters are skipped with a warning. |
| `press_key` | `combo` | A key or chord: `enter`, `esc`, `tab`, `f2`, arrow keys, `ctrl+c`, `ctrl+alt+delete`, `win+r`, `cmd+shift+4`, … |
| `scroll` | `amount` | Wheel detents; positive = up, negative = down. |

## Virtual media

| Tool | Parameters | Behavior |
|------|------------|----------|
| `mount_media_url` | `url`, `mode="CDROM"` | Device streams the image from a URL (HTTP range requests) and presents it to the target as USB media. `mode`: `CDROM` (read-only) \| `Disk` (writable). The path for full-size ISOs. |
| `mount_media_storage` | `filename`, `mode="CDROM"` | Mount an image already on the device's local storage partition. |
| `upload_media` | `local_path` | Upload a file from the workstation to device storage. The partition is small — recovery images, not OS ISOs. |
| `upload_and_mount` | `local_path`, `mode="CDROM"` | Upload, then mount, in one step. |
| `unmount_media` | — | Eject whatever is mounted. |
| `virtual_media_state` | — | `null` if nothing mounted, else `{source, mode, url|filename, size}`. |
| `list_storage` | — | Image files on the device partition. |
| `delete_storage_file` | `filename` | Delete one of them. |
| `storage_space` | — | Free/total bytes on the device partition. |

## Power & platform

| Tool | Parameters | Behavior |
|------|------------|----------|
| `power` | `action` | ATX front-panel control: `power-short` (tap), `power-long` (5 s hard off), `reset`. **Requires the ATX extension board.** |
| `power_state` | — | ATX power/HDD LED state: `{power: bool, hdd: bool}`. |
| `dc_power` | `enabled` | Toggle the DC power extension output. |
| `wake_host` | — | USB wake signal to the attached host. |
| `wol` | `mac_address`, `broadcast_ip?` | Wake-on-LAN magic packet sent from the JetKVM's network. |
| `usb_emulation` | `enabled` | Attach/detach the entire emulated USB gadget (keyboard+mouse+storage). |
| `video_state` | — | HDMI capture state: `{ready, width, height, fps}` (the `streaming` field is unreliable — ignore). |
| `reboot_device` | `force=false` | Reboot the **JetKVM itself**, not the target machine. |

## Destructive-tool checklist

Consider gating these behind per-call confirmation in your MCP client:
`power`, `dc_power`, `reboot_device`, `mount_media_url`, `mount_media_storage`,
`upload_and_mount`, `unmount_media`, `delete_storage_file`, `usb_emulation`, and `press_key`
with reboot chords.
