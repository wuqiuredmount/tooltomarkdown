# -*- coding: utf-8 -*-
"""结果缓存 / 断点续传 / 原子写。

key = sha1(tool | book | page | tool_ver | 图片 mtime | 图片大小)[:12]
  —— 图片被替换或工具升级都会自动失效，不会误用旧结果。

目录：
  tmp/.cache/<tool>/<book_slug>/p<page>.<key>.md
  tmp/.cache/manifest.jsonl        每次运行一行（审计 + 统计）
"""
import os
import re
import json
import time
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = ROOT / 'tmp' / '.cache'
MANIFEST = CACHE_ROOT / 'manifest.jsonl'


def slug(s: str, n: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]+', '_', s)
    return s[:n]


def key(tool: str, book: str, page: int, img: Path, tool_ver: str = '1') -> str:
    h = hashlib.sha1()
    try:
        st = img.stat()
        meta = f'{int(st.st_mtime)}|{st.st_size}'
    except OSError:
        meta = '0|0'
    h.update(f'{tool}|{book}|{page}|{tool_ver}|{meta}'.encode('utf-8'))
    return h.hexdigest()[:12]


def path_for(tool: str, book: str, page: int, k: str) -> Path:
    return CACHE_ROOT / tool / slug(book) / f'p{page}.{k}.md'


def get(tool: str, book: str, page: int, img: Path, tool_ver: str = '1'):
    """命中返回文本，否则 None。"""
    p = path_for(tool, book, page, key(tool, book, page, img, tool_ver))
    if p.exists():
        try:
            t = p.read_text(encoding='utf-8')
            return t if t.strip() else None
        except OSError:
            return None
    return None


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def put(tool: str, book: str, page: int, img: Path, text: str,
        tool_ver: str = '1', dur: float = 0.0, status: str = 'ok'):
    k = key(tool, book, page, img, tool_ver)
    p = path_for(tool, book, page, k)
    atomic_write(p, text)
    log_manifest({'ts': round(time.time(), 1), 'tool': tool, 'book': book,
                  'page': page, 'key': k, 'dur_s': round(dur, 2),
                  'chars': len(text), 'status': status})
    return p


def log_manifest(rec: dict):
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open('a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def stats():
    if not CACHE_ROOT.exists():
        return {'files': 0, 'bytes': 0, 'runs': 0}
    files = [p for p in CACHE_ROOT.rglob('*.md') if p.is_file()]
    size = sum(p.stat().st_size for p in files)
    runs = 0
    if MANIFEST.exists():
        try:
            runs = sum(1 for _ in MANIFEST.open(encoding='utf-8'))
        except OSError:
            pass
    return {'files': len(files), 'bytes': size, 'runs': runs}
