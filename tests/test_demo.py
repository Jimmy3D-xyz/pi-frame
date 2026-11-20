"""Demo (shop) mode: the samples, their credits, and the slides."""
from PIL import Image

from photoframe import demo


def test_every_sample_is_credited_and_public_domain():
    s = demo.samples()
    assert len(s) >= 12
    credits = {c.path.name for c in s}
    files = {p.name for p in demo.DEMO_DIR.glob("*.jpg")}
    assert files == credits  # nothing uncredited ships
    import json

    for c in json.loads((demo.DEMO_DIR / "credits.json").read_text()):
        assert c["title"] and c["creator"] and c["source"].startswith("https://")
        assert "public domain" in c["license"].lower() or "not subject to copyright" in c["license"].lower()
    assert any(x.size[1] > x.size[0] for x in s)  # some tall ones, for collages and portrait frames


def test_slides_render_landscape_and_portrait():
    backdrop = Image.open(demo.samples()[0].path)
    for size in ((1920, 1080), (1080, 1920), (800, 480)):
        for title, text, qr in demo.CARDS:
            img = demo.info_card(size, title, text, "http://piframe.local/" if qr else None, backdrop)
            assert img.size == size
        cap = demo.draw_caption(Image.new("RGB", size, (90, 120, 150)), demo.samples()[0].caption, "bottom-left")
        inside = (size[0] - int(min(size) * 0.08), size[1] - int(min(size) * 0.06))
        assert cap.size == size and cap.getpixel(inside) != (90, 120, 150)  # the tag, bottom-right
        assert cap.getpixel((20, size[1] - 20)) == (90, 120, 150)  # and not where the clock goes


def test_sequence_mixes_photos_collages_and_slides():
    d = demo.Demo()
    kinds = [d.next()[0] for _ in range(20)]
    assert {"photo", "collage", "card", "swap"} <= set(kinds)
    collages = []
    for _ in range(60):
        k, item, _secs, _how = d.next()
        if k in ("collage", "swap"):
            collages.append(list(d.collage))
    assert all(len({s.path for s in c}) == len(c) for c in collages)  # no photo twice in one collage


def test_sequence_shows_off_every_transition():
    d = demo.Demo()
    steps = [d.next() for _ in range(len(demo.Demo.SCRIPT))]
    assert {"3d", "zoom-out", "slide", "zoom-in", "fade"} <= {how for *_, how in steps}
    for i, (kind, item, _secs, how) in enumerate(steps):
        if how == "zoom-out":  # the photo before is in the collage it shrinks into
            assert steps[i - 1][0] == "photo" and steps[i - 1][1] in item
        if how == "zoom-in":  # the photo grows out of the collage before it
            assert any(s[0] in ("collage", "swap") for s in steps[i - 1:i])
        if how == "3d":
            assert steps[i - 1][0] == "photo" and item is not steps[i - 1][1]


def test_peek_is_what_comes_next():
    d = demo.Demo()
    for _ in range(12):
        assert d.peek() == d.peek() == d.next()
