"""JetKVM client: one WebRTC peer connection drives both planes.

  * video track  -> decoded frames -> JPEG snapshots   (the "eyes")
  * "rpc" datachannel -> every JSON-RPC method          (the "hands" + device control)

No firmware change is required: snapshots come from the H.264 video track that the
device already streams, decoded locally with PyAV (an aiortc dependency).

Handshake (verified against jetkvm/kvm source):
  POST /auth/login-local   {"password": ...}              -> sets authToken cookie
  POST /webrtc/session     {"sd": base64(json({type,sdp}))} -> {"sd": base64(answer)}
The device adds a video track; the client opens a reliable datachannel labelled "rpc".
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time

import httpx
from aiortc import RTCPeerConnection, RTCSessionDescription

from . import keymap

log = logging.getLogger("jetkvm")

ABS_MAX = 32767  # HID absolute-pointer logical maximum (hid_mouse_absolute.go)
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x01, 0x02, 0x04


class JetKVMError(RuntimeError):
    pass


class JetKVMClient:
    def __init__(self, base_url: str, password: str = "", verify_tls: bool = False):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self._http = httpx.AsyncClient(
            base_url=self.base_url, verify=verify_tls, timeout=30.0, follow_redirects=True
        )
        self._pc: RTCPeerConnection | None = None
        self._rpc = None
        self._rpc_open = asyncio.Event()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._latest_frame = None  # av.VideoFrame
        self._frame_count = 0
        self._frame_event = asyncio.Event()
        self._last_frame_at = 0.0  # monotonic time of the last decoded frame
        self._last_dims = (0, 0)  # (w, h) of the most recent snapshot we handed out
        self._video_receiver = None
        self._video_ssrcs: set[int] = set()

    # ---------- lifecycle ----------------------------------------------------

    async def connect(self) -> None:
        if self.password:
            r = await self._http.post("/auth/login-local", json={"password": self.password})
            if r.status_code not in (200, 204):
                raise JetKVMError(f"login failed: {r.status_code} {r.text}")

        pc = RTCPeerConnection()
        self._pc = pc
        pc.addTransceiver("video", direction="recvonly")
        rpc = pc.createDataChannel("rpc")  # reliable + ordered (defaults)
        self._rpc = rpc

        @rpc.on("open")
        def _on_open():
            self._rpc_open.set()

        @rpc.on("message")
        def _on_message(message):
            self._on_rpc_message(message)

        @pc.on("track")
        def _on_track(track):
            log.info("track received: kind=%s", track.kind)
            if track.kind == "video":
                asyncio.ensure_future(self._drain_video(track))

        @pc.on("connectionstatechange")
        async def _on_state():
            log.info("pc state: %s", pc.connectionState)
            if pc.connectionState in ("failed", "closed"):
                self._rpc_open.clear()

        await pc.setLocalDescription(await pc.createOffer())
        await self._wait_ice_complete(pc)

        offer_b64 = base64.standard_b64encode(
            json.dumps(
                {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}
            ).encode()
        ).decode()

        r = await self._http.post("/webrtc/session", json={"sd": offer_b64})
        if r.status_code != 200:
            raise JetKVMError(f"webrtc/session failed: {r.status_code} {r.text}")
        answer_b64 = r.json()["sd"]
        answer = json.loads(base64.standard_b64decode(answer_b64))
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )

        for t in pc.getTransceivers():
            if t.kind == "video" and t.receiver:
                self._video_receiver = t.receiver
        self._video_ssrcs = {int(s) for s in re.findall(r"^a=ssrc:(\d+)", answer["sdp"], re.M)}

        await asyncio.wait_for(self._rpc_open.wait(), timeout=15)
        log.info("JetKVM rpc channel open")
        asyncio.ensure_future(self._kick_until_first_frame())

    async def close(self) -> None:
        if self._pc:
            await self._pc.close()
        await self._http.aclose()

    @staticmethod
    async def _wait_ice_complete(pc: RTCPeerConnection) -> None:
        if pc.iceGatheringState == "complete":
            return
        done = asyncio.Event()

        @pc.on("icegatheringstatechange")
        def _():
            if pc.iceGatheringState == "complete":
                done.set()

        try:
            await asyncio.wait_for(done.wait(), timeout=10)
        except asyncio.TimeoutError:
            log.warning("ICE gathering timed out; proceeding with partial candidates")

    # ---------- rpc ----------------------------------------------------------

    def _on_rpc_message(self, message) -> None:
        try:
            data = json.loads(message)
        except (ValueError, TypeError):
            return
        rid = data.get("id")
        if rid is None:  # server-initiated event (e.g. otherSessionConnected)
            log.debug("event: %s", data.get("method"))
            return
        fut = self._pending.pop(rid, None)
        if fut and not fut.done():
            if data.get("error") is not None:
                fut.set_exception(JetKVMError(str(data["error"])))
            else:
                fut.set_result(data.get("result"))

    async def rpc(self, method: str, **params):
        """Call a JSON-RPC method over the rpc datachannel and await its result."""
        if not self._rpc_open.is_set():
            raise JetKVMError("rpc channel not open; call connect() first")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        req = {"jsonrpc": "2.0", "method": method, "id": rid}
        if params:
            req["params"] = params
        self._rpc.send(json.dumps(req))
        return await asyncio.wait_for(fut, timeout=30)

    # ---------- video / snapshot --------------------------------------------

    async def _request_keyframe(self) -> None:
        """Ask the device for an IDR via RTCP PLI. JetKVM (fw 0.5.x) only emits a
        keyframe on feedback — browsers PLI automatically, aiortc does not."""
        recv = self._video_receiver
        if recv is None:
            return
        ssrcs = set(self._video_ssrcs)
        ssrcs.update(getattr(recv, "_RTCRtpReceiver__active_ssrc", {}) or {})
        for ssrc in ssrcs:
            try:
                await recv._send_rtcp_pli(ssrc)
            except Exception as e:
                log.debug("PLI to ssrc=%s failed: %r", ssrc, e)

    async def _kick_until_first_frame(self) -> None:
        for _ in range(20):
            if self._frame_count or self._pc is None:
                return
            await self._request_keyframe()
            await asyncio.sleep(0.5)

    async def _drain_video(self, track) -> None:
        n = 0
        while True:
            try:
                frame = await track.recv()
            except Exception as e:  # track ended / pc closed
                log.warning("_drain_video stopped after %d frames: %r", n, e)
                return
            n += 1
            self._frame_count += 1
            if n == 1:
                log.info("first video frame decoded: %sx%s", frame.width, frame.height)
            self._latest_frame = frame
            self._last_frame_at = time.monotonic()
            self._frame_event.set()

    async def snapshot(self, quality: int = 80) -> tuple[bytes, int, int]:
        """Return (jpeg_bytes, width, height) of the current screen."""
        stale = time.monotonic() - self._last_frame_at > 2.0
        if self._latest_frame is None or stale:
            self._frame_event.clear()
            await self._request_keyframe()
            try:
                await asyncio.wait_for(self._frame_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                if self._latest_frame is None:
                    raise
                log.warning("no fresh frame after keyframe request; using last frame")
        frame = self._latest_frame
        img = frame.to_image()  # PIL.Image via PyAV
        w, h = img.size
        self._last_dims = (w, h)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), w, h

    async def _dims(self) -> tuple[int, int]:
        if self._last_dims != (0, 0):
            return self._last_dims
        try:
            st = await self.rpc("getVideoState")
            w, h = int(st.get("width", 0)), int(st.get("height", 0))
            if w and h:
                self._last_dims = (w, h)
        except Exception:
            pass
        return self._last_dims or (1920, 1080)

    # ---------- input plane (the "hands") -----------------------------------

    async def _abs(self, px: int, py: int, buttons: int) -> None:
        w, h = await self._dims()
        x = max(0, min(ABS_MAX, round(px / max(1, w) * ABS_MAX)))
        y = max(0, min(ABS_MAX, round(py / max(1, h) * ABS_MAX)))
        await self.rpc("absMouseReport", x=x, y=y, buttons=buttons)

    async def move(self, px: int, py: int) -> None:
        await self._abs(px, py, 0)

    async def click(self, px: int, py: int, button: str = "left") -> None:
        b = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}.get(button, BTN_LEFT)
        await self._abs(px, py, b)
        await asyncio.sleep(0.04)
        await self._abs(px, py, 0)

    async def double_click(self, px: int, py: int) -> None:
        await self.click(px, py)
        await asyncio.sleep(0.08)
        await self.click(px, py)

    async def scroll(self, amount: int) -> None:
        # positive = up. Send in steps so big scrolls register.
        step = 1 if amount > 0 else -1
        for _ in range(abs(amount)):
            await self.rpc("wheelReport", wheelY=step, wheelX=0)
            await asyncio.sleep(0.02)

    async def type_text(self, text: str) -> None:
        for ch in text:
            rep = keymap.char_to_report(ch)
            if rep is None:
                log.warning("skipping unmapped char %r", ch)
                continue
            modifier, code = rep
            await self.rpc("keyboardReport", modifier=modifier, keys=[code])
            await self.rpc("keyboardReport", modifier=0, keys=[])
            await asyncio.sleep(0.008)

    async def press_key(self, combo: str) -> None:
        modifier, code = keymap.combo_to_report(combo)
        await self.rpc("keyboardReport", modifier=modifier, keys=[code])
        await asyncio.sleep(0.03)
        await self.rpc("keyboardReport", modifier=0, keys=[])

    # ---------- media upload (HTTP streaming, keyed by uploadId) -------------

    async def upload_to_storage(self, filename: str, data: bytes) -> dict:
        """Init a storage upload via RPC, then stream the bytes to /storage/upload."""
        res = await self.rpc("startStorageFileUpload", filename=filename, size=len(data))
        upload_id = res.get("uploadId") if isinstance(res, dict) else None
        if not upload_id:
            raise JetKVMError(f"no uploadId returned: {res!r}")
        r = await self._http.post(
            "/storage/upload",
            params={"uploadId": upload_id},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        if r.status_code != 200:
            raise JetKVMError(f"upload failed: {r.status_code} {r.text}")
        return {"uploadId": upload_id, "filename": filename, "bytes": len(data)}
