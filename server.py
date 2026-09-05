"""JetKVM MCP server — drive any PC through a JetKVM with an AI agent.

Two planes, both over one WebRTC connection to the device:

  Screen control (vision loop):  screenshot, click, double_click, move_mouse,
                                 type_text, press_key, scroll
  Device control (deterministic): mount_media_url, mount_media_storage,
                                 unmount_media, list_storage, delete_storage_file,
                                 storage_space, power, power_state, dc_power,
                                 wake_host, wol, usb_emulation, video_state,
                                 reboot_device

Config via env:
  JETKVM_URL       e.g. http://192.168.1.50   (the device's address)
  JETKVM_PASSWORD  local password (omit if device is in noPassword mode)
  JETKVM_VERIFY_TLS  "true" to verify the cert (default false; device uses self-signed)
  JETKVM_KEYBOARD_LAYOUT  layout active on the *target* OS: us (default), uk, de, fr.
                          Wrong layout = wrong characters, silently — see keyboard_layout.
"""

from __future__ import annotations

import os
import asyncio
import logging

from mcp.server.fastmcp import FastMCP, Image

from jetkvm import JetKVMClient, keymap

logging.basicConfig(level=logging.INFO)

mcp = FastMCP("jetkvm")

_env_layout = os.environ.get("JETKVM_KEYBOARD_LAYOUT", "").strip()
if _env_layout:
    keymap.set_layout(_env_layout)  # fail loudly at startup, not mid-command
    logging.info("keyboard layout: %s", keymap.current_layout())

_client: JetKVMClient | None = None
_lock = asyncio.Lock()


async def kvm() -> JetKVMClient:
    """Lazily establish (and reuse) one connection to the device.

    If the connection died (device reboot, network blip), tear it down and build a
    fresh one — tool calls transparently recover instead of failing forever."""
    global _client
    async with _lock:
        if _client is not None and not await _client.ping():
            logging.warning("JetKVM connection is dead; reconnecting")
            await _client.close()
            _client = None
        if _client is None:
            url = os.environ.get("JETKVM_URL")
            if not url:
                raise RuntimeError("set JETKVM_URL (e.g. http://192.168.1.50)")
            c = JetKVMClient(
                base_url=url,
                password=os.environ.get("JETKVM_PASSWORD", ""),
                verify_tls=os.environ.get("JETKVM_VERIFY_TLS", "").lower() == "true",
            )
            try:
                await c.connect()
            except Exception:
                await c.close()  # don't leak a half-open connection; next call retries
                raise
            _client = c
        return _client


# ───────────────────────── screen control (the eyes + hands) ────────────────

@mcp.tool()
async def screenshot() -> Image:
    """Capture the target machine's current screen as a JPEG, cropped to the
    visible desktop (letterbox bars from underscanning hosts are removed).
    Coordinates for click/move are pixel coordinates on the image this returns."""
    c = await kvm()
    data, w, h = await c.snapshot()
    return Image(data=data, format="jpeg")


@mcp.tool()
async def click(x: int, y: int, button: str = "left") -> str:
    """Click at pixel (x, y) on the latest screenshot. button: left|right|middle."""
    await (await kvm()).click(x, y, button)
    return f"clicked {button} at ({x},{y})"


@mcp.tool()
async def double_click(x: int, y: int) -> str:
    """Double-click at pixel (x, y)."""
    await (await kvm()).double_click(x, y)
    return f"double-clicked at ({x},{y})"


@mcp.tool()
async def move_mouse(x: int, y: int) -> str:
    """Move the pointer to pixel (x, y) without clicking."""
    await (await kvm()).move(x, y)
    return f"moved to ({x},{y})"


@mcp.tool()
async def type_text(text: str, layout: str = "") -> str:
    """Type a string on the target machine's emulated USB keyboard.

    A USB keyboard sends key positions, not characters — what appears depends on
    the keyboard layout the *target* OS has active. Set it once with
    keyboard_layout (or JETKVM_KEYBOARD_LAYOUT); `layout` overrides it for this
    call. Characters the layout cannot produce are reported back, not dropped
    silently."""
    skipped = await (await kvm()).type_text(text, layout or None)
    active = layout or keymap.current_layout()
    if skipped:
        return (
            f"typed {len(text) - len(skipped)} of {len(text)} chars ({active} layout); "
            f"unreachable on this layout, skipped: {''.join(skipped)!r}"
        )
    return f"typed {len(text)} chars ({active} layout)"


@mcp.tool()
async def press_key(combo: str, layout: str = "") -> str:
    """Press a key or chord, e.g. 'enter', 'ctrl+c', 'ctrl+alt+delete', 'win+r', 'f2'.
    'altgr' is available for third-level characters on non-US layouts. Character
    keys in a chord are resolved through the active keyboard layout, so 'ctrl+z'
    presses the key that really is Z on the target."""
    await (await kvm()).press_key(combo, layout or None)
    return f"pressed {combo}"


@mcp.tool()
async def keyboard_layout(layout: str = "") -> dict:
    """Get or set the keyboard layout the target OS is using.

    Call with no argument to read the current layout and the available ones; pass
    a name ('de', 'German', 'en-GB', …) to switch. This is the target's layout,
    not yours — get it wrong and type_text produces plausible-looking wrong
    characters with no error (on German, 'z' arrives as 'y')."""
    if layout:
        keymap.set_layout(layout)
    return {
        "active": keymap.current_layout(),
        "available": keymap.available_layouts(),
    }


@mcp.tool()
async def scroll(amount: int) -> str:
    """Scroll the wheel. Positive = up, negative = down (number of detents)."""
    await (await kvm()).scroll(amount)
    return f"scrolled {amount}"


# ───────────────────────── device control (deterministic) ───────────────────

@mcp.tool()
async def mount_media_url(url: str, mode: str = "CDROM") -> str:
    """Mount a bootable image as USB media, streamed from a URL (host big ISOs on
    your own server). mode: CDROM (read-only optical) or Disk (writable mass storage).
    The target can then boot from it. Use for OS install / recovery."""
    if mode not in ("CDROM", "Disk"):
        return "mode must be 'CDROM' or 'Disk'"
    await (await kvm()).rpc("mountWithHTTP", url=url, mode=mode)
    return f"mounted {url} as {mode}"


@mcp.tool()
async def mount_media_storage(filename: str, mode: str = "CDROM") -> str:
    """Mount an image already uploaded to the device's local storage."""
    if mode not in ("CDROM", "Disk"):
        return "mode must be 'CDROM' or 'Disk'"
    await (await kvm()).rpc("mountWithStorage", filename=filename, mode=mode)
    return f"mounted {filename} as {mode}"


@mcp.tool()
async def upload_media(local_path: str) -> dict:
    """Upload an image file from this machine to the device's local storage (init via
    startStorageFileUpload, stream to /storage/upload). The device partition is small,
    so this is for boot/recovery images, not full OS ISOs — for those use mount_media_url."""
    import os.path
    with open(local_path, "rb") as f:
        data = f.read()
    name = os.path.basename(local_path)
    return await (await kvm()).upload_to_storage(name, data)


@mcp.tool()
async def upload_and_mount(local_path: str, mode: str = "CDROM") -> str:
    """Upload a local image to device storage and immediately mount it as USB media."""
    if mode not in ("CDROM", "Disk"):
        return "mode must be 'CDROM' or 'Disk'"
    import os.path
    with open(local_path, "rb") as f:
        data = f.read()
    name = os.path.basename(local_path)
    c = await kvm()
    await c.upload_to_storage(name, data)
    await c.rpc("mountWithStorage", filename=name, mode=mode)
    return f"uploaded and mounted {name} as {mode}"


@mcp.tool()
async def unmount_media() -> str:
    """Eject / unmount the currently mounted virtual media."""
    await (await kvm()).rpc("unmountImage")
    return "unmounted"


@mcp.tool()
async def list_storage() -> dict:
    """List image files in the device's local storage."""
    return await (await kvm()).rpc("listStorageFiles")


@mcp.tool()
async def delete_storage_file(filename: str) -> str:
    """Delete an image from the device's local storage."""
    await (await kvm()).rpc("deleteStorageFile", filename=filename)
    return f"deleted {filename}"


@mcp.tool()
async def storage_space() -> dict:
    """Report free/total bytes on the device's storage partition (it's small —
    prefer mount_media_url for full-size ISOs)."""
    return await (await kvm()).rpc("getStorageSpace")


@mcp.tool()
async def virtual_media_state() -> dict:
    """Report what (if anything) is currently mounted."""
    return await (await kvm()).rpc("getVirtualMediaState")


@mcp.tool()
async def power(action: str) -> str:
    """ATX front-panel power control (requires the ATX/header extension wired up).
    action: 'power-short' (tap power), 'power-long' (5s hard off), 'reset'."""
    if action not in ("power-short", "power-long", "reset"):
        return "action must be power-short | power-long | reset"
    await (await kvm()).rpc("setATXPowerAction", action=action)
    return f"ATX {action} sent"


@mcp.tool()
async def power_state() -> dict:
    """Read ATX power/HDD LED state (power: on/off, hdd: activity)."""
    return await (await kvm()).rpc("getATXState")


@mcp.tool()
async def dc_power(enabled: bool) -> str:
    """Toggle DC power output (DC-power extension)."""
    await (await kvm()).rpc("setDCPowerState", enabled=enabled)
    return f"DC power {'on' if enabled else 'off'}"


@mcp.tool()
async def wake_host() -> str:
    """Wake the directly-attached host (USB wake)."""
    await (await kvm()).rpc("wakeHost")
    return "wake signal sent"


@mcp.tool()
async def wol(mac_address: str, broadcast_ip: str | None = None) -> str:
    """Send a Wake-on-LAN magic packet to a MAC on the device's network."""
    params = {"macAddress": mac_address}
    if broadcast_ip:
        params["broadcastIP"] = broadcast_ip
    await (await kvm()).rpc("sendWOLMagicPacket", **params)
    return f"WOL sent to {mac_address}"


@mcp.tool()
async def usb_emulation(enabled: bool) -> str:
    """Enable/disable the emulated USB gadget (keyboard/mouse/storage) to the target."""
    await (await kvm()).rpc("setUsbEmulationState", enabled=enabled)
    return f"USB emulation {'enabled' if enabled else 'disabled'}"


@mcp.tool()
async def video_state() -> dict:
    """Report the captured HDMI signal state and resolution."""
    return await (await kvm()).rpc("getVideoState")


@mcp.tool()
async def reboot_device(force: bool = False) -> str:
    """Reboot the JetKVM device itself (not the target machine)."""
    await (await kvm()).rpc("reboot", force=force)
    return "JetKVM reboot requested"


if __name__ == "__main__":
    mcp.run()
