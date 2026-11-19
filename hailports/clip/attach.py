"""Find and read the files a message talks about. Read-only. Nothing leaves this Mac.

Spreadsheets, CSV, Word, PowerPoint, PDF, text, and images. No third-party packages:
xlsx/docx/pptx are zip+XML, PDF and images use macOS PDFKit and Vision through
osascript when present, and pdftotext/tesseract when installed.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

EXTS = ("xlsx", "xlsm", "csv", "tsv", "pdf", "docx", "pptx", "txt", "md", "json", "png", "jpg", "jpeg",
        "heic", "gif", "tiff", "eml", "html", "htm", "rtf")
_NAME = re.compile(r"([\w][\w \-–_.,()'’&#+]{0,120}?\.(?:" + "|".join(EXTS) + r"))\b", re.I)
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_STOP = {"re", "fw", "fwd", "the", "and", "for", "with", "from", "this", "that", "your", "file", "files", "copy",
         "final", "latest", "new", "update", "updated", "please", "thanks", "of", "to", "in", "on", "a", "an"}
MAX_BYTES = 40 * 1024 * 1024


@dataclass
class Table:
    name: str
    header: list[str]
    rows: list[list]


@dataclass
class Extract:
    path: str
    kind: str
    text: str = ""
    tables: list[Table] = field(default_factory=list)
    error: str = ""


def names_in(text: str) -> list[str]:
    out = []
    for m in _NAME.finditer(text or ""):
        name = m.group(1).strip(" -–,.'’")
        name = re.sub(r"^(?:here'?s the current version\.?|attached is|attached:?|see)\s+", "", name, flags=re.I).strip()
        if name and name.lower() not in [n.lower() for n in out]:
            out.append(name)
    return out[:12]


def _tokens(text: str) -> set[str]:
    toks = set()
    for tok in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(tok) < 2 or tok in _STOP or tok.isdigit() and len(tok) < 2:
            continue
        toks.add(tok)
        if tok == "sys":
            toks.add("system")
        if tok == "system":
            toks.add("sys")
    return toks


def default_dirs(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    cands = [
        home / "Downloads",
        home / "Desktop",
        home / "Documents",
        home / "Library/Containers/com.microsoft.Outlook/Data/tmp",
        home / "Library/Containers/com.microsoft.Outlook/Data/Library/Caches",
        home / "Library/Group Containers/UBF8T346G9.Office/Outlook/Outlook 15 Profiles",
        home / "Library/Containers/com.apple.mail/Data/Library/Mail Downloads",
        home / "Library/Mail Downloads",
        home / ".config/paiste/sources",
    ]
    return [p for p in cands if p.is_dir()]


def find_files(names: list[str], subject: str, dirs: list[Path], max_age_days: int = 60,
               limit: int = 4, budget_s: float = 3.0) -> list[Path]:
    """Exact attachment names first, then recent files whose name shares words with the subject."""
    want = {n.lower() for n in names}
    want_tokens = [_tokens(Path(n).stem) for n in names]
    subj = _tokens(subject)
    cutoff = time.time() - max_age_days * 86400
    scored: list[tuple[float, Path]] = []
    stop_at = time.monotonic() + budget_s
    seen = 0
    for root in dirs:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in {"node_modules", "Photos Library.photoslibrary"}]
            if time.monotonic() > stop_at:
                break
            for fn in filenames:
                seen += 1
                ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
                if ext not in EXTS:
                    continue
                path = Path(dirpath) / fn
                try:
                    st = path.stat()
                except OSError:
                    continue
                if st.st_size > MAX_BYTES or st.st_size == 0:
                    continue
                low = fn.lower()
                score = 0.0
                if low in want:
                    score = 100.0
                else:
                    ftoks = _tokens(Path(fn).stem)
                    for wt in want_tokens:
                        if wt and len(wt & ftoks) >= max(2, int(len(wt) * 0.6)):
                            score = max(score, 60.0 + len(wt & ftoks))
                    if not score and st.st_mtime >= cutoff and ext in {"xlsx", "xlsm", "csv", "tsv", "pdf", "docx", "pptx"}:
                        common = subj & ftoks
                        if len(common) >= 2:
                            score = 20.0 + 5 * len(common)
                if score:
                    score += min(st.st_mtime / 1e10, 0.2)
                    scored.append((score, path))
    scored.sort(key=lambda item: -item[0])
    out: list[Path] = []
    for _score, path in scored:
        if path.name.lower() in {p.name.lower() for p in out}:
            continue
        out.append(path)
        if len(out) >= limit:
            break
    return out


# ---------- readers ----------

def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "A")
    n = 0
    for ch in letters.group(0) if letters else "A":
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def read_xlsx(path: Path, max_rows: int = 5000) -> list[Table]:
    tables: list[Table] = []
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % _NS["m"])))
        wb = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {r.get("Id"): r.get("Target") for r in rels}
        sheets_el = wb.find("m:sheets", _NS)
        for sheet in (list(sheets_el) if sheets_el is not None else []):
            name = sheet.get("name") or "Sheet"
            rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            target = rel_map.get(rid, "")
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            if target not in zf.namelist():
                continue
            rows: list[list] = []
            for _event, el in ET.iterparse(io.BytesIO(zf.read(target))):
                if el.tag != "{%s}row" % _NS["m"]:
                    continue
                cells: dict[int, object] = {}
                for c in el.findall("m:c", _NS):
                    kind = c.get("t")
                    v = c.find("m:v", _NS)
                    val: object = None
                    if kind == "s" and v is not None:
                        try:
                            val = shared[int(v.text or 0)]
                        except (ValueError, IndexError):
                            val = ""
                    elif kind == "inlineStr":
                        val = "".join(t.text or "" for t in c.iter("{%s}t" % _NS["m"]))
                    elif kind == "b" and v is not None:
                        val = v.text == "1"
                    elif v is not None and v.text is not None:
                        try:
                            num = float(v.text)
                            val = int(num) if num.is_integer() and abs(num) < 1e15 else num
                        except ValueError:
                            val = v.text
                    cells[_col_index(c.get("r") or "A")] = val
                el.clear()
                if cells:
                    width = max(cells) + 1
                    rows.append([cells.get(i) for i in range(width)])
                if len(rows) >= max_rows:
                    break
            tables.append(_as_table(name, rows))
    return tables


def _as_table(name: str, rows: list[list]) -> Table:
    rows = [r for r in rows if any(v not in (None, "") for v in r)]
    header_idx = 0
    for idx, row in enumerate(rows[:10]):
        texts = [v for v in row if isinstance(v, str) and v.strip()]
        if len(texts) >= max(2, int(0.5 * len([v for v in row if v not in (None, '')]))):
            header_idx = idx
            break
    header = [str(v).strip() if v not in (None, "") else f"col{i+1}" for i, v in enumerate(rows[header_idx])] if rows else []
    body = rows[header_idx + 1:] if rows else []
    width = max([len(header)] + [len(r) for r in body]) if (header or body) else 0
    header = header + [f"col{i+1}" for i in range(len(header), width)]
    body = [r + [None] * (width - len(r)) for r in body]
    return Table(name, header, body)


def _num(val) -> float | None:
    if isinstance(val, bool) or val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    text = str(val).strip().replace(",", "").replace("$", "")
    neg = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    mult = 1.0
    if text[-1:].lower() == "k":
        mult, text = 1e3, text[:-1]
    elif text[-1:].lower() == "m":
        mult, text = 1e6, text[:-1]
    if text.endswith("%"):
        text = text[:-1]
    try:
        out = float(text) * mult
    except ValueError:
        return None
    return -out if neg else out


def read_csv(path: Path, max_rows: int = 5000) -> list[Table]:
    raw = path.read_bytes()[:MAX_BYTES].decode("utf-8-sig", errors="replace")
    dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=",;\t|")
    except csv.Error:
        pass
    rows = []
    for row in csv.reader(io.StringIO(raw), dialect):
        rows.append([_num(v) if _num(v) is not None and v.strip() else v for v in row])
        if len(rows) >= max_rows:
            break
    return [_as_table(path.stem, rows)]


def _zip_text(path: Path, members: list[str]) -> str:
    out = []
    with zipfile.ZipFile(path) as zf:
        for name in members:
            if name not in zf.namelist():
                continue
            xml = zf.read(name).decode("utf-8", errors="replace")
            xml = re.sub(r"</w:p>|</a:p>|<w:br/>|</w:tr>", "\n", xml)
            xml = re.sub(r"</w:tc>", "\t", xml)
            out.append(re.sub(r"<[^>]+>", "", xml))
    text = "\n".join(out)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&apos;", "'", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read_docx(path: Path) -> str:
    return _zip_text(path, ["word/document.xml"])


def read_pptx(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        slides = sorted((n for n in zf.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
    return _zip_text(path, slides)


_JXA_PDF = """
ObjC.import('PDFKit');
function run(argv){ var d=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));
 if(!d||d.isNil()) return ''; var s=d.string; return s.isNil()?'':ObjC.unwrap(s); }
"""

_JXA_OCR = """
ObjC.import('Vision'); ObjC.import('AppKit');
function run(argv){ var url=$.NSURL.fileURLWithPath(argv[0]);
 var req=$.VNRecognizeTextRequest.alloc.init; req.recognitionLevel=0; req.usesLanguageCorrection=true;
 var h=$.VNImageRequestHandler.alloc.initWithURLOptions(url,$({}));
 h.performRequestsError($([req]), null);
 var res=req.results; var out=[]; if(!res||res.isNil()) return '';
 var items=[]; for(var i=0;i<res.count;i++){ var o=res.objectAtIndex(i); var c=o.topCandidates(1);
  if(c.count>0){ var b=o.boundingBox; items.push({y:1-(b.origin.y+b.size.height), x:b.origin.x, t:ObjC.unwrap(c.objectAtIndex(0).string)}); } }
 items.sort(function(a,b){ return Math.abs(a.y-b.y)<0.008 ? a.x-b.x : a.y-b.y; });
 var lines=[]; var cur=null; var cy=-1;
 for(var j=0;j<items.length;j++){ var it=items[j];
  if(cur!==null && Math.abs(it.y-cy)<0.008){ cur.push(it.t); } else { if(cur!==null) lines.push(cur.join('  ')); cur=[it.t]; cy=it.y; } }
 if(cur!==null) lines.push(cur.join('  '));
 return lines.join('\\n'); }
"""


def _osa(script: str, arg: str, timeout: float = 25.0) -> str:
    if not shutil.which("osascript"):
        return ""
    try:
        done = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, arg],
                              capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (done.stdout or "").strip() if done.returncode == 0 else ""


def read_pdf(path: Path) -> str:
    if shutil.which("pdftotext"):
        try:
            done = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                                  timeout=25, check=False)
            if done.returncode == 0 and done.stdout.strip():
                return done.stdout
        except (OSError, subprocess.TimeoutExpired):
            pass
    text = _osa(_JXA_PDF, str(path))
    if text:
        return text
    return _pdf_fallback(path)


def _pdf_fallback(path: Path) -> str:
    import zlib
    data = path.read_bytes()[:MAX_BYTES]
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        chunk = m.group(1)
        try:
            chunk = zlib.decompress(chunk)
        except zlib.error:
            pass
        for t in re.findall(rb"\((.*?)(?<!\\)\)\s*Tj|\[(.*?)\]\s*TJ", chunk, re.S):
            raw = t[0] or b"".join(re.findall(rb"\((.*?)(?<!\\)\)", t[1]))
            out.append(raw.decode("latin-1", errors="replace"))
    return " ".join(out)


def ocr_image(path: Path) -> str:
    text = _osa(_JXA_OCR, str(path))
    if text:
        return text
    if shutil.which("tesseract"):
        try:
            done = subprocess.run(["tesseract", str(path), "-", "--psm", "4"], capture_output=True, text=True,
                                  timeout=40, check=False)
            return done.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return ""


def extract(path: Path) -> Extract:
    ext = path.suffix.lower().lstrip(".")
    try:
        if ext in {"xlsx", "xlsm"}:
            return Extract(str(path), "table", tables=read_xlsx(path))
        if ext in {"csv", "tsv"}:
            return Extract(str(path), "table", tables=read_csv(path))
        if ext == "docx":
            return Extract(str(path), "doc", text=read_docx(path))
        if ext == "pptx":
            return Extract(str(path), "doc", text=read_pptx(path))
        if ext == "pdf":
            return Extract(str(path), "doc", text=read_pdf(path))
        if ext in {"png", "jpg", "jpeg", "heic", "gif", "tiff"}:
            return Extract(str(path), "image", text=ocr_image(path))
        if ext in {"html", "htm"}:
            raw = path.read_text(encoding="utf-8", errors="replace")
            return Extract(str(path), "doc", text=re.sub(r"<[^>]+>", " ", raw))
        if ext == "rtf" and shutil.which("textutil"):
            done = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(path)], capture_output=True,
                                  text=True, timeout=15, check=False)
            return Extract(str(path), "doc", text=done.stdout)
        return Extract(str(path), "doc", text=path.read_text(encoding="utf-8", errors="replace")[:200000])
    except (OSError, zipfile.BadZipFile, ET.ParseError, ValueError, KeyError) as exc:
        return Extract(str(path), "error", error=type(exc).__name__)


def table_text(table: Table, max_rows: int = 40) -> str:
    lines = [" | ".join(table.header)]
    for row in table.rows[:max_rows]:
        lines.append(" | ".join(_fmt(v) for v in row))
    if len(table.rows) > max_rows:
        lines.append(f"... {len(table.rows) - max_rows} more rows")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".")
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def remote(cmd: str, payload: dict, timeout: float = 40.0) -> str:
    """Ask the configured helper (for example the work machine over ssh) for attachment text."""
    path = Path(cmd).expanduser() if cmd else None
    if not path or not path.is_file():
        return ""
    try:
        done = subprocess.run([str(path)], input=json.dumps(payload), text=True, capture_output=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (done.stdout or "").strip() if done.returncode == 0 else ""
