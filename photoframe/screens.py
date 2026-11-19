"""Full-screen information cards with QR codes, drawn with Pillow.

Equivalent of the firmware's splash_screen.c: a "join the frame's Wi-Fi"
card while in setup mode, and an "add photos" card when there's nothing to
show. Cards are drawn at the logical (upright) screen size.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    import qrcode
    from qrcode.constants import ERROR_CORRECT_M
except ImportError:  # apt: python3-qrcode
    qrcode = None

BG = (27, 31, 30)
BG_TOP = (36, 46, 43)      # deep sage at the top...
BG_BOTTOM = (24, 22, 20)   # ...into walnut at the bottom
INK = (244, 241, 234)
SUN = (232, 176, 74)
MAT = (244, 241, 234)
SOFT = (170, 182, 178)
ACCENT = (140, 180, 166)

_FONT_DIRS = [
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/dejavu"),
    Path("/Library/Fonts"),
    Path("C:/Windows/Fonts"),
]


@lru_cache(maxsize=32)
def _font(kind: str, size: int) -> ImageFont.ImageFont:
    names = {
        "serif": ["DejaVuSerif-Bold.ttf", "DejaVuSerif.ttf", "Georgia Bold.ttf", "georgiab.ttf"],
        "serif-italic": ["DejaVuSerif-BoldItalic.ttf", "DejaVuSerif-Bold.ttf", "Georgia Bold Italic.ttf", "georgiaz.ttf"],
        "sans": ["DejaVuSans.ttf", "Arial.ttf", "arial.ttf"],
        "mono": ["DejaVuSansMono.ttf", "Courier New.ttf", "cour.ttf"],
    }[kind]
    for d in _FONT_DIRS:
        for n in names:
            p = d / n
            if p.exists():
                return ImageFont.truetype(str(p), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def wifi_qr_payload(ssid: str, password: str) -> str:
    def esc(s: str) -> str:
        return "".join("\\" + c if c in '\\;,:"' else c for c in s)

    if password:
        return f"WIFI:T:WPA;S:{esc(ssid)};P:{esc(password)};;"
    return f"WIFI:T:nopass;S:{esc(ssid)};;"


def qr_image(data: str, size: int) -> Image.Image:
    """A crisp QR code of about `size` px, dark on white with a quiet zone."""
    if qrcode is None:
        img = Image.new("RGB", (size, size), (255, 255, 255))
        d = ImageDraw.Draw(img)
        d.multiline_text((12, 12), "Install\npython3-qrcode", fill=(0, 0, 0), font=_font("sans", size // 12))
        return img
    q = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=2, box_size=1)
    q.add_data(data)
    q.make(fit=True)
    modules = q.make_image(fill_color="black", back_color="white").convert("RGB")
    scale = max(1, size // modules.width)
    return modules.resize((modules.width * scale, modules.height * scale), Image.NEAREST)


@dataclass
class QRBlock:
    data: str
    heading: str
    lines: list[str]


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int) -> list[str]:
    words, out, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= width or not cur:
            cur = trial
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def _background(size: tuple[int, int]) -> Image.Image:
    w, h = size
    grad = Image.linear_gradient("L").resize((1, 256))  # 0 at top .. 255 at bottom
    top, bot = BG_TOP, BG_BOTTOM
    col = Image.merge("RGB", [grad.point(lambda v, a=top[i], b=bot[i]: int(a + (b - a) * v / 255)) for i in range(3)])
    return col.resize((w, h), Image.BILINEAR)


def draw_wordmark(img: Image.Image, x: int, y: int, height: int, with_mark: bool = True, measure: bool = False) -> int:
    """Logo mark plus 'Pi Frame' wordmark; returns the width used. With
    measure=True nothing is drawn (to centre it first)."""
    from . import brand

    d = ImageDraw.Draw(img)
    fs = int(height * 0.62)
    pi_f, fr_f = _font("serif-italic", fs), _font("serif", fs)
    tx = x + (height + int(height * 0.3) if with_mark else 0)
    ty = y + (height - fs) // 2 - int(fs * 0.12)
    pi_w = d.textlength("Pi", font=pi_f)
    gap = fs * 0.28
    end = tx + pi_w + gap + d.textlength("Frame", font=fr_f)
    if not measure:
        if with_mark:
            mark = brand.mark(height)
            img.paste(mark, (x, y), mark)
        d.text((tx, ty), "Pi", font=pi_f, fill=SUN)
        d.text((tx + pi_w + gap, ty), "Frame", font=fr_f, fill=INK)
    return int(end - x)


def _matted(qr: Image.Image, pad: int) -> Image.Image:
    """A QR code on a cream mat with a soft drop shadow, like a matted print."""
    w, h = qr.width + 2 * pad, qr.height + 2 * pad
    sh = max(4, pad // 2)
    out = Image.new("RGBA", (w + 2 * sh, h + 2 * sh), (0, 0, 0, 0))
    shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([sh, sh + sh // 2, sh + w, sh + h + sh // 2], radius=pad // 2,
                                             fill=(0, 0, 0, 120))
    from PIL import ImageFilter
    out = Image.alpha_composite(out, shadow.filter(ImageFilter.GaussianBlur(sh * 0.8)))
    mat = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(mat).rounded_rectangle([0, 0, w - 1, h - 1], radius=pad // 2, fill=MAT + (255,))
    mat.paste(qr.convert("RGBA"), (pad, pad))
    out.paste(mat, (sh, sh), mat)
    return out


def _hero(img: Image.Image, cx: int, cy: int, side: int) -> None:
    """Large logo with a soft glow, for otherwise empty space.

    The glow and shadow are soft, so they're blurred at a quarter of the size
    and scaled up: it looks the same and is many times faster (it matters on a
    Pi, where a full-size blur took seconds)."""
    from PIL import ImageFilter
    from . import brand

    k = 4
    small = (max(1, img.width // k), max(1, img.height // k))
    layer = Image.new("RGBA", small, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    r = side * 0.75 / k
    d.ellipse([cx / k - r, cy / k - r, cx / k + r, cy / k + r], fill=(140, 180, 166, 46))
    layer = layer.filter(ImageFilter.GaussianBlur(side * 0.18 / k))
    shadow = Image.new("RGBA", small, (0, 0, 0, 0))
    h = side / 2 / k
    off = side / 24 / k
    ImageDraw.Draw(shadow).rounded_rectangle([cx / k - h, cy / k - h + off, cx / k + h, cy / k + h + off],
                                             radius=side / 6 / k, fill=(0, 0, 0, 110))
    layer = Image.alpha_composite(layer, shadow.filter(ImageFilter.GaussianBlur(max(1, side * 0.05 / k))))
    base = Image.alpha_composite(img.convert("RGBA"), layer.resize(img.size, Image.BILINEAR))
    mark = brand.mark(side, "full")
    base.paste(mark, (cx - side // 2, cy - side // 2), mark)
    img.paste(base.convert("RGB"))


def card(size: tuple[int, int], title: str, subtitle: str, blocks: list[QRBlock], footer: str = "",
         hero: bool = False) -> Image.Image:
    """Brand row, title, then one or two QR blocks side by side (landscape) or
    stacked (portrait), each a matted QR with a heading and a few lines."""
    w, h = size
    img = _background(size)
    d = ImageDraw.Draw(img)
    unit = min(w, h)
    margin = int(unit * 0.07)
    title_f = _font("serif", int(unit * 0.066))
    sub_f = _font("sans", int(unit * 0.032))
    head_f = _font("sans", int(unit * 0.035))
    body_f = _font("sans", int(unit * 0.029))
    mono_f = _font("mono", int(unit * 0.029))

    draw_wordmark(img, margin, int(margin * 0.75), int(unit * 0.075))
    d = ImageDraw.Draw(img)
    y = int(margin * 0.75 + unit * 0.075 + unit * 0.05)
    for line in _wrap(d, title, title_f, w - 2 * margin):
        d.text((margin, y), line, font=title_f, fill=INK)
        y += int(title_f.size * 1.2)
    y += int(unit * 0.008)
    for line in _wrap(d, subtitle, sub_f, w - 2 * margin):
        d.text((margin, y), line, font=sub_f, fill=SOFT)
        y += int(sub_f.size * 1.4)
    y += int(unit * 0.04)

    portrait = h > w
    n = max(1, len(blocks))
    footer_h = int(body_f.size * 2.5) if footer else 0
    gap = int(unit * 0.06)
    hero_landscape = hero and not portrait and n == 1
    if portrait:
        cell_w, cell_h = w - 2 * margin, (h - y - margin - footer_h) // n
    else:
        cell_w, cell_h = (w - 2 * margin - gap * (n - 1)) // n, h - y - margin - footer_h
        if hero_landscape:
            cell_w = int(w * 0.52) - margin

    pad = max(6, int(unit * 0.018))
    # the same QR size for every block, so side-by-side codes match
    qr_side = int(unit * (0.4 if n == 1 else 0.36))
    for b in blocks:
        text_h = int(head_f.size * 1.5) + (len(b.lines) + 1) * int(body_f.size * 1.45) + int(unit * 0.03)
        qr_side = min(qr_side, cell_h - text_h - 2 * pad, cell_w - 2 * pad)
    qr_side = max(80, qr_side)
    bottom = y
    for i, b in enumerate(blocks):
        if portrait:
            cx, cy = margin, y + i * cell_h
        else:
            cx, cy = margin + i * (cell_w + gap), y
        tile = _matted(qr_image(b.data, qr_side), pad)
        img.paste(tile, (cx - max(4, pad // 2), cy - max(4, pad // 2)), tile)
        tx, ty, tw = cx, cy + tile.height - max(4, pad // 2) + int(unit * 0.025), cell_w
        for line in _wrap(d, b.heading, head_f, tw):
            d.text((tx, ty), line, font=head_f, fill=ACCENT)
            ty += int(head_f.size * 1.4)
        ty += int(unit * 0.008)
        for line in b.lines:
            font = mono_f if line.startswith(("http://", "https://")) or line.startswith("  ") else body_f
            for wl in _wrap(d, line.strip(), font, tw):
                d.text((tx, ty), wl, font=font, fill=INK)
                ty += int(body_f.size * 1.45)
        bottom = max(bottom, ty)

    if hero_landscape:
        side = int(min(w * 0.3, (h - 2 * margin) * 0.62))
        _hero(img, int(w * 0.52 + (w * 0.48 - margin) / 2), int(h * 0.52), side)
        d = ImageDraw.Draw(img)
    elif hero and portrait:
        free = h - margin - footer_h - bottom
        side = int(min(w * 0.46, free * 0.7))
        if side > unit * 0.2:
            _hero(img, w // 2, bottom + free // 2, side)
            d = ImageDraw.Draw(img)

    if footer:
        fy = h - margin - int(body_f.size * 1.2)
        d.text((margin, fy), footer, font=body_f, fill=SOFT)
    return img


def empty_card(size: tuple[int, int], urls: list[str], reason: str = "There are no photos on this frame yet.") -> Image.Image:
    lines = ["Scan with a phone on the same Wi-Fi, or open:"] + urls
    return card(
        size,
        "Ready for your photos",
        reason,
        [QRBlock(urls[0], "Add photos from your phone", lines)],
        hero=True,
    )


def wifi_setup_card(size: tuple[int, int], ssid: str, password: str, setup_url: str, reason: str) -> Image.Image:
    return card(
        size,
        "Let's get this frame online",
        reason,
        [
            QRBlock(
                wifi_qr_payload(ssid, password),
                "1. Join the frame's Wi-Fi",
                ["Scan with your phone's camera, or join manually:", f"  Network: {ssid}", f"  Password: {password}"],
            ),
            QRBlock(
                setup_url,
                "2. Choose your home Wi-Fi",
                ["A setup page should open by itself. If not, scan this or open:", setup_url],
            ),
        ],
        footer="The frame keeps trying your saved Wi-Fi every few minutes.",
    )


def welcome_card(size: tuple[int, int], name: str, urls: list[str], footer: str) -> Image.Image:
    return card(
        size,
        f"Welcome to {name}",
        "Your photos will appear in a moment.",
        [
            QRBlock(
                urls[0],
                "Manage this frame from your phone",
                ["Add photos and change settings. Scan with a phone on the same Wi-Fi, or open:"] + urls,
            )
        ],
        footer=footer,
        hero=True,
    )


UPDATE_STEPS = ("Unpack", "Back up", "Install", "Check")


def update_card(size: tuple[int, int], title: str, subtitle: str, step: int | None = None,
                note: str = "Please don't unplug the frame.", note_colour=None) -> Image.Image:
    """Software update screen: the logo, a title, and (while updating) a
    progress bar through the steps. step: 0 = starting, 1-4 = UPDATE_STEPS,
    None = no bar (a result screen)."""
    w, h = size
    img = _background(size)
    unit = min(w, h)
    side = int(unit * 0.22)
    cy = int(h * 0.3)
    _hero(img, w // 2, cy, side)
    d = ImageDraw.Draw(img)
    title_f, sub_f = _font("serif", int(unit * 0.06)), _font("sans", int(unit * 0.03))
    small_f = _font("sans", int(unit * 0.024))
    y = int(cy + side * 0.5 + unit * 0.07)
    for text, f, col, gap in ((title, title_f, INK, 1.35), (subtitle, sub_f, SOFT, 1.5)):
        for line in _wrap(d, text, f, int(w * 0.84)):
            tw = d.textlength(line, font=f)
            d.text(((w - tw) / 2, y), line, font=f, fill=col)
            y += int(f.size * gap)
    if step is not None:
        y += int(unit * 0.03)
        bar_w = int(min(w * 0.7, unit * 1.1))
        x0 = (w - bar_w) // 2
        n = len(UPDATE_STEPS)
        gap = int(unit * 0.012)
        seg = (bar_w - gap * (n - 1)) / n
        bh = max(6, int(unit * 0.014))
        for i, label in enumerate(UPDATE_STEPS):
            sx = x0 + i * (seg + gap)
            done, active = i + 1 < step, i + 1 == step
            fill = ACCENT if done else (SUN if active else (70, 78, 76))
            d.rounded_rectangle([sx, y, sx + seg, y + bh], radius=bh // 2, fill=fill)
            lw = d.textlength(label, font=small_f)
            d.text((sx + (seg - lw) / 2, y + bh + int(unit * 0.012)), label, font=small_f,
                   fill=INK if active else (SOFT if done else (130, 136, 133)))
        y += bh + int(unit * 0.07)
    if note:
        tw = d.textlength(note, font=sub_f)
        d.text(((w - tw) / 2, max(y, int(h * 0.8))), note, font=sub_f, fill=note_colour or SUN)
    return img


def message_card(size: tuple[int, int], title: str, subtitle: str) -> Image.Image:
    return card(size, title, subtitle, [])
