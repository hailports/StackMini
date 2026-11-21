"""Local-only doc -> text PDF (pandoc + weasyprint). No network."""
import html, re, subprocess, sys
from pathlib import Path

CSS = "body{font-family:Helvetica,Arial,sans-serif;font-size:10pt;line-height:1.35}pre,code{font-size:8.5pt;white-space:pre-wrap}table{border-collapse:collapse}td,th{border:1px solid #999;padding:2px 4px;font-size:8.5pt}img{display:none}"

def fix_breaks(lines):
    out = []
    for ln in lines:
        if out and out[-1] and ln and out[-1][-1].isalpha() and ln[0].islower():
            out[-1] += ln  # rejoin chunk-split word
        else:
            out.append(ln)
    return out

def text_to_html(raw: str, title: str) -> str:
    lines = fix_breaks([l.rstrip() for l in raw.splitlines()])
    parts = [f"<h1>{html.escape(title)}</h1>"]
    for ln in lines:
        m = re.match(r"^(\d+(?:\.\d+)*)\.?\s+([A-Z].{2,90})$", ln)
        if m and "\t" not in ln and not re.search(r"\b20[0-9][0-9]\b", ln) and not ln.endswith((".", ";", ":")):
            depth = min(m.group(1).count(".") + 2, 4)
            parts.append(f"<h{depth}>{html.escape(ln)}</h{depth}>")
        elif ln.strip() in ("Appendix", "Glossary", "References"):
            parts.append(f"<h2>{html.escape(ln)}</h2>")
        elif re.match(r"^(git|sf|gh|cd|name:|on:|jobs:|#|\s{2,})", ln):
            parts.append(f"<pre>{html.escape(ln)}</pre>")
        elif ln.strip():
            parts.append(f"<p>{html.escape(ln)}</p>")
    return "<html><head><meta charset='utf-8'><title>%s</title></head><body>%s</body></html>" % (html.escape(title), "\n".join(parts))

def main(src, dst, title=None):
    src, dst = Path(src), Path(dst)
    css = dst.with_suffix(".css"); css.write_text(CSS)
    if src.suffix == ".docx":
        h = dst.with_suffix(".html")
        subprocess.run(["pandoc", str(src), "-s", "-o", str(h), "--metadata", f"title={title or src.stem}"], check=True)
    else:
        h = dst.with_suffix(".html"); h.write_text(text_to_html(src.read_text(), title or src.stem))
    subprocess.run(["weasyprint", "-s", str(css), str(h), str(dst)], check=True, capture_output=True)
    print(dst, dst.stat().st_size)

if __name__ == "__main__":
    main(*sys.argv[1:])
