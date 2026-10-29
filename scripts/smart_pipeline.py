# -*- coding: utf-8 -*-
"""四轨统一调度（分层重构版）。

管线：classify → plan → execute → verify → persist

四项基础设施（scripts/lib/）：
  gpu_lock  —— iGPU 跨进程互斥；并兼容「未加锁的旧脚本正在跑」的探测
  footnotes —— 脚注多信号切分（合并流程用）
  cache     —— 结果缓存、断点续传、原子写、manifest
  artifacts —— staging/cache/logs 分层与清理（默认 dry-run）

用法:
  python scripts/smart_pipeline.py --book 示例书名 --page 12 --dry-run
  python scripts/smart_pipeline.py --book 示例书名 --page 12              # 自动路由 + 执行
  python scripts/smart_pipeline.py --book X --page N --tool glm --force   # 强制重跑
  python scripts/smart_pipeline.py --disk                                 # 中间产物占用
  python scripts/smart_pipeline.py --clean                                # 清理预览
  python scripts/smart_pipeline.py --clean --apply                        # 真删
"""
import os
import re
import sys
import json
import time
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from lib import cache as C
from lib import artifacts as A
from lib import gpu_lock as G
from lib import store as S

ASSETS = ROOT / 'library' / 'assets'
LOGS = ROOT / 'logs'
GLOBAL_PY = Path(os.environ.get('OCR_GLOBAL_PY', sys.executable))

# 工具规格：解释器、脚本、输出目录、是否占 GPU、单页超时
TOOLS = {
    'paddle': dict(py=ROOT / '.venvs' / 'vlm' / 'Scripts' / 'python.exe',
                   script='ocr_vlm.py', out=ROOT / 'tmp' / '_vlm_out',
                   gpu=False, timeout=900),
    'got': dict(py=ROOT / '.venvs' / 'got' / 'Scripts' / 'python.exe',
                script='ocr_got.py', out=ROOT / 'tmp' / '_got_out',
                gpu=False, timeout=600),
    'glm': dict(py=ROOT / '.venvs' / 'glm' / 'Scripts' / 'python.exe',
                script='ocr_glm.py', out=ROOT / 'tmp' / '_glm_out',
                gpu=True, timeout=900),
    # 云端兜底：任何 OpenAI 兼容视觉端点，需要 VLM_API_KEY
    'api_vlm': dict(py=GLOBAL_PY, script='api_vlm.py',
                    out=ROOT / 'tmp' / '_api_vlm_out',
                    gpu=False, timeout=240),
}

API_AVAILABLE = bool(os.environ.get('VLM_API_KEY'))
# RapidOCR 只能整本跑（ocr_scanned.py 暂无 --pages），且该文件正被全量任务占用，
# 本阶段不接入单页；路由到它时直接降级，避免「想跑一页却触发全书 OCR」。
RAPIDOCR_UNAVAILABLE = 'RapidOCR 单页模式未实现（ocr_scanned.py 缺 --pages，且正被全量任务占用）'

def is_hard(an: dict) -> bool:
    """极难页：复杂表格或多个疑似无分隔线脚注块，本地工具大概率翻车。"""
    d = an.get('detail', {})
    if d.get('长横线', 0) >= 6:
        return True
    if an['type'] == '双栏' and d.get('底部墨密度', 0) > 0.12:
        return True
    if an['type'] == '单栏' and d.get('底部墨密度', 0) > 0.08:
        return True          # 疑似「无分隔线的脚注」，分类器认不出但内容很密
    return False


def build_chain(ptype: str, hard: bool, forced=None):
    """生成本页的尝试链；api_vlm 只在配置了密钥时参与。"""
    chain = [forced] if forced else list(FALLBACK[ptype])
    if API_AVAILABLE and 'api_vlm' not in chain:
        chain = (['api_vlm'] + chain) if hard else (chain + ['api_vlm'])
    return chain


FALLBACK = {
    '表格': ['glm', 'paddle', 'got'],
    '双栏': ['paddle', 'glm', 'got'],
    '脚注': ['glm', 'got', 'paddle'],
    '单栏': ['got', 'glm', 'paddle'],
}
MIN_CHARS = 30


# ---------------- 分类 ----------------
def _longest_run(row) -> int:
    d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
    starts = np.where(d == 1)[0]
    ends = np.where(d == -1)[0]
    return int((ends - starts).max()) if len(starts) else 0


def classify(img_path: Path) -> dict:
    im = Image.open(img_path).convert('L')
    a = np.asarray(im)
    H, W = a.shape
    b = a < 160
    small = b[::max(1, H // 500), ::max(1, W // 600)]

    w = small.shape[1]
    rules = sum(1 for row in small if _longest_run(row) > w * 0.55)
    smallT = small.T
    h = smallT.shape[1]
    vrules = sum(1 for col in smallT if _longest_run(col) > h * 0.25)

    # ---- 双栏判定：要求「连续空白带」而非单个空白列 ----
    band = b[int(H * 0.2):int(H * 0.8), :]
    band_h = max(1, band.shape[0])
    col_ink = band.sum(axis=0).astype(float)
    blank = col_ink < 0.01 * band_h              # 该列几乎无墨
    # 找最长的连续空白列段
    best_len, best_mid, run, run_start = 0, 0, 0, 0
    for x, v in enumerate(blank):
        if v:
            if run == 0:
                run_start = x
            run += 1
            if run > best_len:
                best_len, best_mid = run, run_start + run // 2
        else:
            run = 0
    total_ink = float(col_ink.sum()) or 1.0
    left_ink = col_ink[:max(1, best_mid)].sum()
    right_ink = col_ink[best_mid:].sum()
    gutter_ok = (best_len >= 0.025 * W) and (0.30 * W <= best_mid <= 0.70 * W)
    two_col = bool(gutter_ok and left_ink > 0.15 * total_ink and right_ink > 0.15 * total_ink)

    # ---- 脚注判定：逐行扫描底部 22%，找「短横线 + 小字」 ----
    foot_top = int(H * 0.78)
    bottom = b[foot_top:, :]
    short_rule = False
    for row in bottom:                           # 必须逐行：分隔线只有 1-3px 高，跳行会漏
        lr = _longest_run(row)
        if 0.10 * W < lr < 0.55 * W:
            short_rule = True
            break
    density = float(bottom.mean()) if bottom.size else 0
    footnote = bool(short_rule and density > 0.005)

    if rules >= 3 and (vrules >= 2 or rules >= 4):
        t = '表格'
    elif two_col:
        t = '双栏'
    elif footnote:
        t = '脚注'
    else:
        t = '单栏'
    return {'type': t, 'detail': {'长横线': rules, '长竖线': vrules,
                                  '空白带宽': round(best_len / W * 100, 1),
                                  '双栏': two_col, '脚注': footnote,
                                  '底部墨密度': round(density, 4)}}


# ---------------- 执行 ----------------
class Unavailable(Exception):
    pass


def _log_event(rec):
    LOGS.mkdir(parents=True, exist_ok=True)
    f = LOGS / f'pipeline-{datetime.now():%Y%m%d}.jsonl'
    with f.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + '\n')


def run_tool(tool: str, book: str, page: int, img: Path, tag: str,
             force=False, gpu_wait=180, raw=False):
    """返回 (text, source)。命中缓存时 source='cache'。"""
    spec = TOOLS[tool]
    if not force:
        hit = C.get(tool, book, page, img)
        if hit:
            return hit, 'cache', 0.0
    if not spec['py'].exists():
        raise Unavailable(f'解释器不存在: {spec["py"]}')

    cmd = [str(spec['py']), '-u', f'scripts/{spec["script"]}',
           '--book', book, '--page', str(page), '--tag', tag]

    t0 = time.time()
    if spec['gpu']:
        # 等全量任务空档（它不加锁，靠外部活动探测识别）
        with G.gpu_lock(tool, wait=True, timeout=gpu_wait, poll=5):
            p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=spec['timeout'])
    else:
        p = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=spec['timeout'])
    dur = time.time() - t0

    if tool == 'api_vlm' and p.returncode == 2:
        raise Unavailable('未配置 VLM_API_KEY')

    out_file = spec['out'] / f'{tag}.md'
    if p.returncode != 0 or not out_file.exists():
        tail = (p.stderr or p.stdout or '').strip().splitlines()[-3:]
        raise RuntimeError(f'{tool} 失败 rc={p.returncode}: ' + ' | '.join(tail))

    text = out_file.read_text(encoding='utf-8')
    if tool == 'got' and not raw:
        try:
            from postprocess_got import clean as clean_got
            text = clean_got(text)
        except Exception:
            pass
    return text, f'run({dur:.0f}s)', dur


def verify(text: str, ptype: str) -> tuple:
    """质量闸门：空文件 / 过短 / 表格无表格结构。"""
    if not text or not text.strip():
        return False, '空输出'
    cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    if ptype == '表格':
        pipes = text.count('|')
        if cjk < MIN_CHARS and pipes < 6:
            return False, f'汉字 {cjk} 且无表格结构'
        return True, f'汉字 {cjk} / 表格行 {pipes}'
    if cjk < MIN_CHARS:
        return False, f'汉字仅 {cjk}'
    return True, f'汉字 {cjk}'


def execute_page(book: str, page: int, img: Path, ptype: str, forced=None,
                 gpu_wait=180, force=False, raw=False, chain=None, hard=False):
    """按尝试链逐级降级，成功即落缓存并写入单页 JSON。"""
    if chain is None:
        chain = build_chain(ptype, hard, forced)
    tag = C.slug(f'{book}_p{page}', 40)
    tried = []

    if not force:
        for tool in chain:
            if tool == 'rapidocr':
                continue
            hit = C.get(tool, book, page, img)
            if not hit:
                continue
            ok, why = verify(hit, ptype)
            if ok:
                S.upsert(book, page, hit, tool=tool, status='success')
                return True, tool, hit, [{'tool': tool, 'source': 'cache',
                                          'verify': why, 'ok': True}]
    for tool in chain:
        if tool == 'rapidocr':
            tried.append({'tool': tool, 'error': RAPIDOCR_UNAVAILABLE})
            continue
        try:
            text, source, dur = run_tool(tool, book, page, img, tag,
                                         force=force, gpu_wait=gpu_wait, raw=raw)
            ok, why = verify(text, ptype)
            tried.append({'tool': tool, 'source': source, 'verify': why, 'ok': ok})
            if ok:
                if source != 'cache':
                    C.put(tool, book, page, img, text, dur=dur)
                S.upsert(book, page, text, tool=tool, status='success')
                return True, tool, text, tried
        except (Unavailable, RuntimeError, TimeoutError, subprocess.TimeoutExpired) as e:
            tried.append({'tool': tool, 'error': str(e)[:200]})
            continue
    S.upsert(book, page, '', tool=None, status='failed')
    return False, None, '', tried


# ---------------- 主流程 ----------------
def resolve_images(book, pages):
    dirs = [d for d in ASSETS.iterdir() if d.is_dir() and book in d.name]
    if not dirs:
        raise SystemExit(f'未找到书: {book}')
    d = dirs[0]
    out = []
    for pg in pages:
        pat = re.compile(rf'^p{pg}-\d+\.(jpg|jpeg|png|bmp|webp)$', re.I)
        hits = sorted(p for p in d.iterdir() if pat.match(p.name))
        if hits:
            out.append((pg, hits[0]))
    return d, out


def parse_pages(spec):
    pages = []
    for part in spec.split(','):
        part = part.strip()
        if '-' in part:
            a, b = part.split('-', 1)
            pages += list(range(int(a), int(b) + 1))
        elif part:
            pages.append(int(part))
    return pages


def cmd_disk():
    print(f'{"目录":<28} {"大小":>10} {"文件数":>8}')
    print('-' * 50)
    total = 0
    for name, size, files in A.disk_report():
        total += size
        print(f'{name:<28} {A.human(size):>10} {files:>8}')
    print('-' * 50)
    print(f'{"合计":<28} {A.human(total):>10}')
    inter = A.intermediate_total()
    st = C.stats()
    print(f'\n可清理的中间产物：{A.human(inter)}')
    print(f'结果缓存：{st["files"]} 个 / {A.human(st["bytes"])} / {st["runs"]} 次运行记录')
    if inter > 5 * 1024 ** 3:
        print('⚠ 中间产物超过 5GB，建议 --clean 预览清理')


def cmd_clean(apply, days=2.0):
    items, total, deleted = A.clean(apply=apply, out_ttl_days=days)
    if not items:
        print('没有可清理的中间产物。')
        return
    print(f'{"路径":<70} {"大小":>9} {"类别":>8} {"年龄":>7}')
    print('-' * 100)
    for p, size, tag, age in items[:40]:
        print(f'{str(p)[-68:]:<70} {A.human(size):>9} {tag:>8} {age:>6.1f}d')
    if len(items) > 40:
        print(f'… 另有 {len(items) - 40} 项')
    print('-' * 100)
    print(f'{"合计":<70} {A.human(total):>9}')
    if apply:
        print(f'已删除 {deleted} 项。')
    else:
        print('这是预览（dry-run）。确认无误后加 --apply 执行删除。')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book')
    ap.add_argument('--page', type=int)
    ap.add_argument('--pages')
    ap.add_argument('--tool', default='auto', choices=['auto', 'paddle', 'got', 'glm', 'rapidocr', 'api_vlm'])
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--report', action='store_true')
    ap.add_argument('--force', action='store_true', help='忽略缓存，强制重跑')
    ap.add_argument('--disk', action='store_true')
    ap.add_argument('--clean', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--raw', action='store_true', help='GOT 输出不做自动清洗')
    ap.add_argument('--gpu-wait', type=int, default=180,
                    help='GPU 被占用时最多等待秒数，超时则降级到 CPU 工具（默认 180）')
    ap.add_argument('--days', type=float, default=2.0,
                    help='--clean 时判定「过期」的天数阈值（默认 2）')
    ap.add_argument('--status', action='store_true', help='GPU 锁/占用状态')
    args = ap.parse_args()

    if args.disk:
        cmd_disk(); return
    if args.clean:
        cmd_clean(args.apply, args.days); return
    if args.status:
        print(json.dumps(G.status(), ensure_ascii=False, indent=2)); return

    if not args.book:
        raise SystemExit('需要 --book（或用 --disk / --clean / --status）')
    pages = parse_pages(args.pages) if args.pages else ([args.page] if args.page else None)
    if not pages:
        raise SystemExit('需要 --page 或 --pages')

    d, items = resolve_images(args.book, pages)
    created, kept, total_pages = S.init_book(args.book)
    print(f'书: {d.name[:44]} | 页数: {len(items)} | JSON 站位: 新建 {created} / 已有 {kept} / 共 {total_pages}')
    if API_AVAILABLE:
        print('云端通道: 已启用（VLM_API_KEY 已配置）')

    plan = []
    for pg, img in items:
        an = classify(img)
        hard = is_hard(an)
        forced = None if args.tool == 'auto' else args.tool
        chain = build_chain(an['type'], hard, forced)
        plan.append({'page': pg, 'img': img, 'type': an['type'],
                     'detail': an['detail'], 'chain': chain, 'hard': hard})

    if args.report or args.dry_run:
        print(f'{"页":>5} {"类型":<5} {"首选":<7} 明细')
        print('-' * 64)
        for p in plan:
            hard = ' ⚑难' if p['hard'] else ''
            print(f'{p["page"]:>5} {p["type"]:<5} {p["chain"][0]:<7} {p["detail"]}{hard}')
    if args.dry_run:
        from collections import Counter
        print('\n路由汇总:', dict(Counter(p['chain'][0] for p in plan)))
        st = G.status()
        if st['external'] or st['locked']:
            print(f'⚠ GPU 当前忙（{st["reason"] or "有锁"}），实际执行时 GLM 会等待')
        return

    ok = 0
    t_all = time.time()
    for p in plan:
        print(f'[p{p["page"]}] {p["type"]} → {" / ".join(p["chain"])}')
        success, tool, text, tried = execute_page(args.book, p['page'], p['img'],
                                                  p['type'],
                                                  None if args.tool == 'auto' else args.tool,
                                                  gpu_wait=args.gpu_wait, force=args.force,
                                                  raw=args.raw, chain=p['chain'], hard=p['hard'])
        _log_event({'ts': round(time.time(), 1), 'book': args.book, 'page': p['page'],
                    'type': p['type'], 'ok': success, 'tool': tool, 'tried': tried})
        for t in tried:
            print('   ', t.get('tool'), '→', t.get('error') or f'ok {t.get("verify","")}')
        if success:
            ok += 1
            print(f'   ✔ {tool} 产出 {len(text)} 字')
        else:
            print('   ✘ 全部工具失败')

    if len(plan) > 1:
        S.refine(args.book)      # 有文字后重算 front/toc 分区
    print(f'\n完成 {ok}/{len(plan)} 页，用时 {time.time()-t_all:.0f}s')


if __name__ == '__main__':
    main()
