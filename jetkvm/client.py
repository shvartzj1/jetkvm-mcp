"""JetKVM client: one WebRTC peer connection drives both planes.

  * video track  -> decoded frames -> JPEG snapshots   (the "eyes")
  * "rpc" datachannel -> every JSON-RPC method          (the "hands" + device control)

No firmware change is required: snapshots come from the H.264 video track that the
device already streams, decoded locally with PyAV (an aiortc dependency).

Handshake (verified against jetkvm/kvm source and a live 0.5.x device):
  POST /auth/login-local   {"password": ...}              -> sets authToken cookie

then one of two signaling paths:
  * websocket (firmware >= 0.5): WS /webrtc/signaling/client, JSON {type, data}
    frames -- we send {"type":"offer","data":{"sd":base64}}, the device replies
    with {"type":"answer","data":base64} and then *trickles* its ICE candidates
    as {"type":"new-ice-candidate","data":RTCIceCandidateInit}.
  * legacy: POST /webrtc/session {"sd": base64} -> {"sd": base64(answer)}

Prefer the websocket. The legacy answer carries no ICE candidates at all, so the
connection can only come up if the *device* can reach the address we advertised --
true on a LAN, false from inside a container or behind any NAT, where ICE then sits
in "checking" forever. Consuming the trickled candidates lets us reach out instead.

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
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from . import keymap

log = logging.getLogger("jetkvm")

ABS_MAX = 32767  # HID absolute-pointer logical maximum (hid_mouse_absolute.go)
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x01, 0x02, 0x04


class JetKVMError(RuntimeError):
    pass


class JetKVMDisconnected(JetKVMError):
    """The peer connection died (device reboot, network loss)."""


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
        self._content_box = None  # (l, t, r, b) of the desktop inside the frame, if letterboxed
        self._video_receiver = None
        self._video_ssrcs: set[int] = set()
        self._answer_sdp = ""
        self._ws = None  # signaling websocket, kept open for late candidates
        self._ws_task: asyncio.Task | None = None

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
                # fail in-flight calls immediately instead of letting them hit the 30s timeout
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(JetKVMDisconnected(f"connection {pc.connectionState}"))
                self._pending.clear()

        await pc.setLocalDescription(await pc.createOffer())
        await self._wait_ice_complete(pc)

        offer_b64 = base64.standard_b64encode(
            json.dumps(
                {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}
            ).encode()
        ).decode()

        if not await self._negotiate_ws(pc, offer_b64):
            await self._negotiate_http(pc, offer_b64)

        for t in pc.getTransceivers():
            if t.kind == "video" and t.receiver:
                self._video_receiver = t.receiver
        self._video_ssrcs = {int(s) for s in re.findall(r"^a=ssrc:(\d+)", self._answer_sdp, re.M)}

        await asyncio.wait_for(self._rpc_open.wait(), timeout=15)
        log.info("JetKVM rpc channel open")
        asyncio.ensure_future(self._kick_until_first_frame())

    @property
    def connected(self) -> bool:
        """True while the peer connection and rpc channel are actually usable."""
        return (
            self._pc is not None
            and self._pc.connectionState == "connected"
            and self._rpc is not None
            and self._rpc.readyState == "open"
            and self._rpc_open.is_set()
        )

    async def close(self) -> None:
        await self._close_ws()
        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass  # closing a dead connection must never raise
        try:
            await self._http.aclose()
        except Exception:
            pass

    # ---------- signaling -----------------------------------------------------

    async def _negotiate_ws(self, pc: RTCPeerConnection, offer_b64: str) -> bool:
        """Offer/answer over the device's signaling websocket, consuming trickled
        ICE candidates. Returns False if the device doesn't speak it, so the
        caller can fall back to the legacy POST."""
        ws_url = re.sub(r"^http", "ws", self.base_url) + "/webrtc/signaling/client"
        cookie = "; ".join(f"{k}={v}" for k, v in self._http.cookies.items())
        try:
            ws = await asyncio.wait_for(self._ws_connect(ws_url, cookie), timeout=10)
        except Exception as e:
            log.info("websocket signaling unavailable (%s); falling back to POST", e)
            return False

        answered = asyncio.Event()
        early: list[dict] = []  # candidates that beat the answer must wait for it

        async def pump() -> None:
            async for raw in ws:
                if raw == "pong":  # heartbeat
                    continue
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                kind, data = msg.get("type"), msg.get("data")
                if kind == "answer" and not answered.is_set():
                    answer = json.loads(base64.standard_b64decode(data))
                    self._answer_sdp = answer["sdp"]
                    await pc.setRemoteDescription(
                        RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
                    )
                    answered.set()
                    for c in early:
                        await self._add_remote_candidate(pc, c)
                    early.clear()
                elif kind == "new-ice-candidate":
                    if answered.is_set():
                        await self._add_remote_candidate(pc, data)
                    else:
                        early.append(data)

        await ws.send(json.dumps({"type": "offer", "data": {"sd": offer_b64}}))
        self._ws = ws
        self._ws_task = asyncio.ensure_future(pump())

        try:
            await asyncio.wait_for(answered.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.warning("no answer over websocket; falling back to POST")
            await self._close_ws()
            return False
        # Stay subscribed: the device keeps trickling candidates after the answer,
        # and those are the only ones a NAT'd client can actually reach it on.
        return True

    @staticmethod
    async def _ws_connect(url: str, cookie: str):
        headers = {"Cookie": cookie}
        try:
            return await websockets.connect(url, additional_headers=headers)
        except TypeError:  # websockets < 14 spells it differently
            return await websockets.connect(url, extra_headers=headers)

    async def _negotiate_http(self, pc: RTCPeerConnection, offer_b64: str) -> None:
        """Legacy one-shot exchange. The answer carries no candidates, so this only
        works when the device can reach the address we advertised."""
        r = await self._http.post("/webrtc/session", json={"sd": offer_b64})
        if r.status_code != 200:
            raise JetKVMError(f"webrtc/session failed: {r.status_code} {r.text}")
        answer = json.loads(base64.standard_b64decode(r.json()["sd"]))
        self._answer_sdp = answer["sdp"]
        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=answer["sdp"], type=answer["type"])
        )

    @staticmethod
    async def _add_remote_candidate(pc: RTCPeerConnection, init: dict | None) -> None:
        raw = (init or {}).get("candidate")
        if not raw:
            return  # end-of-candidates sentinel
        cand = candidate_from_sdp(raw.split(":", 1)[1] if raw.startswith("candidate:") else raw)
        cand.sdpMid = (init or {}).get("sdpMid")
        cand.sdpMLineIndex = (init or {}).get("sdpMLineIndex")
        await pc.addIceCandidate(cand)
        log.debug("added remote candidate %s:%s", cand.ip, cand.port)

    async def _close_ws(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

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

    async def ping(self, timeout: float = 3.0) -> bool:
        """True if the device answers on the rpc channel right now.

        Any response — even a JSON-RPC error — proves the channel round-trips;
        only silence or a dead connection counts as failure. Much faster death
        detection than waiting ~30s for WebRTC consent expiry."""
        if not self.connected:
            return False
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[rid] = fut
        try:
            self._rpc.send(json.dumps({"jsonrpc": "2.0", "method": "ping", "id": rid}))
            await asyncio.wait_for(fut, timeout=timeout)
            return True
        except JetKVMDisconnected:
            return False
        except JetKVMError:
            return True  # an error response still came over a working channel
        except Exception:
            self._pending.pop(rid, None)
            return False

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
        self._last_dims = img.size
        box = self._detect_content_box(img)
        if box is not None:
            cur = self._content_box
            # codec noise wobbles edges by a pixel; only recalibrate on real change
            if cur is None or any(abs(a - b) > 2 for a, b in zip(box, cur)):
                log.info("content box calibrated: %s in %sx%s frame", box, *img.size)
                self._content_box = box
        if self._content_box:
            img = img.crop(self._content_box)
        w, h = img.size
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return buf.getvalue(), w, h

    @staticmethod
    def _detect_content_box(img) -> tuple[int, int, int, int] | None:
        """Locate the desktop inside a frame that may carry black letterbox bars
        (hosts that underscan their HDMI output). Returns a crop box; the full
        frame when no bars are present; None when the frame is too dark to
        judge, so the caller keeps its previous calibration.

        Mouse math depends on this: the HID absolute range maps to the host's
        desktop, which is exactly the content box — not the full frame."""
        fw, fh = img.size
        g = img.convert("L")
        corners = [g.getpixel(p) for p in ((0, 0), (fw - 1, 0), (0, fh - 1), (fw - 1, fh - 1))]
        black = max(corners)
        if black > 32:
            return (0, 0, fw, fh)  # bright corners: no bars
        bbox = g.point(lambda p: 255 if p > black + 8 else 0).getbbox()
        if bbox is None:
            return None  # all-dark frame (host asleep, fullscreen console): can't judge

        # Real letterboxing is roughly centered. A one-sided "bar" is dark UI
        # (console text, dark windows) — don't trim that axis.
        def _axis(lo: int, hi: int, size: int) -> tuple[int, int]:
            near, far = lo, size - hi
            if min(near, far) < 3 or max(near, far) > 3 * min(near, far) + 8:
                return 0, size
            return lo, hi

        l, r = _axis(bbox[0], bbox[2], fw)
        t, b = _axis(bbox[1], bbox[3], fh)
        if (r - l) < fw * 0.6 or (b - t) < fh * 0.6:
            return None  # implausible shrink: dark content, not bars
        return (l, t, r, b)

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
        # px/py are pixels on the last snapshot, which is cropped to the content
        # box; both the cropped image and the HID range span exactly the desktop.
        if self._content_box:
            l, t, r, b = self._content_box
            w, h = r - l, b - t
        else:
            w, h = await self._dims()
        x = max(0, min(ABS_MAX, round(px / max(1, w) * ABS_MAX)))
        y = max(0, min(ABS_MAX, round(py / max(1, h) * ABS_MAX)))
        if x == 0 and y == 0:
            x = y = 1  # the device silently drops (0,0) absolute reports
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
