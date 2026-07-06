"""Live LAN smoke test: connect, hold open, verify sustained video + multiple snapshots."""
import asyncio, logging, os, sys, tempfile, time
from jetkvm import JetKVMClient

logging.basicConfig(level=logging.WARNING)  # quiet; we print our own progress

URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("JETKVM_URL")
SCRATCH = os.environ.get("SMOKE_OUT", tempfile.gettempdir())
if not URL:
    sys.exit("usage: smoke_test.py <device-url>   (or set JETKVM_URL)")


async def main():
    c = JetKVMClient(
        base_url=URL,
        password=os.environ.get("JETKVM_PASSWORD", ""),
        verify_tls=os.environ.get("JETKVM_VERIFY_TLS", "").lower() == "true",
    )
    print(f"connecting to {URL} ...")
    await c.connect()
    print("connected. video_state:", await c.rpc("getVideoState"))

    # Hold the connection open and snapshot every 2s for 8s to prove sustained video.
    for i in range(4):
        await asyncio.sleep(2)
        t0 = time.monotonic()
        data, w, h = await c.snapshot()
        dt = (time.monotonic() - t0) * 1000
        path = f"{SCRATCH}/shot_{i}.jpg"
        with open(path, "wb") as f:
            f.write(data)
        print(f"  snapshot {i}: {w}x{h}  {len(data):>7} bytes  (grab {dt:4.0f} ms)  frames_seen={c._frame_count}")

    await c.close()
    print("done.")


asyncio.run(main())
