# -*- coding: utf-8 -*-
"""单页 JSON 存储（先站位后填充）。

目录：`library/.ocr_json/<书名>/p<物理序号:04d>.json`

为什么文件名用物理序号而不是 page_key：
  物理序号与 `library/assets/<书名>/p<N>-*` 严格 1:1，排序永远稳定；
  page_key（front_01/toc_01/body_001）会随分区判定变化，放**内容里**就不影响定位。

单页 JSON 结构：
{
  "schema_version": 1,
  "book": "示例书名",
  "physical_index": 12,          # 物理顺序，1 起
  "page_key": "body_012",
  "real_page_num": 12,           # 页内印的真实页码，取不到为 null
  "real_page_label": "· 12 ·",   # 原文页脚/页眉的写法
  "section_type": "front|toc|body",
  "text": "",
  "images": ["assets/<书名>/p12-1.jpg"],
  "source_tool": "paddle|got|glm|api_vlm|rapidocr|null",
  "status": "empty|success|failed|review",
  "updated_at": "2026-10-18T19:20:00",
  "meta": {"chars": 0}
}
"""
import re
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'library'
ASSETS = LIB / 'assets'
STORE = LIB / 'ocr_json'

CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩'

# 目录页特征
TOC_HINT = re.compile(r'目\s*录|CONTENTS|目\s*次')
# 前置页特征（序言类）
FRONT_HINT = re.compile(r'^\s*(序|自序|代序|译者序|前言|出版说明|凡例|导言|引言|PREFACE|FOREWORD)\b',
                        re.I)
# 后置页特征
BACK_HINT = re.compile(r'^\s*(后记|附录|参考文献|索引|译名对照|跋)\b', re.I)

# 页脚页码：· 12 · / - 12 - / 12 / 第12页
PAGE_NUM_RE = [
    re.compile(r'^\s*[·•・\-—–\s]{0,3}\s*(\d{1,4})\s*[·•・\-—–\s]{0,3}\s*$'),
    re.compile(r'^\s*第\s*(\d{1,4})\s*页\s*$'),
]
ROMAN_RE = re.compile(r'^\s*([ivxlcdm]{1,7})\s*$', re.I)


# ---------------- 路径 ----------------
def resolve_name(book: str) -> str:
    """把用户给的短书名解析成 assets 下的完整目录名。"""
    if (ASSETS / book).is_dir():
        return book
    try:
        hits = [d.name for d in ASSETS.iterdir() if d.is_dir() and book in d.name]
    except OSError:
        return book
    return hits[0] if hits else book


def book_dir(book: str) -> Path:
    book = resolve_name(book)
    d = STORE / re.sub(r'[\\/:*?"<>|]', '_', book)[:80]
    d.mkdir(parents=True, exist_ok=True)
    return d


def page_path(book: str, physical: int) -> Path:
    return book_dir(book) / f'p{physical:04d}.json'


def index_path(book: str) -> Path:
    return book_dir(book) / '_index.json'


# ---------------- 读取 ----------------
def load(book: str, physical: int):
    p = page_path(book, physical)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return None


def load_book(book: str):
    d = book_dir(book)
    out = []
    for p in sorted(d.glob('p*.json')):
        try:
            out.append(json.loads(p.read_text(encoding='utf-8')))
        except Exception:
            continue
    out.sort(key=lambda r: r.get('physical_index', 0))
    return out


def atomic_write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


# ---------------- 页面资产 ----------------
def image_pages(book: str):
    """返回 [(physical_index, [图片相对路径...])]，按页号升序。"""
    book = resolve_name(book)
    ad = ASSETS / book
    if not ad.exists():
        return []
    groups = {}
    for p in ad.iterdir():
        if p.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.bmp', '.webp'):
            continue
        m = re.search(r'p(\d+)-', p.name) or re.search(r'(\d+)', p.name)
        if not m:
            continue
        groups.setdefault(int(m.group(1)), []).append(f'assets/{book}/{p.name}')
    return sorted(((k, sorted(v)) for k, v in groups.items()))


# ---------------- 页码与分区 ----------------
def extract_real_page(text: str):
    """从页内文字提取真实页码。返回 (int|None, label|None)。

    只看首尾各 4 行——页码一定在页眉/页脚。
    """
    if not text:
        return None, None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return None, None
    head, tail = lines[:4], lines[-4:]
    for cand in tail[::-1] + head:
        for pat in PAGE_NUM_RE:
            m = pat.match(cand)
            if m:
                return int(m.group(1)), cand
        m = ROMAN_RE.match(cand)
        if m and len(cand) <= 8:
            return None, cand          # 罗马数字页码：保留 label，数值留给调用方判断
    return None, None


def _text_of(rec):
    return (rec.get('text') or '').strip()


def detect_sections(records):
    """判定 front / toc / body，并写回 page_key。

    规则（按优先级）：
      1. 文字含「目录/CONTENTS」且行内有引导点或页码 → toc（连续页一起算）
      2. 首个正文页之前、含「序/前言/出版说明」等 → front
      3. 其余 → body
    没有任何文字时（纯扫描且未 OCR），默认 body，等填充后调用 refine() 校正。
    """
    n = len(records)
    section = ['body'] * n
    has_text = any(_text_of(r) for r in records)

    if has_text:
        # 1) 目录页
        toc_idx = [i for i, r in enumerate(records) if TOC_HINT.search(_text_of(r))]
        if toc_idx:
            start = min(toc_idx)
            # 目录通常连续：从首个目录页往后，直到连续 2 页都不像目录
            miss = 0
            for i in range(start, n):
                if TOC_HINT.search(_text_of(records[i])) or re.search(r'\.{3,}\s*\d+', _text_of(records[i])):
                    section[i] = 'toc'
                    miss = 0
                else:
                    miss += 1
                    if miss >= 2:
                        break
            # 目录之前的页 → front
            for i in range(start):
                section[i] = 'front'
        else:
            # 2) 无目录：首个「序/前言」块之前到其结束算 front
            front_end = 0
            for i, r in enumerate(records[:max(1, n // 10)]):
                if FRONT_HINT.search(_text_of(r)):
                    front_end = i + 1
            for i in range(front_end):
                section[i] = 'front'

    # 3) 生成 page_key
    counters = {'front': 0, 'toc': 0, 'body': 0}
    for i, r in enumerate(records):
        st = section[i]
        counters[st] += 1
        width = 2 if st in ('front', 'toc') else 3
        r['section_type'] = st
        r['page_key'] = f'{st}_{counters[st]:0{width}d}'
    return records


# ---------------- 站位（初始化） ----------------
def init_book(book: str, force: bool = False):
    """为该书的每个物理页生成空模板（已存在则保留，除非 force）。

    返回 (新建数, 已存在数, 总页数)。
    """
    book = resolve_name(book)
    pages = image_pages(book)
    if not pages:
        return 0, 0, 0

    created = kept = 0
    records = []
    dirty = set()
    for phys, imgs in pages:
        rec = load(book, phys)
        if rec and not force:
            kept += 1
            if not rec.get('images'):
                rec['images'] = imgs
                dirty.add(phys)
            records.append(rec)
            continue
        records.append({
            'schema_version': 1,
            'book': book,
            'physical_index': phys,
            'page_key': '',                 # 由 detect_sections 统一生成
            'real_page_num': None,
            'real_page_label': None,
            'section_type': 'body',
            'text': '',
            'images': imgs,
            'source_tool': None,
            'status': 'empty',
            'updated_at': None,
            'meta': {'chars': 0},
        })
        created += 1
        dirty.add(phys)

    before = {r['physical_index']: (r['page_key'], r['section_type']) for r in records}
    detect_sections(records)
    for rec in records:
        phys = rec['physical_index']
        if phys in dirty or before.get(phys) != (rec['page_key'], rec['section_type']):
            atomic_write(page_path(book, phys), rec)

    atomic_write(index_path(book), {
        'book': book,
        'total_pages': len(records),
        'sections': {s: sum(1 for r in records if r['section_type'] == s)
                     for s in ('front', 'toc', 'body')},
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    return created, kept, len(records)


# ---------------- 填充 ----------------
def upsert(book: str, physical: int, text: str, tool: str,
           status: str = 'success', images=None, real_page=None):
    """填充某页。real_page 不传则由文本自动提取。"""
    rec = load(book, physical)
    if rec is None:
        # 没有模板就补一个（容错）
        imgs = images or dict(image_pages(book)).get(physical, [])
        rec = {'schema_version': 1, 'book': book, 'physical_index': physical,
               'page_key': '', 'real_page_num': None, 'real_page_label': None,
               'section_type': 'body', 'text': '', 'images': imgs,
               'source_tool': None, 'status': 'empty', 'updated_at': None,
               'meta': {'chars': 0}}

    text = (text or '').strip()
    rec['text'] = text
    rec['source_tool'] = tool
    if status == 'failed' and not text:
        rec['status'] = 'failed'
    else:
        rec['status'] = status if text else 'empty'
    if images is not None:
        rec['images'] = images
    if real_page is not None:
        rec['real_page_num'] = real_page
    elif text:
        num, label = extract_real_page(text)
        if num is not None:
            rec['real_page_num'] = num
            rec['real_page_label'] = label
    rec['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    rec['meta'] = {'chars': len(text)}
    atomic_write(page_path(book, physical), rec)
    return rec


def refine(book: str):
    """填充后重算分区与 page_key（文字出现后目录/前言才可判）。"""
    records = load_book(book)
    if not records:
        return 0
    detect_sections(records)
    for rec in records:
        atomic_write(page_path(book, rec['physical_index']), rec)
    atomic_write(index_path(book), {
        'book': book,
        'total_pages': len(records),
        'sections': {s: sum(1 for r in records if r['section_type'] == s)
                     for s in ('front', 'toc', 'body')},
        'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    return len(records)


def summary(book: str):
    recs = load_book(book)
    if not recs:
        return {'book': book, 'total': 0}
    st = {}
    for r in recs:
        st[r.get('status', 'empty')] = st.get(r.get('status', 'empty'), 0) + 1
    return {'book': book, 'total': len(recs), 'status': st,
            'sections': {s: sum(1 for r in recs if r['section_type'] == s)
                         for s in ('front', 'toc', 'body')}}


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1:
        b = sys.argv[1]
        print(json.dumps(init_book(b), ensure_ascii=False))
        print(json.dumps(summary(b), ensure_ascii=False))
