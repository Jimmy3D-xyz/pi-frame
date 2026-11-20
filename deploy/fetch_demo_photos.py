#!/usr/bin/env python3
"""Fetch the sample photos for the frame's demo (shop) mode.

Sources, both free of copyright restrictions:
  - The Metropolitan Museum of Art's Open Access collection: only objects the
    Met marks as public domain (isPublicDomain), released under CC0.
  - NASA's image library (images.nasa.gov): NASA imagery is generally not
    subject to copyright in the United States. Items credited to anyone other
    than NASA (or that mention copyright) are skipped.

    python3 deploy/fetch_demo_photos.py explore OUT_DIR
        search both, download ~30 candidates and make contact.jpg to choose from
    python3 deploy/fetch_demo_photos.py fetch
        download the chosen ones listed in deploy/demo_photos.json into
        photoframe/data/demo/, resized for the frame, with credits.json

Needs: python3-requests, python3-pil. Run on GitHub (see
.github/workflows/demo-photos.yml); the sites aren't reachable from everywhere.
"""

from __future__ import annotations

import io
import json
import re
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont, ImageOps

REPO = Path(__file__).resolve().parents[1]
CHOSEN = REPO / "deploy" / "demo_photos.json"
OUT = REPO / "photoframe" / "data" / "demo"
MET = "https://collectionapi.metmuseum.org/public/collection/v1"
NASA = "https://images-api.nasa.gov"
UA = {"User-Agent": "Pi-Frame demo photo fetcher (https://github.com/JimmySte/pi-frame)"}
LONG_EDGE = 2048
MET_QUERIES = ["landscape", "seascape", "mountains", "river", "garden", "flowers", "harbor", "snow", "sunset", "autumn"]
NASA_QUERIES = ["earth from space station", "aurora from space station", "milky way", "Saturn Cassini",
                "Grand Canyon from space", "Himalayas from space", "Bahamas from space", "nebula Webb"]


def get(url: str, **kw) -> requests.Response:
    for attempt in range(4):
        try:
            r = requests.get(url, headers=UA, timeout=60, **kw)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 404):
                r.raise_for_status()
        except requests.RequestException:
            if attempt == 3:
                raise
        time.sleep(2 * (attempt + 1))
    r.raise_for_status()
    return r


# ---------------------------------------------------------------- the Met
def met_object(oid: int) -> dict | None:
    o = get(f"{MET}/objects/{oid}").json()
    if not o.get("isPublicDomain") or not o.get("primaryImage"):
        return None
    return {"source": "met", "id": oid, "title": o.get("title") or "Untitled",
            "creator": o.get("artistDisplayName") or "Unknown artist", "date": o.get("objectDate") or "",
            "url": o["primaryImage"], "page": o.get("objectURL") or f"https://www.metmuseum.org/art/collection/search/{oid}",
            "license": "Public domain (The Met Open Access, CC0)", "kind": o.get("classification") or ""}


def met_candidates(per_query: int = 3) -> list[dict]:
    out, seen = [], set()
    for q in MET_QUERIES:
        ids = get(f"{MET}/search", params={"q": q, "hasImages": "true", "isHighlight": "true"}).json().get("objectIDs") or []
        n = 0
        for oid in ids[:40]:
            if oid in seen:
                continue
            seen.add(oid)
            o = met_object(oid)
            if o and o["kind"].lower().startswith("paint"):
                out.append(o)
                n += 1
                if n >= per_query:
                    break
    return out


# ---------------------------------------------------------------- NASA
def nasa_item(nasa_id: str) -> dict | None:
    r = get(f"{NASA}/search", params={"nasa_id": nasa_id, "media_type": "image"}).json()
    items = r.get("collection", {}).get("items", [])
    return _nasa(items[0]) if items else None


def _nasa(item: dict) -> dict | None:
    d = item["data"][0]
    text = " ".join(str(d.get(k, "")) for k in ("description", "secondary_creator", "photographer", "title")).lower()
    if "©" in text or "copyright" in text:
        return None
    sec = str(d.get("secondary_creator", "")).lower()
    if sec and "nasa" not in sec:
        return None  # credited to someone else
    files = get(item["href"]).json()
    pick = next((f for f in files if re.search(r"~large\.jpe?g$", f, re.I)), None) or \
        next((f for f in files if re.search(r"~orig\.jpe?g$", f, re.I)), None)
    if not pick:
        return None
    return {"source": "nasa", "id": d["nasa_id"], "title": d.get("title") or d["nasa_id"],
            "creator": "NASA" + (f" ({d['center']})" if d.get("center") else ""), "date": (d.get("date_created") or "")[:10],
            "url": pick.replace("http://", "https://"), "page": f"https://images.nasa.gov/details/{d['nasa_id']}",
            "license": "NASA image, not subject to copyright in the US", "kind": "photo"}


def nasa_candidates(per_query: int = 2) -> list[dict]:
    out = []
    for q in NASA_QUERIES:
        items = get(f"{NASA}/search", params={"q": q, "media_type": "image", "page_size": 20}).json()
        n = 0
        for item in items.get("collection", {}).get("items", []):
            try:
                c = _nasa(item)
            except requests.RequestException:
                c = None
            if c:
                out.append(c)
                n += 1
                if n >= per_query:
                    break
    return out


# ---------------------------------------------------------------- images
def load(url: str) -> Image.Image:
    im = Image.open(io.BytesIO(get(url).content))
    return ImageOps.exif_transpose(im).convert("RGB")


def contact_sheet(cands: list[dict], images: list[Image.Image], path: Path) -> None:
    cell, cols = 300, 6
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 40)), "white")
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
    for i, (c, im) in enumerate(zip(cands, images)):
        x, y = (i % cols) * cell, (i // cols) * (cell + 40)
        t = im.copy()
        t.thumbnail((cell - 8, cell - 8))
        sheet.paste(t, (x + (cell - t.width) // 2, y + 4 + (cell - 8 - t.height) // 2))
        d.text((x + 4, y + cell), f"{i + 1}. {c['source']} {c['id']}", fill="black", font=font)
        d.text((x + 4, y + cell + 17), c["title"][:40], fill="#555", font=font)
    sheet.save(path, quality=85)


def explore(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cands = met_candidates() + nasa_candidates()
    images, kept = [], []
    for c in cands:
        try:
            im = load(c["url"])
        except Exception as e:  # noqa: BLE001
            print(f"skip {c['source']} {c['id']}: {e}")
            continue
        c["size"] = list(im.size)
        images.append(im)
        kept.append(c)
        print(f"{len(kept):2d}. {c['source']:4s} {c['id']}  {im.width}x{im.height}  {c['title'][:60]} / {c['creator']}")
    contact_sheet(kept, images, out_dir / "contact.jpg")
    (out_dir / "candidates.json").write_text(json.dumps(kept, indent=1))


def fetch() -> None:
    chosen = json.loads(CHOSEN.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.jpg"):
        old.unlink()
    credits = []
    for n, pick in enumerate(chosen, 1):
        c = met_object(int(pick["id"])) if pick["source"] == "met" else nasa_item(str(pick["id"]))
        if not c:
            sys.exit(f"{pick} is no longer available as public domain: choose another")
        im = load(c["url"])
        if pick.get("crop"):  # [left, top, right, bottom] as fractions, to trim borders or frames
            l, t, r, b = pick["crop"]
            im = im.crop((int(l * im.width), int(t * im.height), int(r * im.width), int(b * im.height)))
        im.thumbnail((LONG_EDGE, LONG_EDGE), Image.LANCZOS)
        slug = re.sub(r"[^a-z0-9]+", "-", c["title"].lower()).strip("-")[:40] or "photo"
        name = f"{n:02d}-{slug}.jpg"
        im.save(OUT / name, quality=86, optimize=True, progressive=True)
        credits.append({"file": name, "title": pick.get("title") or c["title"], "creator": c["creator"],
                        "date": c["date"], "source": c["page"], "license": c["license"],
                        "size": list(im.size)})
        print(f"{name}: {im.width}x{im.height}, {(OUT / name).stat().st_size // 1024} KB")
    (OUT / "credits.json").write_text(json.dumps(credits, indent=1, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "explore":
        explore(Path(sys.argv[2]))
    elif len(sys.argv) == 2 and sys.argv[1] == "fetch":
        fetch()
    else:
        print(__doc__)
        sys.exit(2)
