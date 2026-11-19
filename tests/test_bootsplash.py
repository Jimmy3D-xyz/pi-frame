import json
import importlib.util
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bootsplash", ROOT / "deploy" / "bootsplash.py")
bs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bs)

# the developer's actual cmdline.txt (from the Pi), plus the 32-bit colour option added later
CMDLINE = ("console=serial0,115200 console=tty1 root=PARTUUID=4d917939-02 rootfstype=ext4 fsck.repair=yes rootwait "
           "ds=nocloud;i=rpi-imager-1790571569585 cfg80211.ieee80211_regdom=AU consoleblank=0 vt.global_cursor_default=0 "
           "video=HDMI-A-1:1920x1080-32@60\n")
CONFIG = "# For more options...\ndtparam=audio=on\nauto_initramfs=1\n\n[cm4]\notg_mode=1\n\n[all]\narm_boost=1\n"


def test_cmdline_enable_and_undo():
    on = bs.edit_cmdline(CMDLINE, True)
    params = on.split()
    # a console is kept (the boot needs one when the serial port is off), but on hidden tty3,
    # in the same place tty1 was
    assert "console=tty1" not in params and "console=tty3" in params
    assert params.index("console=tty3") == 1 and params[0] == "console=serial0,115200"
    assert "loglevel=5" in params  # warnings only; at 4 or below ("quiet") the kernel skips its logo
    for keep in ("root=PARTUUID=4d917939-02", "ds=nocloud;i=rpi-imager-1790571569585", "consoleblank=0",
                 "video=HDMI-A-1:1920x1080-32@60", "cfg80211.ieee80211_regdom=AU"):
        assert keep in params
    assert params.count("vt.global_cursor_default=0") == 1  # not duplicated
    assert "fullscreen_logo=1" in params and "fullscreen_logo_name=logo.tga" in params
    assert on.count("\n") == 1  # still one line
    assert bs.edit_cmdline(on, True) == on  # running it again changes nothing
    off = bs.edit_cmdline(on, False).split()
    assert "fullscreen_logo=1" not in off and "loglevel=5" not in off and "console=tty3" not in off
    assert off.index("console=tty1") == off.index("root=PARTUUID=4d917939-02") - 1


def test_cmdline_from_the_hand_edited_test():
    # what the developer's Pi has now, after the manual test: tty3 + logo settings + loglevel=5
    now = ("console=serial0,115200 console=tty3 root=PARTUUID=ad496537-02 rootfstype=ext4 fsck.repair=yes rootwait "
           "ds=nocloud;i=rpi-imager-1790666843069 cfg80211.ieee80211_regdom=AU consoleblank=0 vt.global_cursor_default=0 "
           "video=HDMI-A-1:1920x1080-32@60 fullscreen_logo=1 fullscreen_logo_name=logo.tga loglevel=1\n")
    out = bs.edit_cmdline(now, True).split()
    assert out.count("console=tty3") == 1 and out.count("fullscreen_logo=1") == 1
    assert "loglevel=5" in out and "loglevel=1" not in out  # the logo needs 5 or more
    assert sorted(p for p in out if not p.startswith("loglevel")) == sorted(p for p in now.split() if not p.startswith("loglevel"))


def test_config_enable_and_undo():
    on = bs.edit_config(CONFIG, True)
    assert on.rstrip().endswith("[all]\n# Pi Frame: no rainbow square at power-on\ndisable_splash=1")
    assert "[cm4]\notg_mode=1" in on and "arm_boost=1" in on
    assert bs.edit_config(on, True) == on  # idempotent
    assert bs.edit_config(on, False).strip() == CONFIG.strip()  # undo restores it exactly


def test_image_meets_kernel_limits(tmp_path):
    out = tmp_path / "s.tga"
    bs.make_image(out, (1920, 1080))
    assert bs.check_image(out) == []
    big = tmp_path / "big.tga"
    bs.make_image(big, (3840, 2160))  # a 4K screen: kept within the 1920x1080 limit
    assert Image.open(big).size == (1920, 1080) and bs.check_image(big) == []


def test_no_jump_between_kernel_logo_and_redraw():
    """the developer's 1920x1200 screen: the kernel's logo (max 1920x1080, drawn centred)
    must be pixel-identical to the middle of the full-screen redraw."""
    kernel, full = bs.make_images((1920, 1200))
    assert kernel.size == (1920, 1080) and full.size == (1920, 1200)
    centre = full.crop((0, 60, 1920, 1140))
    assert list(centre.getdata()) == list(kernel.getdata())  # trimmed, not shrunk: nothing moves
    assert len(full.getcolors(100000)) <= 224
    # the same holds for a portrait-mounted 1920x1200 screen
    k2, f2 = bs.make_images((1920, 1200), "portrait")
    assert list(f2.crop((0, 60, 1920, 1140)).getdata()) == list(k2.getdata())


def test_image_is_upright_for_a_portrait_screen(tmp_path):
    port = tmp_path / "p.tga"
    bs.make_image(port, (1920, 1080), "portrait")
    img = Image.open(port)
    assert img.size == (1920, 1080)  # still the panel's shape
    # turn it back the way a portrait-mounted screen shows it: the wordmark
    # (cream text) must sit below the logo, near the horizontal centre
    upright = img.rotate(90, expand=True).convert("L")
    w, h = upright.size
    assert (w, h) == (1080, 1920)
    band = upright.crop((int(w * 0.3), int(h * 0.53), int(w * 0.7), int(h * 0.62)))
    assert max(band.getdata()) > 200
    sides = upright.crop((0, int(h * 0.53), int(w * 0.08), int(h * 0.62)))
    assert max(sides.getdata()) < 120  # and nothing bright off to the sides


def test_install_and_remove_end_to_end(tmp_path, monkeypatch):
    import json
    import os
    import stat
    boot, data = tmp_path / "boot", tmp_path / "data"
    boot.mkdir(); data.mkdir()
    (boot / "cmdline.txt").write_text(CMDLINE)
    (boot / "config.txt").write_text(CONFIG)
    (data / "config.json").write_text(json.dumps({"config": {"display_orientation": "landscape"}}))
    bindir = tmp_path / "bin"; bindir.mkdir()
    log = tmp_path / "calls.txt"
    tool = bindir / "configure-splash"  # stands in for Raspberry Pi's tool
    tool.write_text(f"#!/bin/sh\necho \"$@\" >> {log}\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setattr(bs, "BOOT", boot)
    monkeypatch.setattr(bs, "DATA", data)
    monkeypatch.setattr(bs, "IMAGE", tmp_path / "splash.tga")
    monkeypatch.setattr(bs, "IMAGE_PNG", tmp_path / "splash.png")
    monkeypatch.setattr(bs, "IMAGE_META", tmp_path / "splash.json")
    monkeypatch.setattr(bs, "UNIT", tmp_path / "pi-frame-splash.service")
    assert bs.install() == 0
    called = log.read_text().split()
    assert called[0] == str(tmp_path / "splash.tga") and "--no-cmdline" in called  # we edit cmdline ourselves
    assert (boot / "cmdline.txt.pre-bootsplash").read_text() == CMDLINE  # backups taken
    assert (boot / "config.txt.pre-bootsplash").read_text() == CONFIG
    assert "fullscreen_logo=1" in (boot / "cmdline.txt").read_text()
    assert "disable_splash=1" in (boot / "config.txt").read_text()
    unit = (tmp_path / "pi-frame-splash.service").read_text()
    assert "Before=systemd-udev-trigger.service" in unit and "console_splash.py" in unit
    assert (tmp_path / "splash.png").exists()
    meta = json.loads((tmp_path / "splash.json").read_text())
    assert meta["orientation"] == "landscape" and meta["rotation"] == 0  # what the images were made for
    assert bs.install((2560, 1440)) == 0  # for a given size (a resolution change, before restarting)
    assert json.loads((tmp_path / "splash.json").read_text())["size"] == [2560, 1440]
    assert Image.open(tmp_path / "splash.png").size == (2560, 1440)
    assert bs.install() == 0 and (boot / "config.txt").read_text().count("disable_splash=1") == 1  # re-run safe
    assert bs.remove() == 0
    assert not (tmp_path / "pi-frame-splash.service").exists()
    assert "console=tty1" in (boot / "cmdline.txt").read_text().split()
    assert sorted((boot / "cmdline.txt").read_text().split()) == sorted(CMDLINE.split())  # back as it was
    assert (boot / "config.txt").read_text().strip() == CONFIG.strip()
