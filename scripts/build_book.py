# -*- coding: utf-8 -*-
"""从单页 JSON 构建整本 Markdown（先站位后填充）。

核心保证：**绝不跳页**。骨架先按物理顺序铺满 `## Page N / total`，
status=success 的页才填内容，其余填 `*(本页内容待补充)*`，页码永不缺失、后续不会错位。

用法:
  python scripts/build_book.py --book 示例书名                 # → outputs/books/<书名>.md
  python scripts/build_book.py --book 示例书名 --inplace       # 直接覆盖 library/*.md（先备份 .bak）
  python scripts/build_book.py --book 示例书名 --from 1 --to 20  # 只构建片段（预览用）
"""
import re
import sys
import time
import shutil
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from lib import store as S

LIB = ROOT / 'library'
OUT_DIR = ROOT / 'outputs' / 'books'


def title_of(book: str) -> str:
    t = re.split(r'\s*[（(]', book)[0]
    return t.strip() or book


def build_book_style(book: str, recs) -> str:
    """回写「原书版式」：模板来自 config/book_profiles.json 的 profile。

    这是 `import_md.py` 的逆运算，也是定点修改后重建 Markdown 的入口。
    页码以 real_page_num / real_page_label 为准（目录与正文各成体系互不干扰）。
    """
    from lib import bookfmt as F
    md_path = ROOT / 'library' / f'{book}.md'
    profile = None
    if md_path.exists():
        try:
            _, prof = F.get_profile(None, md_path.read_text(encoding='utf-8')[:200000])
            profile = prof
        except SystemExit:
            profile = None
    if profile is None:
        _, profile = F.get_profile('page-heading')
    return F.build_markdown(recs, profile, title_of(book))


def build(book: str, start=None, end=None, style='page') -> tuple:
    recs = S.load_book(book)
    if not recs:
        raise SystemExit(f'没有单页 JSON：{book}\n先运行 python scripts/lib/store.py "{book}" 站位')

    total = len(recs)
    recs = [r for r in recs
            if (start is None or r['physical_index'] >= start)
            and (end is None or r['physical_index'] <= end)]

    if style == 'book':
        filled = sum(1 for r in recs
                     if r.get('status') in ('success', 'corrected') and (r.get('text') or '').strip())
        return build_book_style(book, recs), filled, len(recs) - filled, total

    lines = [f'# {title_of(book)}', '']
    filled = blank = 0

    for r in recs:
        phys = r['physical_index']
        lines.append(f'## Page {phys} / {total}')
        lines.append('')

        text = (r.get('text') or '').strip()
        if r.get('status') in ('success', 'corrected') and text:
            lines.append(text)
            filled += 1
        else:
            lines.append('*(本页内容待补充)*')
            blank += 1

        imgs = r.get('images') or []
        if imgs:
            lines.append('')
            for img in imgs:
                lines.append(f'![]({img})')

        lines.append('')
        lines.append('---')
        lines.append('')

    return '\n'.join(lines).rstrip() + '\n', filled, blank, total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--out')
    ap.add_argument('--inplace', action='store_true')
    ap.add_argument('--from', dest='start', type=int)
    ap.add_argument('--to', dest='end', type=int)
    ap.add_argument('--style', choices=['page', 'book'], default='page',
                    help='page=## Page N / total（扫描书）；book=原书版式（--- + 居中页脚页码）')
    args = ap.parse_args()

    md, filled, blank, total = build(args.book, args.start, args.end, args.style)

    if args.inplace:
        target = LIB / f'{args.book}.md'
        if target.exists():
            backup = target.with_suffix(f'.bak-{time.strftime("%Y%m%d%H%M%S")}.md')
            shutil.copy2(target, backup)
            print(f'[BUILD] 已备份原文件 → {backup.name}')
        tmp = target.with_suffix('.tmp')
        tmp.write_text(md, encoding='utf-8')
        tmp.replace(target)
    else:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        name = args.out or (S.re.sub(r'[\\/:*?"<>|]', '_', args.book)[:60] + '.md')
        target = OUT_DIR / name
        target.write_text(md, encoding='utf-8')

    print(f'[BUILD] {target}')
    print(f'[BUILD] 总页 {total} | 已填 {filled} | 待补 {blank}')
    if blank:
        print('[BUILD] 待补页在 Markdown 中占位为「*(本页内容待补充)*」，页码未跳过')


if __name__ == '__main__':
    main()
