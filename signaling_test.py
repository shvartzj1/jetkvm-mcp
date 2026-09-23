"""Offline check of the signaling websocket's TLS handling — no device needed.

Regression guard for the verify_tls path: websockets rejects an explicit
`ssl=None` on a wss:// URI, and _negotiate_ws swallows the resulting ValueError
into a silent fall back to the legacy POST — whose answer carries no ICE
candidates, so NAT'd/Docker clients stop connecting. The failure is invisible
at runtime, so it gets a test.

    python signaling_test.py
"""
import asyncio
import ssl
import sys

from jetkvm import JetKVMClient

failures: list[str] = []


def check(cond: bool, what: str) -> None:
    if not cond:
        failures.append(what)


WSS = "wss://10.0.0.1:443/webrtc/signaling/client"
WS = "ws://10.0.0.1/webrtc/signaling/client"


def client(url: str, verify_tls: bool) -> JetKVMClient:
    return JetKVMClient(base_url=url, verify_tls=verify_tls)


# ── _ws_ssl returns a context only where one is actually needed ─────────────
c = client("https://10.0.0.1", verify_tls=False)
ctx = c._ws_ssl(WSS)
check(isinstance(ctx, ssl.SSLContext), "wss + verify_tls=False: needs a permissive context")
if isinstance(ctx, ssl.SSLContext):
    check(ctx.verify_mode == ssl.CERT_NONE, "wss + verify_tls=False: must not verify")
    check(not ctx.check_hostname, "wss + verify_tls=False: must not check hostname")

check(
    client("https://10.0.0.1", verify_tls=True)._ws_ssl(WSS) is None,
    "wss + verify_tls=True: no context, so the library builds its verifying default",
)
check(
    client("http://10.0.0.1", verify_tls=False)._ws_ssl(WS) is None,
    "ws://: no TLS context at all",
)


# ── the real regression: connecting must never raise ValueError ─────────────
# A refused/timed-out TCP connection means we got past argument validation and
# actually dialed, which is all this test can prove without a device.
async def dial(url: str, verify: bool) -> Exception | None:
    cl = client(url.replace("wss://", "https://").replace("ws://", "http://"), verify_tls=verify)
    try:
        await asyncio.wait_for(cl._ws_connect(url, ""), 3)
    except Exception as e:  # noqa: BLE001 — the type is exactly what we're asserting on
        return e
    finally:
        await cl.close()
    return None


async def main() -> None:
    for url, verify, label in (
        (WSS, True, "wss + verify_tls=True"),
        (WSS, False, "wss + verify_tls=False"),
        (WS, False, "ws + verify_tls=False"),
    ):
        err = await dial(url, verify)
        check(
            not isinstance(err, (ValueError, TypeError)),
            f"{label}: rejected before dialing -> {type(err).__name__}: {err}",
        )


asyncio.run(main())

if failures:
    print(f"FAILED ({len(failures)}):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ok — signaling TLS argument handling")
