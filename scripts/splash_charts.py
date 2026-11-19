#!/usr/bin/env python3
"""Render the 2026-11-19 engine-switch charts from docs/data/splash_2026-11-19.json (measured numbers only).

Outputs docs/img/splash_speed_before_after.png and docs/img/splash_context_sweep.png (1600x900).
Needs Pillow. Fonts fall back to Pillow's default when DejaVu is not installed."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "docs" / "data" / "splash_2026-11-19.json"
OUT = ROOT / "docs" / "img"
W, H = 1600, 900
BG = (11, 15, 23); PANEL = (15, 21, 32); EDGE = (45, 58, 78); GRID = (30, 40, 56)
BEFORE = (148, 163, 184); AFTER = (52, 211, 153); LINE = (56, 189, 248); TX = (240, 246, 252); MU = (148, 163, 184)
AM = (251, 191, 36)
_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "/System/Library/Fonts/Supplemental"]


def font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        p = Path(d) / name
        if p.exists():
            return ImageFont.truetype(str(p), size)
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def canvas(title: str, sub: str):
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.text((60, 40), title, font=font(40, True), fill=TX)
    d.text((60, 96), sub, font=font(21), fill=MU)
    return im, d


def footer(d, text: str):
    d.text((60, H - 50), text, font=font(18), fill=MU)


def bars(d, box, groups, vmax, unit, lower_better=False):
    """groups: [(label, before, after)]; draws paired bars inside box=(x0,y0,x1,y1)."""
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, 14, fill=PANEL, outline=EDGE)
    px0, py0, px1, py1 = x0 + 40, y0 + 70, x1 - 30, y1 - 70
    for i in range(5):
        y = py1 - (py1 - py0) * i / 4
        d.line((px0, y, px1, y), fill=GRID)
        d.text((x0 + 8, y - 10), f"{vmax * i / 4:.0f}", font=font(15), fill=MU)
    gw = (px1 - px0) / len(groups)
    bw = gw * 0.3
    for gi, (lab, b, a) in enumerate(groups):
        cx = px0 + gw * (gi + 0.5)
        for j, (v, col) in enumerate(((b, BEFORE), (a, AFTER))):
            if v is None:
                continue
            bx = cx - bw - 6 if j == 0 else cx + 6
            top = py1 - (py1 - py0) * min(v, vmax) / vmax
            d.rounded_rectangle((bx, top, bx + bw, py1), 6, fill=col)
            s = f"{v:g}"
            tw = d.textlength(s, font=font(22, True))
            d.text((bx + bw / 2 - tw / 2, top - 32), s, font=font(22, True), fill=TX)
        if b and a:
            ratio = (b / a) if lower_better else (a / b)
            tag = f"{ratio:.1f}x {'faster' if lower_better else 'higher'}"
            tw = d.textlength(tag, font=font(18, True))
            d.text((cx - tw / 2, py1 + 34), tag, font=font(18, True), fill=AM)
        tw = d.textlength(lab, font=font(19))
        d.text((cx - tw / 2, py1 + 8), lab, font=font(19), fill=TX)
    d.text((x0 + 40, y0 + 22), unit, font=font(20, True), fill=TX)


def legend(d, x, y, before: str, after: str):
    for lab, col in ((before, BEFORE), (after, AFTER)):
        d.rounded_rectangle((x, y, x + 22, y + 22), 4, fill=col)
        d.text((x + 32, y), lab, font=font(19), fill=TX)
        x += 32 + d.textlength(lab, font=font(19)) + 40


def speed_chart(m: dict) -> Path:
    lt, su, vt = m["live_traffic_same_day"], m["suite_16_prompts"], m["vision_ttft_s"]
    im, d = canvas("Same 24 GB M4 mini, new engine: decode up, waits down",
                   f"before: {m['before']['model']} {m['before']['quant']} on {m['before']['engine']}   "
                   f"after: {m['after']['model']} {m['after']['quant']} on {m['after']['engine']}")
    bars(d, (60, 180, 800, 810), [
        ("real traffic, median", lt["before"]["median_decode"], lt["after"]["median_decode"]),
        ("16-prompt suite, mean", su["before_decode_mean"], su["after_decode_mean"]),
    ], 100, "decode tok/s (higher is better)")
    bars(d, (840, 180, 1540, 810), list(zip(vt["labels"], vt["before"], vt["after"])), 8,
         "time to first token on images, s (lower is better)", lower_better=True)
    legend(d, 60, 138, "before", "after")
    footer(d, f"measured {m['measured_on']}. real traffic = answers of 100+ tokens the same day "
              f"({lt['before']['answers_100plus']} before, {lt['after']['answers_100plus']} after); medians, not a controlled benchmark.")
    p = OUT / "splash_speed_before_after.png"
    im.save(p, optimize=True)
    return p


def context_chart(m: dict) -> Path:
    sw, bf = m["context_sweep_after"], m["context_sweep_before"][0]
    im, d = canvas("Context window: 16K -> 64K on the same 24 GB machine",
                   "one long document per run, answer = a code word hidden inside it. bars = decode tok/s, line = time to first token")
    x0, y0, x1, y1 = 60, 180, 1540, 790
    d.rounded_rectangle((x0, y0, x1, y1), 14, fill=PANEL, outline=EDGE)
    px0, py0, px1, py1 = x0 + 90, y0 + 70, x1 - 110, y1 - 90
    vmax, tmax = 60, 250
    for i in range(5):
        y = py1 - (py1 - py0) * i / 4
        d.line((px0, y, px1, y), fill=GRID)
        d.text((x0 + 18, y - 10), f"{vmax * i / 4:.0f}", font=font(15), fill=MU)
        d.text((px1 + 16, y - 10), f"{tmax * i / 4:.0f} s", font=font(15), fill=LINE)
    d.text((x0 + 18, y0 + 22), "decode tok/s", font=font(20, True), fill=TX)
    tw = d.textlength("time to first token", font=font(20, True))
    d.text((x1 - 30 - tw, y0 + 22), "time to first token", font=font(20, True), fill=LINE)
    cols = [("12.5K", bf, sw[0]), ("25K", None, sw[1]), ("51K", None, sw[2]), ("63K", None, sw[3])]
    gw = (px1 - px0) / len(cols)
    bw = gw * 0.28
    pts = []
    for gi, (lab, b, a) in enumerate(cols):
        cx = px0 + gw * (gi + 0.5)
        for j, (row, col) in enumerate(((b, BEFORE), (a, AFTER))):
            bx = cx - bw - 6 if j == 0 else cx + 6
            if row is None:
                if j == 0:
                    d.text((bx + 4, py1 - 60), "over the", font=font(16), fill=MU)
                    d.text((bx + 4, py1 - 38), "16K cap", font=font(16), fill=MU)
                continue
            top = py1 - (py1 - py0) * row["decode"] / vmax
            d.rounded_rectangle((bx, top, bx + bw, py1), 6, fill=col)
            s = f"{row['decode']:g}"
            t = d.textlength(s, font=font(21, True))
            d.text((bx + bw / 2 - t / 2, top - 30), s, font=font(21, True), fill=TX)
        pts.append((cx + 6 + bw / 2, py1 - (py1 - py0) * a["ttft_s"] / tmax, a["ttft_s"]))
        t = d.textlength(lab + " prompt", font=font(19))
        d.text((cx - t / 2, py1 + 10), lab + " prompt", font=font(19), fill=TX)
    d.line([(x, y) for x, y, _ in pts], fill=LINE, width=4)
    for x, y, v in pts:
        d.ellipse((x - 8, y - 8, x + 8, y + 8), fill=LINE)
        lab = f"{v:.0f} s"
        tw = d.textlength(lab, font=font(19, True))
        bx, by = x - tw - 22, y - 38
        d.rounded_rectangle((bx - 8, by - 4, bx + tw + 8, by + 28), 6, fill=BG, outline=LINE)
        d.text((bx, by), lab, font=font(19, True), fill=LINE)
    legend(d, 60, 138, "before (16K cap)", "after (64K cap)")
    d.text((60, H - 84), f"measured {m['measured_on']}. the code word was found at every size after the switch; before, anything over 16K was rejected.",
           font=font(18), fill=MU)
    footer(d, f"an {m['over_cap_failure_prompt_tokens'] // 1000}K prompt ran the engine out of memory, so the cap is 64K and a trimmer fits bigger requests under it.")
    p = OUT / "splash_context_sweep.png"
    im.save(p, optimize=True)
    return p


def main() -> int:
    m = json.loads(DATA.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    for p in (speed_chart(m), context_chart(m)):
        print(p.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
