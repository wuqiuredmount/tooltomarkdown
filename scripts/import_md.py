# -*- coding: utf-8 -*-
"""把「人工精校版 / 任意来源的 Markdown」反向导入为单页 JSON。

版式差异（页码写法、分页符、分区规则）全部来自 `config/book_profiles.json`，
默认 `--profile auto` 自动识别，换书不用改代码：

  center-pageno : `<p align="center">· 12 ·</p>`（页码在分页符之后，属上一页页脚）
  formfeed      : `\\f` 分页，页码在页尾独立成行
  page-heading  : `## Page 12 / 300` 作为页眉
  bare          : 只有 `---`，无页码（不猜）

用法:
  python scripts/import_md.py --book 示例书名                # 自动识别
  python scripts/import_md.py --book 某书 --profile formfeed --md path.md
  python scripts/import_md.py --book 某书 --dry-run
  python scripts/import_md.py --book 某书 --list-profiles
"""
import re
import sys
import time
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from lib import store as S
from lib import bookfmt as F

LIB = ROOT / 'library'


def import_md(book, md_path, profile=None, dry_run=False, force=False, cfg_path=None):
    cfg = F.load_config(cfg_path)
    md_text = md_path.read_text(encoding='utf-8')
    pname, prof = F.get_profile(profile, md_text, cfg)

    parsed = F.pages(md_text, prof)
    if not parsed:
        raise SystemExit('没有解析出任何页，请检查 profile / 分页符设置')
    F.detect_sections(parsed, prof)

    recs = []
    for p in parsed:
        body = p['text']
        recs.append({
            'schema_version': 1,
            'book': book,
            'physical_index': p['phys'],
            'page_key': p['page_key'],
            'real_page_num': p['number'],
            'real_page_label': p['label'],
            'section_type': p['section_type'],
            'text': body,
            'images': [],
            'source_tool': 'manual',
            'status': 'success' if body else 'empty',
            'updated_at': None,
            'meta': {'chars': len(body)},
        })

    if dry_run:
        return pname, recs, 0

    kept = 0
    for rec in recs:
        if S.page_path(book, rec['physical_index']).exists() and not force:
            kept += 1
            continue
        S.atomic_write(S.page_path(book, rec['physical_index']), rec)

    S.atomic_write(S.index_path(book), {
        'book': book,
        'total_pages': len(recs),
        'profile': pname,
        'sections': {s: sum(1 for r in recs if r['section_type'] == s)
                     for s in ('front', 'toc', 'body')},
        'source': f'import_md:{md_path.name}',
        'imported_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
    })
    return pname, recs, kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book')
    ap.add_argument('--md')
    ap.add_argument('--profile', help='版式名；默认 auto 自动识别')
    ap.add_argument('--config', help='配置文件路径（默认 config/book_profiles.json）')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='覆盖已存在的 JSON')
    ap.add_argument('--list-profiles', action='store_true')
    args = ap.parse_args()

    if args.list_profiles:
        cfg = F.load_config(args.config)
        print(f'{"名称":<16} 说明')
        print('-' * 72)
        for k, v in cfg['profiles'].items():
            print(f'{k:<16} {v.get("desc", "")}')
        return

    if not args.book:
        ap.error('需要 --book')

    md = Path(args.md) if args.md else LIB / f'{args.book}.md'
    if not md.exists():
        hits = list(LIB.glob(f'*{args.book}*.md'))
        if not hits:
            sys.exit(f'找不到 md：{md}')
        md = hits[0]

    pname, recs, kept = import_md(args.book, md, args.profile, args.dry_run,
                                  args.force, args.config)

    cjk = sum(len(re.findall(r'[\u4e00-\u9fff]', r['text'])) for r in recs)
    foot = sum(1 for r in recs if any(l.lstrip().startswith('>') for l in r['text'].splitlines()))
    secs = {s: sum(1 for r in recs if r['section_type'] == s) for s in ('front', 'toc', 'body')}
    nums = [r['real_page_num'] for r in recs if r['real_page_num'] is not None]

    print(f'[IMPORT] {args.book}  （profile={pname}{" / 自动识别" if not args.profile else ""}）')
    print(f'  页数 {len(recs)} | 汉字 {cjk} | 含脚注页 {foot} | 分区 {secs}')
    print(f'  页码范围 {min(nums) if nums else "-"}..{max(nums) if nums else "-"}'
          f'（缺号页 {sum(1 for r in recs if r["real_page_num"] is None)}）')
    print(f'  跳过已存在 {kept} | 首末：p1 {recs[0]["page_key"]} '
          f'{recs[0]["real_page_label"]!r} / p{len(recs)} {recs[-1]["page_key"]} '
          f'{recs[-1]["real_page_label"]!r}')


if __name__ == '__main__':
    main()
