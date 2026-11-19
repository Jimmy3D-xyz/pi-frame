"""Turn photos into full-screen frames for an RGB LCD.

Where the ESP32 firmware tone-maps and dithers to a 6-colour e-paper palette,
an LCD needs none of that: we only EXIF-orient, scale (fit or cover), fill the
margins, optionally build a two-photo collage, and rotate to the panel.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps

NAMED_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "yellow": (255, 230, 0),
    "red": (200, 30, 30),
    "blue": (20, 60, 160),
    "green": (40, 110, 60),
}

GAP = 12  # px between collage tiles


HEIF_BRANDS = (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis", b"avif")


def is_heif(head: bytes) -> bool:
    """iPhone photos (HEIC/HEIF) start with an ISO box whose type is 'ftyp'."""
    return len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in HEIF_BRANDS


def _heif_to_jpeg(data: bytes) -> bytes:
    """Decode HEIC/HEIF with pillow-heif if installed, else libheif's
    heif-convert (apt: libheif-examples)."""
    try:
        import pillow_heif  # type: ignore

        heif = pillow_heif.open_heif(io.BytesIO(data))
        img = heif.to_pillow()
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "JPEG", quality=95, exif=img.info.get("exif", b""))
        return buf.getvalue()
    except ImportError:
        pass
    tool = shutil.which("heif-convert") or shutil.which("heif-dec")
    if not tool:
        raise ValueError("HEIC photos need the libheif-examples package (re-run install.sh)")
    with tempfile.TemporaryDirectory() as d:
        src, dst = Path(d) / "in.heic", Path(d) / "out.jpg"
        src.write_bytes(data)
        r = subprocess.run([tool, "-q", "95", str(src), str(dst)], capture_output=True, timeout=120)
        if r.returncode != 0 or not dst.exists():
            r = subprocess.run([tool, str(src), str(dst)], capture_output=True, timeout=120)
        if not dst.exists():
            raise ValueError("couldn't read that HEIC photo: " + r.stderr.decode(errors="replace").strip()[-200:])
        return dst.read_bytes()


def open_image(src: Path | bytes, target: tuple[int, int] | None = None) -> Image.Image:
    """Open, EXIF-orient and convert to RGB. With `target`, let the JPEG
    decoder downscale on the fly (a big speed-up on a Pi Zero). Reads HEIC too."""
    if isinstance(src, (bytes, bytearray)):
        if is_heif(bytes(src[:16])):
            src = _heif_to_jpeg(bytes(src))
    else:
        with open(src, "rb") as f:
            if is_heif(f.read(16)):
                src = _heif_to_jpeg(Path(src).read_bytes())
    img = Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src)
    if target and img.format == "JPEG":
        # draft() works on the stored (pre-EXIF-rotation) axes, so ask for
        # enough pixels on both axes.
        side = max(target)
        img.draft("RGB", (side, side))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (0, 0, 0))
            bg.paste(img, mask=img.getchannel("A"))
            img = bg
        else:
            img = img.convert("RGB")
    return img


def keep_exif(img: Image.Image) -> bytes:
    """The photo's EXIF details to carry over to a resized copy. The pixels are
    already turned upright, so the orientation tag is reset to 'normal'."""
    try:
        exif = img.getexif()
        if not exif:
            return b""
        exif[0x0112] = 1
        return exif.tobytes()
    except Exception:  # noqa: BLE001  (odd EXIF must never stop an import)
        return b""


def logical_size(panel: tuple[int, int], orientation: str) -> tuple[int, int]:
    w, h = panel
    if orientation == "portrait" and w > h:
        return h, w
    if orientation == "landscape" and h > w:
        return h, w
    return w, h


def _background(img: Image.Image, size: tuple[int, int], mode: str) -> Image.Image:
    if mode == "blur":
        small = ImageOps.fit(img, (max(1, size[0] // 16), max(1, size[1] // 16)), Image.BILINEAR)
        small = small.filter(ImageFilter.GaussianBlur(3))
        bg = small.resize(size, Image.BILINEAR)
        return Image.blend(bg, Image.new("RGB", size, (0, 0, 0)), 0.35)  # darken so the photo pops
    if mode.startswith("#") and len(mode) == 7:
        try:
            return Image.new("RGB", size, tuple(int(mode[i : i + 2], 16) for i in (1, 3, 5)))
        except ValueError:
            pass
    return Image.new("RGB", size, NAMED_COLORS.get(mode, (0, 0, 0)))


def place(img: Image.Image, size: tuple[int, int], scale_mode: str, background: str) -> Image.Image:
    """Scale one photo into a box of `size`."""
    if scale_mode == "cover":
        return ImageOps.fit(img, size, Image.LANCZOS, centering=(0.5, 0.45))
    canvas = _background(img, size, background)
    fitted = ImageOps.contain(img, size, Image.LANCZOS)
    canvas.paste(fitted, ((size[0] - fitted.width) // 2, (size[1] - fitted.height) // 2))
    return canvas


def compose(images: list[Image.Image], size: tuple[int, int], scale_mode: str, background: str) -> Image.Image:
    """Lay 1 or 2 photos onto a logical-size canvas. Two photos are stacked
    along the screen's long axis, each filling (cover) its half."""
    if len(images) == 1:
        return place(images[0], size, scale_mode, background)
    w, h = size
    canvas = Image.new("RGB", size, NAMED_COLORS.get(background, (0, 0, 0)))
    if h >= w:  # portrait screen: stack vertically
        tile = (w, (h - GAP) // 2)
        offsets = [(0, 0), (0, tile[1] + GAP)]
    else:  # landscape screen: side by side
        tile = ((w - GAP) // 2, h)
        offsets = [(0, 0), (tile[0] + GAP, 0)]
    for im, off in zip(images[:2], offsets):
        canvas.paste(ImageOps.fit(im, tile, Image.LANCZOS, centering=(0.5, 0.4)), off)
    return canvas


def is_mismatched(img: Image.Image, size: tuple[int, int], ratio: float = 1.15) -> bool:
    """True when a photo is clearly the 'wrong' orientation for the screen,
    i.e. a candidate for pairing in a collage."""
    img_landscape = img.width / img.height >= ratio
    img_portrait = img.height / img.width >= ratio
    screen_portrait = size[1] > size[0]
    return (screen_portrait and img_landscape) or (not screen_portrait and img_portrait)


def to_panel(img: Image.Image, panel: tuple[int, int], extra_rotation: int) -> Image.Image:
    """Rotate a logical (upright) frame onto the physical panel."""
    rot = extra_rotation % 360
    if (img.width > img.height) != (panel[0] > panel[1]) and img.width != img.height:
        rot = (rot + 90) % 360
    if rot:
        # PIL rotates counter-clockwise; we want clockwise degrees.
        img = img.rotate(-rot, expand=True)
    if img.size != panel:
        img = img.resize(panel, Image.BILINEAR)
    return img


def undo_native_layout(img: Image.Image, logical: tuple[int, int]) -> Image.Image:
    """The stock image server rotates portrait frames 90deg clockwise into the
    panel's native (landscape) layout. Bring such images back upright."""
    if (img.width > img.height) != (logical[0] > logical[1]) and img.width != img.height:
        return img.rotate(90, expand=True)  # 90deg counter-clockwise
    return img


def thumbnail_jpeg(img: Image.Image, max_side: int = 400, quality: int = 82) -> bytes:
    t = img.copy()
    t.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def store_copy(img: Image.Image, panel: tuple[int, int]) -> Image.Image:
    """Downscale an upload for storage: keep enough pixels to *cover* the
    screen in either orientation (short edge >= the panel's long edge)."""
    need = max(panel)
    short = min(img.size)
    if short <= need:
        return img
    scale = need / short
    return img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
