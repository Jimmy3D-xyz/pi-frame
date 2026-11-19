#!/usr/bin/env python3
"""Pi Frame boot screen: replaces the rainbow, the four raspberries and the
scrolling console text with a full-screen Pi Frame image, using Raspberry Pi's
official rpi-splash-screen-support (the kernel draws the image very early in
boot), and keeps console text off the screen at shutdown too.

    sudo python3 deploy/bootsplash.py install     # set it up (install.sh runs this)
    sudo python3 deploy/bootsplash.py remove      # undo everything

Boot-setting changes (backed up first, to *.pre-bootsplash):
  cmdline.txt: console=tty1 -> console=tty3, and add loglevel=5
               fullscreen_logo=1 fullscreen_logo_name=logo.tga vt.global_cursor_default=0
  config.txt:  disable_splash=1 (no rainbow square at power-on)

Why console=tty3 rather than removing console=tty1 (as Raspberry Pi's own
tool does): on a Pi whose serial port is off (the default on a Pi 4), that
leaves no console at all, and the initramfs can't open one, so boot stops at
the logo. tty3 is a hidden virtual screen: boot has a console, nobody sees it.
console= doesn't stop the kernel's log being drawn on the visible screen,
though. Turning the log level down to "quiet" (4 or less) would hide it, but
the kernel then doesn't draw its logo at all, so loglevel=5 (warnings only),
and pi-frame-splash.service, very early in boot, switches the screen to
graphics mode (no console text at all from then on) and redraws the splash
cleanly over the few warnings printed before it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BOOT = Path("/boot/firmware") if Path("/boot/firmware").is_dir() else Path("/boot")
DATA = Path("/var/lib/pi-photoframe")
IMAGE = Path("/usr/share/pi-photoframe/splash.tga")
IMAGE_PNG = Path("/usr/share/pi-photoframe/splash.png")  # what pi-frame-splash.service redraws
IMAGE_META = Path("/usr/share/pi-photoframe/splash.json")  # the size/orientation they were made for
BOOT_NOTE = "Starting up… this takes about a minute"
UNIT = Path("/etc/systemd/system/pi-frame-splash.service")
UNIT_TEXT = """[Unit]
Description=Pi Frame boot splash (keeps console text off the screen)
DefaultDependencies=no
After=systemd-remount-fs.service
Before=systemd-udev-trigger.service sysinit.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {app}/deploy/console_splash.py

[Install]
WantedBy=sysinit.target
"""
MAX_W, MAX_H, MAX_COLOURS = 1920, 1080, 224  # limits of the kernel's fullscreen logo
SPLASH_PARAMS = ["loglevel=5", "fullscreen_logo=1", "fullscreen_logo_name=logo.tga", "vt.global_cursor_default=0"]
REMOVE_PARAMS = ["plymouth.ignore-serial-consoles"]
SCREEN_CONSOLE, HIDDEN_CONSOLE = "console=tty1", "console=tty3"
CONFIG_LINE = "disable_splash=1"
CONFIG_MARK = "# Pi Frame: no rainbow square at power-on"


# ---------------------------------------------------------------- the image
def panel_size() -> tuple[int, int]:
    try:
        w, h = (int(x) for x in Path("/sys/class/graphics/fb0/virtual_size").read_text().split(","))
        if w > 0 and h > 0:
            return w, h
    except (OSError, ValueError):
        pass
    return 1920, 1080


def _draw(panel: tuple[int, int], orientation: str, rotation: int):
    """The boot image at the screen's full size; returns (image, content box
    in panel pixels) where the box covers the logo and wordmark."""
    from PIL import Image

    from photoframe import render, screens

    logical = render.logical_size(panel, orientation)
    w, h = logical
    unit = min(w, h)
    img = screens._background(logical)
    # big centred logo with its glow, and the wordmark underneath
    side = int(unit * 0.34)
    cy = int(h * 0.44)
    screens._hero(img, w // 2, cy, side)
    word_h = int(unit * 0.085)
    word_w = screens.draw_wordmark(img, 0, 0, word_h, with_mark=False, measure=True)
    wy = int(cy + side * 0.62)
    screens.draw_wordmark(img, (w - word_w) // 2, wy, word_h, with_mark=False)
    # what must survive trimming: logo (with a margin for its shadow) and wordmark
    box = (min(w // 2 - side // 2, (w - word_w) // 2) - 8, cy - side // 2 - 8,
           max(w // 2 + side // 2, (w + word_w) // 2) + 8, wy + word_h + 8)
    # turn the box the same way as the picture
    mask = Image.new("L", logical, 0)
    mask.paste(255, box)
    panel_mask = render.to_panel(mask, panel, rotation)
    return render.to_panel(img, panel, rotation), panel_mask.getbbox()


def make_images(panel: tuple[int, int], orientation: str = "landscape", rotation: int = 0):
    """Returns (kernel_image, full_image).

    full_image is the whole screen, for the clean redraw 5 s into boot.
    kernel_image is what the kernel draws in the first seconds: at most
    1920x1080 and 224 colours. For a bigger screen it's the *centre* of
    full_image, trimmed rather than shrunk, so the logo is the same size and in
    the same place in both and nothing jumps when one replaces the other (the
    kernel draws its logo centred). Only if the logo wouldn't survive the trim
    is the whole picture shrunk instead."""
    from PIL import Image

    full, box = _draw(panel, orientation, rotation)
    # both images share one 224-colour palette, so they match exactly
    full = full.quantize(colors=MAX_COLOURS - 4, method=Image.Quantize.MEDIANCUT,
                         dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
    cw, ch = min(panel[0], MAX_W), min(panel[1], MAX_H)
    x0, y0 = (panel[0] - cw) // 2, (panel[1] - ch) // 2
    if box[0] >= x0 and box[1] >= y0 and box[2] <= x0 + cw and box[3] <= y0 + ch:
        kernel = full.crop((x0, y0, x0 + cw, y0 + ch))
    else:
        scale = min(MAX_W / panel[0], MAX_H / panel[1])
        size = (int(panel[0] * scale) // 2 * 2, int(panel[1] * scale) // 2 * 2)
        small, _ = _draw(size, orientation, rotation)
        kernel = small.quantize(colors=MAX_COLOURS - 4, method=Image.Quantize.MEDIANCUT,
                                dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
    return kernel, full


def add_note(full, panel: tuple[int, int], orientation: str = "landscape", rotation: int = 0,
             note: str = BOOT_NOTE):
    """The redraw's version: the same picture with a line under the wordmark.
    Drawn on top of the finished image, so the logo's pixels don't change and
    nothing jumps when it replaces the kernel's logo."""
    from PIL import Image, ImageDraw

    from photoframe import render, screens

    logical = render.logical_size(panel, orientation)
    w, h = logical
    unit = min(w, h)
    side, cy, word_h = int(unit * 0.34), int(h * 0.44), int(unit * 0.085)
    layer = Image.new("RGBA", logical, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = screens._font("sans", max(14, int(unit * 0.03)))
    tw = d.textlength(note, font=f)
    d.text(((w - tw) / 2, int(cy + side * 0.62) + word_h + int(unit * 0.05)), note, font=f, fill=(196, 190, 178, 255))
    return Image.alpha_composite(full.convert("RGBA"), render.to_panel(layer, panel, rotation)).convert("RGB")


def make_image(out: Path, panel: tuple[int, int], orientation: str = "landscape", rotation: int = 0):
    """Write the kernel's TGA; returns (kernel_image, full_image)."""
    kernel, full = make_images(panel, orientation, rotation)
    out.parent.mkdir(parents=True, exist_ok=True)
    kernel.save(out, format="TGA")  # uncompressed, 24-bit
    return kernel, full


def check_image(path: Path) -> list[str]:
    from PIL import Image

    problems = []
    with Image.open(path) as im:
        if im.format != "TGA":
            problems.append(f"not a TGA file ({im.format})")
        if im.mode != "RGB":
            problems.append(f"not 24-bit RGB ({im.mode})")
        if im.width > MAX_W or im.height > MAX_H:
            problems.append(f"too big ({im.width}x{im.height})")
        colours = im.convert("RGB").getcolors(maxcolors=100000)
        if colours is None or len(colours) > MAX_COLOURS:
            problems.append(f"too many colours ({len(colours) if colours else 'lots'})")
    return problems


# ---------------------------------------------------------------- boot settings
def edit_cmdline(text: str, enable: bool) -> str:
    params = text.replace("\r", " ").replace("\n", " ").split()
    ours = {p.split("=")[0] for p in SPLASH_PARAMS}
    if enable:
        # keep a console, but on a hidden screen; it stays where tty1 was
        params = [HIDDEN_CONSOLE if p == SCREEN_CONSOLE else p for p in params]
        if HIDDEN_CONSOLE not in params:
            idx = next((i for i, p in enumerate(params) if p.startswith("root=")), len(params))
            params.insert(idx, HIDDEN_CONSOLE)
        params = [p for p in params if p not in REMOVE_PARAMS and p.split("=")[0] not in ours]
        params += SPLASH_PARAMS
    else:
        params = [p for p in params if p.split("=")[0] not in ("fullscreen_logo", "fullscreen_logo_name", "loglevel")]
        params = [SCREEN_CONSOLE if p == HIDDEN_CONSOLE else p for p in params]
        if SCREEN_CONSOLE not in params:
            idx = next((i for i, p in enumerate(params) if p.startswith("root=")), len(params))
            params.insert(idx, SCREEN_CONSOLE)
    return " ".join(params) + "\n"


def edit_config(text: str, enable: bool) -> str:
    lines = text.splitlines()
    lines = [l for l in lines if l.strip() not in (CONFIG_LINE, CONFIG_MARK)]
    # tidy up after a previous run: an empty trailing [all] section is ours
    while lines and (not lines[-1].strip() or lines[-1].strip() == "[all]"):
        lines.pop()
    if enable:
        # at the very end, under [all], so no earlier [section] filter can hide it
        lines += ["", "[all]", CONFIG_MARK, CONFIG_LINE]
    return "\n".join(lines) + "\n"


def _backup(path: Path) -> None:
    bak = path.with_name(path.name + ".pre-bootsplash")
    if not bak.exists():
        shutil.copy2(path, bak)


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def made_for() -> dict:
    cfg = {}
    try:
        cfg = json.loads((DATA / "config.json").read_text()).get("config", {})
    except (OSError, ValueError):
        pass
    return {"orientation": cfg.get("display_orientation", "landscape"),
            "rotation": int(cfg.get("display_rotation_deg", 0))}


def install(size: tuple[int, int] | None = None) -> int:
    """Make the boot images. size: the screen size to make them for (default:
    the current screen; a resolution change passes the new one before restarting)."""
    cfg = {}
    try:
        cfg = json.loads((DATA / "config.json").read_text()).get("config", {})
    except (OSError, ValueError):
        pass
    panel = size or panel_size()
    orient, rot = cfg.get("display_orientation", "landscape"), int(cfg.get("display_rotation_deg", 0))
    kernel, full = make_image(IMAGE, panel, orient, rot)
    # the clean redraw: whole screen, same logo pixels as the kernel's, plus "please wait"
    add_note(full, panel, orient, rot).save(IMAGE_PNG)
    IMAGE_META.write_text(json.dumps({"size": list(panel), "orientation": orient, "rotation": rot}))
    problems = check_image(IMAGE)
    if problems:
        print("    boot image isn't usable: " + "; ".join(problems))
        return 1
    print(f"    boot image: {IMAGE} (for a {panel[0]}x{panel[1]} screen, {cfg.get('display_orientation', 'landscape')})")
    if shutil.which("configure-splash"):
        r = subprocess.run(["configure-splash", str(IMAGE), "--no-cmdline", "--skip-image-checks"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("    configure-splash failed: " + (r.stderr or r.stdout).strip()[-300:])
            return 1
    else:
        print("    rpi-splash-screen-support isn't installed: the console text will be hidden, but no image shown")
    # the early step that turns console text off and redraws the splash cleanly
    UNIT.parent.mkdir(parents=True, exist_ok=True)
    UNIT.write_text(UNIT_TEXT.format(app=ROOT))
    if shutil.which("systemctl"):
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "enable", UNIT.name], check=False, capture_output=True)
    cmdline, config = BOOT / "cmdline.txt", BOOT / "config.txt"
    for path, edit in ((cmdline, edit_cmdline), (config, edit_config)):
        if path.exists():
            _backup(path)
            new = edit(path.read_text(), True)
            if new != path.read_text():
                _write(path, new)
                print(f"    updated {path} (backup: {path.name}.pre-bootsplash)")
    return 0


def remove() -> int:
    for path, edit in ((BOOT / "cmdline.txt", edit_cmdline), (BOOT / "config.txt", edit_config)):
        if path.exists():
            _write(path, edit(path.read_text(), False))
            print(f"    restored {path}")
    if UNIT.exists():
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "disable", UNIT.name], check=False, capture_output=True)
        UNIT.unlink()
    hook = Path("/etc/initramfs-tools/hooks/splash-screen-hook.sh")
    if hook.exists():
        hook.unlink()
        subprocess.run(["update-initramfs", "-k", "all", "-u"], check=False)
    print("    boot screen removed; reboot to see the normal boot text again")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "preview":  # write a PNG preview without touching the system
        out = Path(sys.argv[2] if len(sys.argv) > 2 else "splash-preview.png")
        size = tuple(int(x) for x in sys.argv[3].split("x")) if len(sys.argv) > 3 else (1920, 1080)
        add_note(make_image(out.with_suffix(".tga"), size)[1], size).save(out)
        print("wrote", out)
        sys.exit(0)
    if cmd not in ("install", "remove"):
        print(__doc__)
        sys.exit(2)
    if cmd == "install" and len(sys.argv) > 2:  # e.g. install 2560x1440
        sys.exit(install(tuple(int(x) for x in sys.argv[2].lower().split("x"))))
    sys.exit(install() if cmd == "install" else remove())
