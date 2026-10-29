#!/usr/bin/env python3
"""research.py - 免密钥研究工具（Tool to Markdown）

子命令:
    python scripts/research.py arxiv   "query"   # arXiv 论文
    python scripts/research.py crossref "query"  # Crossref 文献元数据
    python scripts/research.py s2      "query"   # Semantic Scholar 论文
    python scripts/research.py fetch   "URL"     # 抓网页正文转 md
    python scripts/research.py all     "query"   # 三个学术源一起查

通用参数:
    --max N       每个源最多返回条数（默认 10）
    --out DIR     输出资料库（默认 library/）
    --stdout      只打印，不写文件

依赖: 仅标准库（urllib）。网页转 md 复用 to_md.py（需 markdownify+bs4）。
所有 API 均免密钥；仅需本机可联网。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "library"
NL = chr(10)
UA = "ToolToMarkdown-Research/1.0 (local research tool)"


def http_get(url, timeout=30, extra=None):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if extra:
        headers.update(extra)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "")


def slug(text, n=50):
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", text or "").strip("-")
    return (s[:n] or "result").lower()


def today():
    return dt.date.today().isoformat()


def q_arxiv(query, mx=10):
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
        {"search_query": "all:" + query, "start": 0, "max_results": mx})
    raw, _ = http_get(url)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(raw)
    rows = []
    for e in root.findall("a:entry", ns):
        def txt(tag):
            el = e.find(tag, ns)
            return (el.text or "").strip() if el is not None else ""
        link = ""
        for l in e.findall("a:link", ns):
            if l.get("rel") == "alternate" or not link:
                link = l.get("href", "")
        rows.append({
            "title": re.sub(r"\s+", " ", txt("a:title")),
            "authors": [a.find("a:name", ns).text for a in e.findall("a:author", ns)
                        if a.find("a:name", ns) is not None],
            "published": txt("a:published")[:10],
            "summary": re.sub(r"\s+", " ", txt("a:summary")),
            "url": link,
        })
    return rows


def q_crossref(query, mx=10):
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(
        {"query": query, "rows": mx, "select": "title,author,issued,DOI,URL,container-title,abstract"})
    raw, _ = http_get(url)
    data = json.loads(raw.decode("utf-8", "replace"))
    rows = []
    for it in data.get("message", {}).get("items", []):
        authors = [" ".join(x for x in [a.get("given"), a.get("family")] if x)
                   for a in it.get("author", [])]
        year = ""
        parts = it.get("issued", {}).get("date-parts", [[""]])
        if parts and parts[0]:
            year = str(parts[0][0])
        rows.append({
            "title": (it.get("title") or [""])[0],
            "authors": authors,
            "published": year,
            "summary": re.sub(r"<[^>]+>", "", it.get("abstract", "") or "").strip(),
            "url": it.get("URL", ""),
            "venue": (it.get("container-title") or [""])[0],
            "doi": it.get("DOI", ""),
        })
    return rows


def q_s2(query, mx=10):
    import time
    fields = "title,authors,year,abstract,url,venue,externalIds"
    url = "https://api.semanticscholar.org/graph/v1/paper/search?" + urllib.parse.urlencode(
        {"query": query, "limit": mx, "fields": fields})
    import os
    key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    extra = {"x-api-key": key} if key else None
    raw = None
    for attempt in range(3):
        try:
            raw, _ = http_get(url, extra=extra)
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    data = json.loads(raw.decode("utf-8", "replace"))
    rows = []
    for it in data.get("data", []):
        ids = it.get("externalIds") or {}
        rows.append({
            "title": it.get("title", ""),
            "authors": [a.get("name", "") for a in it.get("authors", [])],
            "published": str(it.get("year", "")),
            "summary": re.sub(r"\s+", " ", it.get("abstract", "") or ""),
            "url": it.get("url", ""),
            "venue": it.get("venue", ""),
            "doi": ids.get("DOI", ""),
        })
    return rows


def q_fetch(url):
    import importlib.util
    spec = importlib.util.spec_from_file_location("to_md", str(ROOT / "scripts" / "to_md.py"))
    tm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tm)
    raw, ctype = http_get(url)
    text = raw.decode("utf-8", "replace")
    if "html" in ctype.lower() or text.lstrip()[:1] == "<":
        body = tm.conv_html_raw(text)
        fmt = "html"
    else:
        body, fmt = text, ctype or "text"
    return [{"title": url, "url": url, "body": body, "format": fmt, "retrieved": today()}]


def render(source, query, rows):
    out = ["---", "source: " + source, "query: " + query,
           "retrieved: " + today(), "count: " + str(len(rows)), "---", ""]
    for i, r in enumerate(rows, 1):
        out.append("## " + str(i) + ". " + (r.get("title") or "(untitled)"))
        meta = []
        if r.get("authors"):
            meta.append("作者: " + ", ".join(r["authors"][:5]) + (" 等" if len(r["authors"]) > 5 else ""))
        if r.get("published"):
            meta.append("年份: " + str(r["published"]))
        if r.get("venue"):
            meta.append("出处: " + r["venue"])
        if r.get("doi"):
            meta.append("DOI: " + r["doi"])
        if meta:
            out.append("- " + " | ".join(meta))
        if r.get("url"):
            out.append("- 链接: " + r["url"])
        if r.get("body"):
            out.append("")
            out.append(r["body"])
        elif r.get("summary"):
            out.append("")
            out.append("> " + r["summary"][:1200])
        out.append("")
    return NL.join(out).rstrip() + NL


SOURCES = {"arxiv": q_arxiv, "crossref": q_crossref, "s2": q_s2}


def main():
    ap = argparse.ArgumentParser(description="免密钥研究工具")
    ap.add_argument("cmd", choices=["arxiv", "crossref", "s2", "fetch", "all"])
    ap.add_argument("query")
    ap.add_argument("--max", type=int, default=10)
    ap.add_argument("--out", default=str(LIB))
    ap.add_argument("--stdout", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    jobs = [("arxiv", q_arxiv), ("crossref", q_crossref), ("s2", q_s2)] if args.cmd == "all" \
        else ([(args.cmd, q_fetch)] if args.cmd == "fetch" else [(args.cmd, SOURCES[args.cmd])])

    failed = 0
    for name, fn in jobs:
        try:
            rows = fn(args.query, args.max) if fn is not q_fetch else fn(args.query)
        except Exception as e:
            print("FAIL " + name + "  " + type(e).__name__ + ": " + str(e))
            failed += 1
            continue
        if not rows:
            print("    " + name + ": 无结果")
            continue
        md = render(name, args.query, rows)
        if args.stdout:
            print(md)
        else:
            out_dir.mkdir(parents=True, exist_ok=True)
            fp = out_dir / ("research-" + name + "-" + slug(args.query) + ".md")
            fp.write_text(md, encoding="utf-8")
            print("OK   " + name + "  " + str(len(rows)) + " 条 -> " + out_dir.name + "/" + fp.name)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
