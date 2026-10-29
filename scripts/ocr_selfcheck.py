# -*- coding: utf-8 -*-
"""
OCR 自检抽样：导出原图 + 打印 OCR 结果，供 AI 视觉比对。
用法:
  python scripts/ocr_selfcheck.py                      # 随机书、随机页
  python scripts/ocr_selfcheck.py --book 解放神学       # 指定书，随机页
  python scripts/ocr_selfcheck.py --book 解放神学 --page 100
  python scripts/ocr_selfcheck.py --count 3            # 抽 3 页
"""
import os, sys, re, glob, random, argparse
from pathlib import Path
from PIL import Image

LIB = Path('library')
ASSETS = LIB / 'assets'
TEXT_CACHE = LIB / 'ocr_text'
OUT = Path('tmp/_selfcheck')

def page_no(name):
    m = re.search(r'p(\d+)-', name)
    return int(m.group(1)) if m else 0

def list_books():
    books = []
    if not TEXT_CACHE.exists():
        return books
    for d in TEXT_CACHE.iterdir():
        if d.is_dir() and any(d.glob('p*.txt')):
            books.append((d.name, d))
    return sorted(books)

def pick_pages(text_dir, count):
    txts = []
    for t in text_dir.glob('p*.txt'):
        try:
            if t.stat().st_size > 30:
                txts.append(t)
        except Exception:
            pass
    if not txts:
        return []
    return random.sample(txts, min(count, len(txts)))

def export_png(book_stem, pg, tag):
    asset_dir = ASSETS / book_stem
    if not asset_dir.exists():
        return None
    candidates = [p for p in asset_dir.iterdir() if re.search(r'p%d-' % pg, p.name)]
    if not candidates:
        return None
    src = candidates[0]
    OUT.mkdir(parents=True, exist_ok=True)
    im = Image.open(src).convert('RGB')
    w, h = im.size
    if max(w, h) > 2000:
        s = 2000.0 / max(w, h)
        im = im.resize((int(w*s), int(h*s)), Image.LANCZOS)
    out = OUT / f'{tag}.png'
    im.save(out)
    return out, src

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', help='书名关键字')
    ap.add_argument('--page', type=int, help='指定页码')
    ap.add_argument('--count', type=int, default=1, help='抽样页数')
    args = ap.parse_args()

    books = list_books()
    if args.book:
        books = [b for b in books if args.book in b[0]]
    if not books:
        print('没有找到有 OCR 缓存的书。'); return

    random.seed()
    if args.page and args.book and len(books) == 1:
        stem, td = books[0]
        tf = td / f'p{args.page}.txt'
        chosen = [(stem, td, tf)] if tf.exists() else []
        if not chosen:
            print(f'未找到 {stem} 的 p{args.page}.txt'); return
    else:
        chosen = []
        for _ in range(args.count):
            stem, td = random.choice(books)
            pages = pick_pages(td, 1)
            if pages:
                chosen.append((stem, td, pages[0]))

    for idx, (stem, td, tf) in enumerate(chosen, 1):
        pg = page_no(tf.name) or int(re.search(r'p(\d+)', tf.name).group(1))
        tag = f'selfcheck_{idx}_{pg}'
        exported = export_png(stem, pg, tag)
        ocr_text = tf.read_text(encoding='utf-8', errors='replace').strip()

        print('=' * 70)
        print(f'[抽检 #{idx}]')
        print(f'书    : {stem}')
        print(f'页    : {pg}')
        print(f'OCR字数: {len(ocr_text)}')
        if exported:
            png, src = exported
            print(f'原图   : {src}')
            print(f'附件   : {png.as_posix()}')
        else:
            print('原图   : [导出失败]')
        print('--- OCR 文本 ---')
        print(ocr_text[:1500] + ('\n...(截断)' if len(ocr_text) > 1500 else ''))
        print('=' * 70)

if __name__ == '__main__':
    main()
