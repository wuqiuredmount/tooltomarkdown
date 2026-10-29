#!/usr/bin/env python3
"""to_md.py - 全格式转 Markdown（Tool to Markdown）

用法:
    python scripts/to_md.py             # newfiles/ 转 library/，成功后清空已转换文件
    python scripts/to_md.py --out DIR   # 指定输出资料库（默认 library/）
    python scripts/to_md.py --dry-run   # 只报告，不写入、不删除
    python scripts/to_md.py --keep      # 保留 newfiles 原文件

PDF: 按页转换（项目指引）——每页输出 `## Page N / total` + 该页文本 + 该页内嵌图像
     （图像导出到 <out>/assets/<文件名>/，md 内以相对路径引用）。依赖 pdfplumber。
依赖: 基本只用标准库。HTML/EPUB 用 markdownify+bs4；PDF 按页需 pdfplumber。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "newfiles"
DEFAULT_OUT = ROOT / "library"
NL = chr(10)
BAR = chr(124)
BS = chr(92)
TICK3 = chr(96) * 3


class ConvError(Exception):
    """不可转换或缺少依赖。"""


def md_table(rows):
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]
    head, body = rows[0], rows[1:]

    def esc(c):
        return (c or "").replace(BAR, BS + BAR).replace(NL, " ")

    def line(cells):
        return BAR + " " + (" " + BAR + " ").join(esc(c) for c in cells) + " " + BAR

    sep = BAR + " " + (" " + BAR + " ").join("---" for _ in range(width)) + " " + BAR
    return NL.join([line(head), sep] + [line(r) for r in body])


def front_matter(src, fmt):
    return "---" + NL + "source: " + src.name + NL + "format: " + fmt + NL + "---" + NL + NL


def conv_csv(path, delim=None):
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(raw[:4096], delimiters=delim or ",;\t|")
    except csv.Error:
        dialect = csv.excel
    return md_table(list(csv.reader(io.StringIO(raw), dialect)))


def conv_json(path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        pretty = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        pretty = raw.rstrip()
    return TICK3 + "json" + NL + pretty + NL + TICK3


def conv_html_raw(raw):
    try:
        from markdownify import markdownify
        from bs4 import BeautifulSoup
    except ImportError as e:
        raise ConvError("HTML 转换需要 markdownify: pip install markdownify") from e
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return markdownify(str(soup), heading_style="ATX").strip()


def conv_html(path):
    return conv_html_raw(path.read_text(encoding="utf-8", errors="replace"))


def conv_rtf(path):
    raw = path.read_text(encoding="latin-1", errors="replace")
    raw = re.sub(r"'([0-9a-fA-F]{2})",
                 lambda m: bytes([int(m.group(1), 16)]).decode("cp1252", "replace"),
                 raw.replace(BS + "'", "'"))
    raw = re.sub(BS + r"par[d]?\b", NL + NL, raw)
    raw = re.sub(BS + r"line\b", NL, raw)
    raw = re.sub(BS + r"[a-zA-Z]+-?[0-9]* ?", "", raw)
    raw = raw.replace("{", "").replace("}", "")
    return re.sub(NL + "{3,}", NL + NL, raw).strip()


def conv_docx(path):
    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(W + "body")
    if body is None:
        return ""
    parts = []
    for el in list(body):
        if el.tag == W + "p":
            text = "".join(t.text or "" for t in el.iter(W + "t")).strip()
            if not text:
                continue
            style = el.find(W + "pPr/" + W + "pStyle")
            val = (style.get(W + "val", "") if style is not None else "").lower()
            if val.startswith("heading"):
                lvl = re.sub(r"[^0-9]", "", val) or "1"
                parts.append("#" * min(int(lvl), 6) + " " + text)
            else:
                parts.append(text)
        elif el.tag == W + "tbl":
            rows = [["".join(t.text or "" for t in tc.iter(W + "t"))
                     for tc in tr.iter(W + "tc")] for tr in el.iter(W + "tr")]
            parts.append(md_table(rows))
    return (NL + NL).join(p for p in parts if p)


def conv_xlsx(path):
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        shared = []
        if "xl/sharedStrings.xml" in names:
            r = ET.fromstring(z.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in si.iter(NS + "t"))
                      for si in r.iter(NS + "si")]
        sheets = sorted((n for n in names if re.match(r"xl/worksheets/sheet[0-9]+\.xml$", n)),
                        key=lambda n: int(re.search(r"([0-9]+)", n).group(1)))
        blocks = []
        for idx, s in enumerate(sheets, 1):
            r = ET.fromstring(z.read(s))
            rows = []
            for row in r.iter(NS + "row"):
                cells = []
                for c in row.iter(NS + "c"):
                    t = c.get("t")
                    v = c.find(NS + "v")
                    is_ = c.find(NS + "is")
                    if t == "s" and v is not None and v.text:
                        val = shared[int(v.text)] if int(v.text) < len(shared) else ""
                    elif t == "inlineStr" and is_ is not None:
                        val = "".join(x.text or "" for x in is_.iter(NS + "t"))
                    else:
                        val = v.text if v is not None else ""
                    cells.append(val or "")
                rows.append(cells)
            table = md_table(rows)
            if table:
                blocks.append("## Sheet " + str(idx) + NL + NL + table)
    return (NL + NL).join(blocks)


def conv_pptx(path):
    A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide[0-9]+\.xml$", n)),
                        key=lambda n: int(re.search(r"([0-9]+)", n).group(1)))
        blocks = []
        for i, s in enumerate(slides, 1):
            r = ET.fromstring(z.read(s))
            texts = [t.text or "" for t in r.iter(A + "t")]
            body = (NL + NL).join(x.strip() for x in texts if x.strip())
            if body:
                blocks.append("## Slide " + str(i) + NL + NL + body)
    return (NL + NL).join(blocks)


def conv_odt(path):
    T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    TB = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
    OFF = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    body = root.find(OFF + "body")
    holder = body.find(OFF + "text") if body is not None else None
    if holder is None:
        return ""
    parts = []
    for el in list(holder):
        if el.tag == T + "h":
            lvl = re.sub(r"[^0-9]", "", el.get(T + "outline-level") or "1") or "1"
            parts.append("#" * min(int(lvl), 6) + " " + "".join(el.itertext()).strip())
        elif el.tag == T + "p":
            text = "".join(el.itertext()).strip()
            if text:
                parts.append(text)
        elif el.tag == TB + "table":
            rows = [["".join(c.itertext()).strip() for c in tr.iter(TB + "table-cell")]
                    for tr in el.iter(TB + "table-row")]
            parts.append(md_table(rows))
    return (NL + NL).join(p for p in parts if p)


def conv_epub(path, out_dir=None):
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist()
                       if n.lower().endswith((".xhtml", ".html", ".htm")))
        img_root = (out_dir / "assets" / path.stem) if out_dir is not None else None
        if img_root is not None:
            img_root.mkdir(parents=True, exist_ok=True)

        parts = []
        for n in names:
            html = z.read(n).decode("utf-8", "replace")
            md = conv_html_raw(html)
            if img_root is not None and md:
                def repl_img(m):
                    alt = m.group(1)
                    src = m.group(2)
                    base = os.path.dirname(n)
                    zpath = os.path.normpath(os.path.join(base, src)).replace("\\", "/")
                    if zpath in z.namelist():
                        fname = os.path.basename(zpath)
                        out_path = img_root / fname
                        if not out_path.exists():
                            try:
                                out_path.write_bytes(z.read(zpath))
                            except Exception:
                                pass
                        return "![" + alt + "](assets/" + path.stem + "/" + fname + ")"
                    return m.group(0)
                md = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", repl_img, md)
            parts.append(md)
    return (NL + NL + "---" + NL + NL).join(p for p in parts if p)


def conv_pdf(path, out_dir=None):
    """按页转换 PDF：优先使用 PyMuPDF 极速提取，若无则降级到 pdfplumber。"""
    try:
        import pymupdf
        return _conv_pdf_pymupdf(path, out_dir, pymupdf)
    except ImportError:
        pass

    try:
        import fitz
        return _conv_pdf_pymupdf(path, out_dir, fitz)
    except ImportError:
        pass

    try:
        import pdfplumber
    except ImportError as e:
        raise ConvError("PDF 转换需要 pdfplumber 或 PyMuPDF：pip install pymupdf") from e

    return _conv_pdf_pdfplumber(path, out_dir, pdfplumber)

def _conv_pdf_pymupdf(path, out_dir, mupdf_mod):
    """使用 PyMuPDF (fitz) 极速转换 PDF。"""
    import os
    img_root = (out_dir / "assets" / path.stem) if out_dir is not None else None
    if img_root is not None:
        img_root.mkdir(parents=True, exist_ok=True)

    parts = []
    doc = mupdf_mod.open(str(path))
    total = len(doc)
    n_img = 0

    for i in range(total):
        # 写入进度打点
        if out_dir is not None:
            try:
                prog_file = out_dir.parent / 'tmp' / '_tomd_progress.json'
                prog_file.parent.mkdir(exist_ok=True)
                prog_file.write_text(json.dumps({"book": path.name, "curr": i + 1, "total": total}), encoding='utf-8')
            except Exception:
                pass

        page = doc[i]
        parts.append("## Page " + str(i + 1) + " / " + str(total))
        parts.append("")
        text = page.get_text("text").strip()
        parts.append(text if text else "*(本页无可抽取文本)*")

        img_list = page.get_images(full=True)
        for k, img in enumerate(img_list, 1):
            xref = img[0]
            try:
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                ext = base_image["ext"]
                rel = None
                if img_root is not None:
                    new = "p" + str(i + 1) + "-" + str(k) + "." + ext
                    img_path = img_root / new
                    img_path.write_bytes(image_bytes)
                    rel = "assets/" + path.stem + "/" + new

                n_img += 1
                parts.append("")
                if rel:
                    parts.append("![page " + str(i + 1) + " image " + str(k) + "](" + rel + ")")
                else:
                    parts.append("*(第 " + str(i + 1) + " 页第 " + str(k) + " 张图像，未导出)*")
            except Exception:
                pass

        parts.append("")
        parts.append("---")
        parts.append("")

    doc.close()

    if out_dir is not None:
        try:
            prog_file = out_dir.parent / 'tmp' / '_tomd_progress.json'
            if prog_file.exists():
                prog_file.unlink()
        except Exception:
            pass

    body = NL.join(parts).rstrip() + NL
    header = "共 " + str(total) + " 页，图像 " + str(n_img) + " 张。" + NL + NL
    return header + body, "pdf"

def _conv_pdf_pdfplumber(path, out_dir, pdfplumber_mod):
    """使用 pdfplumber 慢速转换（兜底方案）。"""
    from pdfminer.image import ImageWriter
    from pdfminer.layout import LTImage

    import os
    img_root = (out_dir / "assets" / path.stem) if out_dir is not None else None
    if img_root is not None:
        img_root.mkdir(parents=True, exist_ok=True)
    writer = ImageWriter(str(img_root)) if img_root is not None else None
    parts = []
    total = 0
    n_img = 0
    with pdfplumber_mod.open(str(path)) as pdf:
        total = len(pdf.pages)
        for i, page in enumerate(pdf.pages, 1):
            if out_dir is not None:
                try:
                    prog_file = out_dir.parent / 'tmp' / '_tomd_progress.json'
                    prog_file.parent.mkdir(exist_ok=True)
                    prog_file.write_text(json.dumps({"book": path.name, "curr": i, "total": total}), encoding='utf-8')
                except Exception:
                    pass

            parts.append("## Page " + str(i) + " / " + str(total))
            parts.append("")
            text = (page.extract_text() or "").strip()
            parts.append(text if text else "*(本页无可抽取文本)*")
            try:
                imgs = page.images or []
            except Exception:
                imgs = []
            for k, im in enumerate(imgs, 1):
                stream = im.get("stream")
                if stream is None:
                    continue
                bbox = (im.get("x0", 0.0), im.get("y0", 0.0),
                        im.get("x1", 0.0), im.get("y1", 0.0))
                rel = None
                if writer is not None:
                    try:
                        fn = writer.export_image(LTImage(im.get("name", "img"), stream, bbox))
                    except Exception:
                        fn = None
                    if fn:
                        fn = os.path.basename(fn)
                        ext = os.path.splitext(fn)[1] or ".img"
                        new = "p" + str(i) + "-" + str(k) + ext
                        try:
                            os.replace(os.path.join(str(img_root), fn),
                                       os.path.join(str(img_root), new))
                            rel = "assets/" + path.stem + "/" + new
                        except OSError:
                            rel = "assets/" + path.stem + "/" + fn
                n_img += 1
                parts.append("")
                if rel:
                    parts.append("![page " + str(i) + " image " + str(k) + "](" + rel + ")")
                else:
                    parts.append("*(第 " + str(i) + " 页第 " + str(k) + " 张图像，未导出)*")
            parts.append("")
            parts.append("---")
            parts.append("")
    # 切图完成，清理进度文件
    if out_dir is not None:
        try:
            prog_file = out_dir.parent / 'tmp' / '_tomd_progress.json'
            if prog_file.exists():
                prog_file.unlink()
        except Exception:
            pass

    body = NL.join(parts).rstrip() + NL
    header = "共 " + str(total) + " 页，图像 " + str(n_img) + " 张。" + NL + NL
    return header + body, "pdf"


CODE_LANG = {
    ".py": "python", ".pyw": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".jsx": "jsx",
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php",
    ".swift": "swift", ".m": "objectivec", ".mm": "objectivec",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "bash",
    ".bat": "bat", ".cmd": "bat", ".ps1": "powershell",
    ".sql": "sql", ".r": "r", ".jl": "julia", ".lua": "lua", ".pl": "perl",
    ".css": "css", ".scss": "scss", ".sass": "sass", ".less": "less",
    ".vue": "vue", ".svelte": "svelte", ".dart": "dart", ".ex": "elixir",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".ini": "ini", ".cfg": "ini", ".conf": "ini", ".properties": "properties",
    ".tf": "hcl", ".gradle": "groovy", ".tex": "latex", ".bib": "bibtex",
}
PLAIN_EXT = {".txt", ".log", ".text", ".rst", ".org"}
XML_EXT = {".xml", ".svg", ".xsd", ".xsl", ".rss", ".plist"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico",
             ".tif", ".tiff", ".heic", ".psd", ".ai"}
LEGACY_OFFICE = {".doc", ".xls", ".ppt"}


def convert(path, out_dir=None):
    ext = path.suffix.lower()
    if ext in (".md", ".markdown"):
        return path.read_text(encoding="utf-8", errors="replace"), "markdown"
    if ext in PLAIN_EXT:
        return path.read_text(encoding="utf-8", errors="replace"), ext.lstrip(".")
    if ext in CODE_LANG:
        body = path.read_text(encoding="utf-8", errors="replace")
        return TICK3 + CODE_LANG[ext] + NL + body.rstrip() + NL + TICK3, ext.lstrip(".")
    if ext == ".csv":
        return conv_csv(path), "csv"
    if ext == ".tsv":
        return conv_csv(path, "\t"), "tsv"
    if ext in (".json", ".ipynb"):
        return conv_json(path), "json"
    if ext in XML_EXT:
        body = path.read_text(encoding="utf-8", errors="replace")
        return TICK3 + "xml" + NL + body.rstrip() + NL + TICK3, "xml"
    if ext in (".html", ".htm", ".xhtml"):
        return conv_html(path), "html"
    if ext == ".rtf":
        return conv_rtf(path), "rtf"
    if ext == ".docx":
        return conv_docx(path), "docx"
    if ext == ".xlsx":
        return conv_xlsx(path), "xlsx"
    if ext == ".pptx":
        return conv_pptx(path), "pptx"
    if ext in (".odt", ".fodt"):
        return conv_odt(path), "odt"
    if ext == ".epub":
        return conv_epub(path, out_dir), "epub"
    if ext == ".pdf":
        return conv_pdf(path, out_dir)
    if ext in LEGACY_OFFICE:
        raise ConvError("旧版二进制格式 " + ext + " 不支持，请另存为 .docx/.xlsx/.pptx")
    if ext in IMAGE_EXT:
        raise ConvError("图片不是文本格式，跳过")
    raise ConvError("未支持的扩展名 " + (ext or "(无扩展名)"))


def iter_inbox():
    if not INBOX.is_dir():
        return []
    return sorted(p for p in INBOX.rglob("*")
                  if p.is_file()
                  and not any(part.startswith(".") for part in p.relative_to(INBOX).parts))


def main():
    ap = argparse.ArgumentParser(description="全格式转 Markdown 转换器")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="输出资料库目录（默认 library/）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写入、不删除")
    ap.add_argument("--keep", action="store_true", help="转换后保留 newfiles 原文件")
    args = ap.parse_args()

    INBOX.mkdir(parents=True, exist_ok=True)
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    files = iter_inbox()
    if not files:
        print("to_md: newfiles/ 为空 (" + str(INBOX) + ")")
        return 0

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    ok, failed = [], []
    for src in files:
        rel = src.relative_to(INBOX)
        try:
            body, fmt = convert(src, None if args.dry_run else out_dir)
        except ConvError as e:
            failed.append((rel, str(e)))
            continue
        except Exception as e:
            failed.append((rel, type(e).__name__ + ": " + str(e)))
            continue
        target = out_dir / (src.stem + ".md")
        if not args.dry_run:
            target.write_text(front_matter(src, fmt) + body.rstrip() + NL, encoding="utf-8")
        ok.append((rel, target.name, fmt, len(body)))

    removed = 0
    if not args.dry_run and not args.keep:
        for src in files:
            rel = src.relative_to(INBOX)
            if any(rel == r for r, *_ in ok):
                src.unlink()
                removed += 1
        for d in sorted((p for p in INBOX.rglob("*") if p.is_dir()), reverse=True):
            try:
                d.rmdir()
            except OSError:
                pass

    tag = "[dry-run] " if args.dry_run else ""
    for rel, name, fmt, size in ok:
        print(tag + "OK   " + str(rel) + "  ->  " + out_dir.name + "/" + name
              + "  (" + fmt + ", " + str(size) + " chars)")
    for rel, err in failed:
        print("FAIL " + str(rel) + "  " + err)
    tail = "kept source" if (args.keep or args.dry_run) else ("cleared " + str(removed))
    print(NL + "to_md: ok=" + str(len(ok)) + " failed=" + str(len(failed)) + " (" + tail + ")")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
