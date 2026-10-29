# -*- coding: utf-8 -*-
"""OCR 进度监控（只读）。同时作为 dashboard.py 的数据源。

关键修复（2026-10-18）：
  页面总数**以 md 里的 `## Page N` 为准**，不再用图片张数——「一页多图」会把
  251 页的书算成 252 页，永远停在 99% 并误报「卡住」。

用法:
  python scripts/ocr_status.py          # 打印一次表格
  python scripts/ocr_status.py --json   # 输出 JSON（供看板消费）
"""
import re
import json
import time
import argparse
from pathlib import Path

LIB = Path('library')
ASSETS = LIB / 'assets'
CACHE = LIB / 'ocr_text'
LOG = LIB / '.ocr_log.txt'

STALL_SEC = 300          # 未完成且空闲超过此秒数 → 疑似卡住
TAIL_WINDOW = 30         # 速率取样窗口（最近 N 页）


def page_no(name):
    m = re.search(r'p(\d+)', name)
    return int(m.group(1)) if m else 0


def book_total_pages(book_stem):
    """OCR 进度的分母 = **有图片的不同页号数**（即可 OCR 的页）。

    为什么不用 md 的 `## Page N`：书里有缺图页时（比如标称 450 页但只有 448 页有图），
    用 md 页数会让进度永远停在 99%。也不用图片张数：一页多图会虚高
    （比如 251 页却有 252 张图）。取「页号去重」两者都覆盖。
    缺 assets 时退回 md 页数。
    """
    ad = ASSETS / book_stem
    if ad.exists():
        nums = set()
        for p in ad.iterdir():
            m = re.search(r'p(\d+)-', p.name)
            if m:
                nums.add(int(m.group(1)))
            else:
                m2 = re.search(r'(\d+)', p.name)     # Im125.bmp 这类异常名
                if m2:
                    nums.add(int(m2.group(1)))
        if nums:
            return len(nums)

    md = LIB / f'{book_stem}.md'
    if md.exists():
        try:
            text = md.read_text(encoding='utf-8', errors='replace')
            pages = [int(x) for x in re.findall(r'^## Page (\d+) /', text, re.M)]
            if pages:
                return len(pages)
        except Exception:
            pass
    d = CACHE / book_stem
    if d.exists():
        nums = [page_no(p.name) for p in d.glob('p*.txt')]
        if nums:
            return max(nums)
    return 0


def fmt_sec(s):
    if s is None or s < 0 or s > 86400 * 7:
        return '-'
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f'{h}h{m:02d}m' if h else f'{m}m{sec:02d}s'


def rate_of(txts):
    """最近 N 页的 s/页；样本不足返回 None。"""
    if len(txts) < 2:
        return None
    recent = txts[-TAIL_WINDOW:]
    try:
        t0 = recent[0].stat().st_mtime
        t1 = recent[-1].stat().st_mtime
    except OSError:
        return None
    span = t1 - t0
    return span / (len(recent) - 1) if span > 0 else None


def collect():
    """返回每本书的进度字典列表（只读，不写任何文件）。"""
    if not CACHE.exists():
        return []
    now = time.time()
    rows = []
    for d in sorted(CACHE.iterdir()):
        if not d.is_dir():
            continue
        txts = sorted(d.glob('p*.txt'), key=lambda x: page_no(x.name))
        if not txts:
            continue
        done = len(txts)
        total = book_total_pages(d.name) or done
        rate = rate_of(txts)
        try:
            idle = now - txts[-1].stat().st_mtime
        except OSError:
            idle = STALL_SEC + 1
        complete = done >= total
        eta = None if complete else ((total - done) * rate if rate else None)

        chars = 0
        recent = txts[-TAIL_WINDOW:] if len(txts) >= 2 else txts
        for t in recent:
            try:
                chars += len(t.read_text(encoding='utf-8').strip())
            except OSError:
                pass
        rows.append({
            'book': d.name,
            'done': done,
            'total': total,
            'pct': min(100, done * 100 // max(1, total)),
            'rate': rate,
            'eta': eta,
            'idle': idle,
            'chars_per_page': chars // max(1, len(recent)),
            'complete': complete,
            'stalled': (not complete) and idle > STALL_SEC,
        })
    return rows


def active_rows(rows):
    return [r for r in rows if not r['complete'] and not r['stalled']]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    rows = collect()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if not rows:
        print('无 OCR 缓存目录，尚未开始任何任务。')
        return

    print(f'{"书":<32} {"进度":>12} {"速率":>8} {"剩余":>9} {"空闲":>7} {"字/页":>7}')
    print('-' * 84)
    for r in rows:
        rate_s = f'{r["rate"]:.2f}s' if r['rate'] else '-'
        idle_s = '-' if r['complete'] else f'{int(r["idle"])}s'
        flag = ' ✔完成' if r['complete'] else (' ⚠卡住?' if r['stalled'] else '')
        print(f'{r["book"][:30]:<32} {r["done"]:>4}/{r["total"]:<4}{r["pct"]:>3}% '
              f'{rate_s:>8} {fmt_sec(r["eta"]):>9} {idle_s:>7} {r["chars_per_page"]:>7}{flag}')

    print()
    act = active_rows(rows)
    done_n = sum(1 for r in rows if r['complete'])
    print(f'● 活跃 {len(act)} 本 | ✔ 完成 {done_n} 本 | 共 {len(rows)} 本有缓存')


if __name__ == '__main__':
    main()
