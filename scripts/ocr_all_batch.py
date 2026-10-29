# -*- coding: utf-8 -*-
"""RapidOCR 全量批处理（父进程调度 + 子进程执行）。

为什么要子进程：
  Intel 核显在长时间连续推理下偶发原生崩溃（0xc0000005，无 Python 堆栈），
  一旦崩溃整个进程就没了。把「一本书」隔离到一个子进程里，父进程检测退出码
  并自动重启续跑（脚本本身支持断点续传），可保证全量任务不因单次崩溃而中断。

用法:
  python scripts/ocr_all_batch.py                  # 全部（页数升序）
  python scripts/ocr_all_batch.py --max-pages 700  # 只跑 ≤700 页的
  python scripts/ocr_all_batch.py --build-only     # 只重构缓存、不跑
"""
import sys, os, time, subprocess, argparse, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import ocr_scanned as O
from lib import done as D

LOG = Path('library/.ocr_log.txt')
STATE = Path('library/.ocr_state.json')
PY = sys.executable


def log(msg):
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    print(line, flush=True)
    with LOG.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def cache_dirs(stem):
    """缓存目录可能有新旧两种写法（ocr_text / .ocr_text），都要认。

    教训：长跑期间若中途改了 ocr_scanned.py 的目录名，父进程仍按旧路径统计，
    子进程写新路径，就会得到「0→0 页」并把已完成的书误判为硬故障。
    """
    out = []
    for name in ('ocr_text', '.ocr_text'):
        d = O.LIB / name / stem
        if d.exists():
            out.append(d)
    return out


def cache_count(stem):
    """已尝试的页数（**不论是否识别出文字**）。

    空白页/整页插图页本来就没有文字，如果只数「非空文件」，这些书永远无法判定完成。
    """
    return sum(len(list(d.glob('p*.txt'))) for d in cache_dirs(stem))


def cache_text_pages(stem):
    """其中真正有文字的页数（用于报告，不用于完成判定）。"""
    return sum(len([p for p in d.glob('p*.txt') if p.stat().st_size > 0])
               for d in cache_dirs(stem))


def run_book(md, n_pages, retries=6):
    """子进程跑单本，崩溃则重试续跑。返回 True 表示页数已覆盖。"""
    for attempt in range(1, retries + 1):
        done_before = cache_count(md.stem)
        if done_before >= n_pages:
            return True
        log(f'  子进程 #{attempt} 启动（已有 {done_before}/{n_pages} 页）')
        t0 = time.time()
        p = subprocess.run([PY, 'scripts/ocr_scanned.py', '--book', md.stem],
                           cwd=str(Path.cwd()), capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        dt = time.time() - t0
        done_after = cache_count(md.stem)
        said_done = '全部完成' in (p.stdout or '')
        tail = (p.stdout or '').strip().splitlines()[-3:]
        log(f'  退出码 {p.returncode} | {done_before}→{done_after} 页 | {dt/60:.1f}min')
        for t in tail:
            log(f'    {t}')
        if done_after >= n_pages:
            return True
        if said_done and done_after > 0:
            # 子进程明确跑完（可能有少量空白页），不算硬故障
            log(f'  {md.stem[:24]} 已跑完，{n_pages - done_after} 页无文本（空白/插图页）')
            return True
        if done_after <= done_before:      # 毫无进展 => 立刻放弃，避免空转
            log('  无进展，判定为硬故障，跳过本书')
            return False
        log('  进程异常退出，续跑…')
        time.sleep(3)                      # 给 GPU 一点恢复时间
    return cache_count(md.stem) >= n_pages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--max-pages', type=int, default=0)
    ap.add_argument('--only')
    ap.add_argument('--force', action='store_true',
                    help='忽略完成标记，重跑选中的书')
    ap.add_argument('--done-list', action='store_true',
                    help='列出已记录完成的书后退出')
    ap.add_argument('--backfill-done', action='store_true',
                    help='把缓存已满的书一次性补记进完成标记后退出')
    args = ap.parse_args()

    if args.backfill_done:
        # 历史基线：这些书早就跑完了，只是当时还没有标记文件。
        # 缓存页数 >= 图片页数 即视为完成，页数以当前 assets 为准。
        added = skipped = 0
        for md in sorted(O.LIB.glob('*.md')):
            ad = O.ASSETS / md.stem
            if not ad.exists():
                continue
            pages = O.image_pages(ad)
            if not pages:
                continue
            n = len(pages)
            cached = cache_count(md.stem)
            if cached < n:
                continue
            if D.is_done(md.stem, n):
                skipped += 1
                continue
            D.mark(md.stem, n, cached)
            added += 1
            print(f'  + {cached}/{n}  {md.stem[:50]}')
        print(f'补记 {added} 本，已存在 {skipped} 本')
        return

    if args.done_list:
        books = D.all_done()
        print(f'已记录完成 {len(books)} 本：')
        for stem, rec in sorted(books.items()):
            print(f"  {rec.get('pages'):>5}页 cached={rec.get('cached'):>5} "
                  f"merged={rec.get('merged')} {rec.get('at')}  {stem[:50]}")
        return

    # 候选来源有两个，缺一不可：
    #   ① md 正文稀疏的扫描书 —— 从没跑过 OCR 的
    #   ② 缓存未满的扫描书 —— md 正文可能已回填一部分，但还有页没跑完
    # 只用 ① 会漏掉后者：一旦回填过若干页，md 密度越过 50 字/页的门槛，
    # 这本书就再也不会被选中，剩余页永远停在占位符（页数多的大书尤其明显）。
    cands = set(O.find_scanned_books())
    for md in O.LIB.glob('*.md'):
        ad = O.ASSETS / md.stem
        if not ad.exists():
            continue
        pages = O.image_pages(ad)
        if not pages:
            continue
        done = cache_count(md.stem)
        # 只补「OCR 进行中」的书（0 < 已缓存 < 总页数）。
        # 缓存为 0 且 md 已有正文的书，正文来自 PDF 文字层抽取，
        # 属于成品而非半成品；要 OCR 它们请显式 --only 指定。
        if 0 < done < len(pages):
            cands.add(md)

    picked, shells, missing = [], [], 0
    for md in cands:
        ad = O.ASSETS / md.stem
        if not ad.exists():
            missing += 1
            continue
        pages = O.image_pages(ad)
        if args.only and args.only not in md.stem:
            continue
        if not pages:
            continue
        md_pages = len(O.re.findall(r'^## Page \d+', md.read_text(encoding='utf-8', errors='replace'), O.re.M))
        if md_pages and len(pages) < md_pages * 0.5:
            shells.append(f'{len(pages)}图/{md_pages}页 {md.stem[:36]}')
            continue
        if args.max_pages and len(pages) > args.max_pages:
            continue
        picked.append((md, len(pages)))

    if shells:
        log(f'SKIP(空壳) {len(shells)} 本（图数远少于页数，需重下源 PDF）')
        for s in shells:
            log(f'    {s}')
    if missing:
        log(f'  跳过 {missing} 本无 assets 的书')

    picked.sort(key=lambda x: x[1])
    log(f'=== 本轮 {len(picked)} 本，共 {sum(n for _, n in picked)} 页 ===')

    t_all = time.time()
    ok = fail = 0

    # 完成判定看三样，任一说「已完成」即跳过：
    #   ① 显式标记 .ocr_done.json（最强，抗缓存误删）
    #   ② 缓存页数已覆盖图片页数
    #   ③ 缓存有正文且 md 已无占位页（半成品 md 的回填已完成）
    # 标记里的页数与当前图片页数不一致时视为失效（换过 PDF），会重新处理。
    done_marked, done_cached = 0, 0
    remain = []
    for md, n in picked:
        if args.force:
            remain.append((md, n))
            continue
        if D.is_done(md.stem, n):
            done_marked += 1
            continue
        if cache_count(md.stem) >= n:
            done_cached += 1
            D.mark(md.stem, n, cache_count(md.stem))
            continue
        remain.append((md, n))

    if done_marked or done_cached:
        log(f'  跳过 {done_marked + done_cached} 本已完成'
            f'（标记 {done_marked} / 缓存满 {done_cached}）')
    log(f'  本轮需处理 {len(remain)} 本')

    for i, (md, n) in enumerate(remain, 1):
        log(f'--- [{i}/{len(remain)}] {md.stem[:44]} ({n}页) ---')
        if run_book(md, n):
            ok += 1
            O.merge_to_md(md)
            cached = cache_count(md.stem)
            D.mark(md.stem, n, cached)
            log(f'  ✔ 完成 {md.stem[:30]}（已处理 {cached} 页 / 有文本 '
                f'{cache_text_pages(md.stem)} 页）→ 已记录完成标记')
        else:
            fail += 1
            log(f'  ✘ 未完成 {md.stem[:30]}')
        STATE.write_text(json.dumps({'ok': ok, 'fail': fail, 'last': md.stem},
                                    ensure_ascii=False), encoding='utf-8')

    log(f'=== 结束：成功 {ok} 本，失败 {fail} 本，用时 {(time.time()-t_all)/60:.1f}min ===')


if __name__ == '__main__':
    main()
