#!/usr/bin/env python3
"""Pi Frame: can this Pi fade between pictures in hardware?

Checks, step by step, what a hardware fade needs (DRM display planes with
adjustable transparency, through python3-kms++), then fades red -> blue for
4 seconds and reports the frame rate.

    sudo apt install -y python3-kms++
    sudo systemctl stop pi-photoframe      # so nothing else draws meanwhile
    python3 kms_fade_test.py               # fades back and forth for 3 minutes
    python3 kms_fade_test.py 10            # ...or for 10 minutes
    sudo systemctl start pi-photoframe

Results are also saved to ~/kms_fade_test.log, so you can read them later.
Ctrl+C stops it early. It changes nothing permanently; the screen goes back to
normal when it ends.
"""
import os
import sys
import time
import traceback

MINUTES = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
LOG = os.path.expanduser("~/kms_fade_test.log")
open(LOG, "w").close()


def say(text):
    print(text)
    with open(LOG, "a") as f:
        f.write(text + "\n")


def trace():
    say(traceback.format_exc().rstrip())


def step(n, text):
    say(f"\n[{n}] {text}")


def show_api(obj, label):
    names = [x for x in dir(obj) if not x.startswith("_")]
    say(f"    {label} offers: {', '.join(names)}")


try:
    import numpy as np
    import pykms
except ImportError as e:
    sys.exit(f"Missing: {e}. Run: sudo apt install -y python3-kms++ python3-numpy")

step(1, "Opening the display hardware")
try:
    card = pykms.Card()
    res = pykms.ResourceManager(card)
    conn = res.reserve_connector()
    crtc = res.reserve_crtc(conn)
    mode = conn.get_default_mode()
    w, h = mode.hdisplay, mode.vdisplay
    say(f"    OK: {conn.fullname}, {w}x{h} @ {mode.vrefresh} Hz")
except Exception:
    trace()
    show_api(pykms, "pykms")
    say("FAILED (details above)"); sys.exit("FAILED at step 1")

step(2, "Finding two display layers (the top one needs adjustable transparency)")
try:
    fmt = pykms.PixelFormat.XRGB8888
    primary = res.reserve_primary_plane(crtc, fmt)
    overlay = res.reserve_overlay_plane(crtc, fmt)
    if primary is None or overlay is None:
        raise RuntimeError(f"primary={primary}, overlay={overlay}")
    props = sorted(p.name for p in overlay.props) if hasattr(overlay, "props") else []
    say(f"    OK: primary plane {primary.id}, overlay plane {overlay.id}")
    say(f"    overlay properties: {', '.join(props) or '(could not list)'}")
    if props and "alpha" not in props:
        say("FAILED (details above)"); sys.exit("FAILED at step 2: the overlay plane has no 'alpha' property, so no hardware fades on this Pi")
except SystemExit:
    raise
except Exception:
    trace()
    show_api(res, "ResourceManager")
    say("FAILED (details above)"); sys.exit("FAILED at step 2")

step(3, "Making two screen buffers and drawing into them")
try:
    fb_a = pykms.DumbFramebuffer(card, w, h, fmt)
    fb_b = pykms.DumbFramebuffer(card, w, h, fmt)

    def pixels(fb):
        buf = fb.map(0)
        return np.frombuffer(buf, dtype=np.uint8).reshape(h, fb.stride(0))[:, : w * 4].reshape(h, w, 4)

    pa, pb = pixels(fb_a), pixels(fb_b)
    pa[:] = (40, 40, 220, 0)  # BGRX: red
    pb[:] = (220, 90, 40, 0)  # BGRX: blue
    say("    OK: both buffers drawn (red and blue)")
except Exception:
    trace()
    show_api(pykms.DumbFramebuffer, "DumbFramebuffer")
    say("FAILED (details above)"); sys.exit("FAILED at step 3")

step(4, "Showing red on the bottom layer")
try:
    crtc.set_mode(conn, fb_a, mode)
    say("    OK: the screen should be red now")
    time.sleep(1.5)
except Exception:
    trace()
    show_api(crtc, "Crtc")
    say("FAILED (details above)"); sys.exit("FAILED at step 4")

step(5, f"Fading between red and blue in hardware, back and forth for {MINUTES:g} minutes (Ctrl+C stops)")
try:
    req = pykms.AtomicReq(card)
    req.add(overlay, {"FB_ID": fb_b.id, "CRTC_ID": crtc.id,
                      "SRC_X": 0, "SRC_Y": 0, "SRC_W": w << 16, "SRC_H": h << 16,
                      "CRTC_X": 0, "CRTC_Y": 0, "CRTC_W": w, "CRTC_H": h, "alpha": 0})
    req.commit_sync(allow_modeset=True)
    end = time.monotonic() + MINUTES * 60
    cycle = 0
    try:
        while time.monotonic() < end:
            cycle += 1
            for direction in (1, -1):  # blue fades in over 4 s, then out again
                start, frames = time.monotonic(), 0
                while True:
                    t = (time.monotonic() - start) / 4.0
                    if t >= 1:
                        break
                    e = t * t * (3 - 2 * t)  # smooth ease in/out
                    r = pykms.AtomicReq(card)
                    r.add(overlay, "alpha", int(65535 * (e if direction == 1 else 1 - e)))
                    r.commit_sync()  # waits for the screen's next refresh
                    frames += 1
                secs = time.monotonic() - start
                say(f"    fade {cycle}{'a' if direction == 1 else 'b'} ({'red to blue' if direction == 1 else 'blue to red'}): "
                    f"{frames} frames in {secs:.2f} s = {frames / secs:.1f} fps")
                time.sleep(1.0)  # a pause on each colour
    except KeyboardInterrupt:
        say("    stopped with Ctrl+C")
    say("    Did the fades look smooth? (A sudden switch would mean the transparency was ignored.)")
except Exception:
    trace()
    show_api(pykms.AtomicReq, "AtomicReq")
    say("FAILED (details above)"); sys.exit("FAILED at step 5")

say("\nAll steps passed: this Pi can fade in hardware. Restart the frame with:"
    "\n    sudo systemctl start pi-photoframe")
