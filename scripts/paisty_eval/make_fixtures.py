#!/usr/bin/env python3
"""Build the synthetic files the paisty eval cases attach. No real names or data.

Writes xlsx, csv, docx, pdf and png fixtures with the standard library plus Pillow
(Pillow only for the images and the screenshot renders).
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "tests" / "fixtures" / "paisty_eval"


def col(n: int) -> str:
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path: Path, sheets: dict[str, list[list]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        names = list(sheets)
        zf.writestr("[Content_Types].xml",
                    '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                    '<Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                    + "".join(f'<Override PartName="/xl/worksheets/sheet{i+1}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(len(names)))
                    + "</Types>")
        zf.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        zf.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                    + "".join(f'<sheet name="{escape(n)}" sheetId="{i+1}" r:id="rId{i+1}"/>' for i, n in enumerate(names))
                    + "</sheets></workbook>")
        zf.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    + "".join(f'<Relationship Id="rId{i+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i+1}.xml"/>' for i in range(len(names)))
                    + "</Relationships>")
        for i, name in enumerate(names):
            rows = []
            for r, row in enumerate(sheets[name]):
                cells = []
                for c, val in enumerate(row):
                    ref = f"{col(c)}{r+1}"
                    if val is None:
                        continue
                    if isinstance(val, (int, float)):
                        cells.append(f'<c r="{ref}"><v>{val}</v></c>')
                    else:
                        cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(str(val))}</t></is></c>')
                rows.append(f'<row r="{r+1}">' + "".join(cells) + "</row>")
            zf.writestr(f"xl/worksheets/sheet{i+1}.xml",
                        '<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                        + "".join(rows) + "</sheetData></worksheet>")


def write_docx(path: Path, paras: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>" for p in paras)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
                    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        zf.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        zf.writestr("word/document.xml", '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                    + body + "</w:body></w:document>")


def write_pdf(path: Path, lines: list[str]) -> None:
    text = "BT /F1 11 Tf 50 760 Td 14 TL " + " ".join(
        "(" + l.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for l in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(text)} >>\nstream\n{text}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objs):
        offsets.append(len(out))
        out += f"{i+1} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs)+1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(out)


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    for name in (["DejaVuSans-Bold.ttf"] if bold else ["DejaVuSans.ttf"]) + ["Arial.ttf", "Helvetica.ttc"]:
        for base in ("/usr/share/fonts/truetype/dejavu/", "/Library/Fonts/", "/System/Library/Fonts/"):
            try:
                return ImageFont.truetype(base + name, size)
            except OSError:
                continue
    return ImageFont.load_default()


def write_table_png(path: Path, title: str, rows: list[list[str]]) -> None:
    from PIL import Image, ImageDraw
    w, h = 760, 60 + 34 * len(rows)
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.text((16, 14), title, fill="black", font=_font(20, True))
    for r, row in enumerate(rows):
        y = 52 + r * 34
        d.line((10, y - 4, w - 10, y - 4), fill="#bbb")
        for c, cell in enumerate(row):
            d.text((20 + c * 240, y + 4), cell, fill="black", font=_font(18, r == 0))
    img.save(path)


def render_thread(path: Path, case: dict) -> None:
    """An email-client-like screenshot of a case thread: newest message on top, quoted chain below."""
    from PIL import Image, ImageDraw
    import textwrap
    w = 1100
    lines: list[tuple[str, str]] = []
    msgs = case["thread"]
    top = msgs[0]
    lines.append(("subj", case["subject"]))
    lines.append(("head", f"{top['from']}"))
    lines.append(("meta", f"To: {', '.join(top.get('to', []))}" + (f"    Cc: {', '.join(top.get('cc', []))}" if top.get("cc") else "")))
    lines.append(("meta", top["date"]))
    for a in case.get("attachments_shown", []):
        lines.append(("chip", "[attachment]  " + a))
    lines.append(("gap", ""))
    for para in top["body"].split("\n"):
        for chunk in textwrap.wrap(para, 105) or [""]:
            lines.append(("body", chunk))
    if case.get("inline_image"):
        lines.append(("gap", ""))
        for chunk in case["inline_image"]:
            lines.append(("img", chunk))
    for m in msgs[1:]:
        lines.append(("gap", ""))
        lines.append(("rule", ""))
        lines.append(("meta", f"From: {m['from']}"))
        lines.append(("meta", f"Sent: {m['date']}"))
        lines.append(("meta", "To: " + ", ".join(m.get("to", []))))
        if m.get("cc"):
            lines.append(("meta", "Cc: " + ", ".join(m["cc"])))
        lines.append(("meta", "Subject: " + case["subject"]))
        lines.append(("gap", ""))
        for para in m["body"].split("\n"):
            for chunk in textwrap.wrap(para, 105) or [""]:
                lines.append(("body", chunk))
    h = 40 + 26 * len(lines)
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    y = 20
    fonts = {"subj": _font(24, True), "head": _font(19, True), "meta": _font(15), "chip": _font(15, True),
             "body": _font(17), "img": _font(17, True), "gap": _font(10), "rule": _font(10)}
    for kind, text in lines:
        if kind == "rule":
            d.line((20, y + 10, w - 20, y + 10), fill="#999")
        elif kind == "img":
            d.rectangle((40, y - 2, w - 200, y + 24), fill="#fff6d5", outline="#d9b84a")
            d.text((50, y), text, fill="#222", font=fonts[kind])
        elif kind == "chip":
            d.rectangle((20, y - 2, 20 + 12 * len(text), y + 22), outline="#4a7bd9")
            d.text((28, y), text, fill="#123a8a", font=fonts[kind])
        else:
            d.text((24, y), text, fill="#555" if kind == "meta" else "black", font=fonts[kind])
        y += 26
    img.save(path)


def main() -> int:
    cases = json.loads((HERE / "cases.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    for case in cases:
        for spec in case.get("files", []):
            path = OUT / spec["name"]
            kind = spec["kind"]
            if kind == "xlsx":
                write_xlsx(path, spec["sheets"])
            elif kind == "csv":
                path.write_text("\n".join(",".join(str(v) for v in row) for row in spec["rows"]) + "\n", encoding="utf-8")
            elif kind == "docx":
                write_docx(path, spec["paras"])
            elif kind == "pdf":
                write_pdf(path, spec["lines"])
            elif kind == "png":
                write_table_png(path, spec["title"], spec["rows"])
            elif kind == "text":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(spec["text"], encoding="utf-8")
        if case.get("screenshot"):
            render_thread(OUT / f"{case['id']}.png", case)
    print(f"wrote fixtures to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
