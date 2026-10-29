# -*- coding: utf-8 -*-
"""OCR 实时监控看板（只读）。

三种模式：
  python scripts/dashboard.py             # 终端动态看板（rich，2s 刷新，Ctrl+C 退出）
  python scripts/dashboard.py --once      # 只画一帧
  python scripts/dashboard.py --html      # 每 2s 重写 outputs/dashboard.html（浏览器里看）

只读保证：本脚本只读取 `library/.ocr_text/`、`library/.ocr_log.txt` 与各工具输出目录的
mtime，**不写任何被主进程使用的文件**（--html 只写 outputs/dashboard.html）。
"""
import re
import sys
import time
import html
import argparse
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from ocr_status import collect, fmt_sec, book_total_pages, page_no, CACHE, LOG, LIB, ASSETS

VLM_OUT = {'PaddleOCR-VL': ROOT / 'tmp' / '_vlm_out',
           'GOT-OCR': ROOT / 'tmp' / '_got_out',
           'GLM-OCR': ROOT / 'tmp' / '_glm_out'}
HTML_OUT = ROOT / 'outputs' / 'dashboard.html'
_QUEUE_CACHE = {'ts': 0.0, 'data': []}
_PAGE_CACHE = {}
QUEUE_TTL = 60          # 队列信息缓存秒数（读 46 个 md 太慢）
FRESH_SEC = 180          # 多久内有产出才算「正在用这个工具」


# ---------------- 数据采集 ----------------
def latest_mtime(d: Path):
    if not d.exists():
        return 0
    ts = [p.stat().st_mtime for p in d.rglob('*') if p.is_file()]
    return max(ts) if ts else 0


def detect_tool():
    """按最近产出时间判断当前工具。"""
    now = time.time()
    cands = []
    for name, d in VLM_OUT.items():
        ts = latest_mtime(d)
        if ts and now - ts < FRESH_SEC:
            cands.append((ts, name))
    ts = latest_mtime(CACHE)
    if ts and now - ts < FRESH_SEC:
        cands.append((ts, 'RapidOCR'))
    if not cands:
        return '空闲 / 未知'
    return max(cands)[1]


def current_book(rows):
    act = [r for r in rows if not r['complete'] and not r['stalled']]
    if not act:
        return None
    act.sort(key=lambda r: r['idle'])
    return act[0]


def parse_log():
    """返回 (本轮开始时间戳, 当前书开始时间戳, 最近若干行)。"""
    if not LOG.exists():
        return None, None, []
    try:
        lines = LOG.read_text(encoding='utf-8', errors='replace').splitlines()
    except OSError:
        return None, None, []

    today = datetime.now().date()

    def ts_of(line):
        m = re.search(r'\[(\d{2}):(\d{2}):(\d{2})\]', line)
        if not m:
            return None
        h, mi, s = map(int, m.groups())
        return datetime.combine(today, datetime.min.time()).replace(
            hour=h, minute=mi, second=s).timestamp()

    # 本轮起点 = 最后一条「=== 本轮 N 本」
    start = None
    for l in reversed(lines):
        if '=== 本轮' in l:
            start = ts_of(l)
            break
    if start is None:
        start = next((ts_of(l) for l in lines if ts_of(l)), None)

    # 当前书起点 = 最后一条「--- [i/N] 书名」
    cur = None
    for l in reversed(lines):
        if re.search(r'--- \[\d+/\d+\]', l):
            cur = ts_of(l)
            break
    return start, cur, lines[-8:]


def cached_total(stem):
    v = _PAGE_CACHE.get(stem)
    if v is None:
        v = book_total_pages(stem)
        _PAGE_CACHE[stem] = v
    return v


def queue_books(rows, exclude=None):
    """排队中的书：扫描版、覆盖率≥50%、尚未跑完（与批处理同口径）。带 60s 缓存。"""
    now = time.time()
    if now - _QUEUE_CACHE['ts'] < QUEUE_TTL and _QUEUE_CACHE['data']:
        data = _QUEUE_CACHE['data']
        return [b for b in data if b['book'] != exclude]
    done_map = {r['book']: r['complete'] for r in rows}
    out = []
    for md in LIB.glob('*.md'):
        stem = md.stem
        total = cached_total(stem)
        if not total:
            continue
        ad = ASSETS / stem
        if not ad.exists():
            continue
        imgs = [p for p in ad.iterdir() if p.suffix.lower() in ('.jpg', '.jpeg', '.bmp', '.png')]
        if len(imgs) < total * 0.5:
            continue                      # 空壳书，跳过
        if done_map.get(stem):
            continue
        d = CACHE / stem
        done = len(list(d.glob('p*.txt'))) if d.exists() else 0
        out.append({'book': stem, 'total': total, 'done': done})
    out.sort(key=lambda x: x['total'] - x['done'])
    _QUEUE_CACHE.update(ts=now, data=out)
    return [b for b in out if b['book'] != exclude]


# ---------------- 终端渲染 ----------------
def render_rich(rows, cur, tool, started, cur_started, queue, log_tail, console):
    """--once 模式：把同一套布局打印一次。"""
    console.print(_group(rows, cur, tool, started, cur_started, queue, log_tail))


def format_clock(ts):
    return datetime.fromtimestamp(ts).strftime('%H:%M:%S')


# ---------------- HTML 渲染 ----------------
def render_html(rows, cur, tool, started, cur_started, queue, log_tail):
    now = time.time()
    total_done = sum(r['done'] for r in rows)
    total_all = sum(r['total'] for r in rows)
    done_n = sum(1 for r in rows if r['complete'])

    def row_html(r):
        state = '✔ 完成' if r['complete'] else ('⚠ 卡住' if r['stalled'] else '运行中')
        cls = 'done' if r['complete'] else ('warn' if r['stalled'] else 'run')
        rate_txt = f'{r["rate"]:.2f}s' if r['rate'] else '-'
        return (f'<tr class="{cls}"><td class="book">{html.escape(r["book"])}</td>'
                f'<td>{r["done"]}/{r["total"]}</td>'
                f'<td><div class="bar"><i style="width:{r["pct"]}%"></i></div></td>'
                f'<td>{r["pct"]}%</td>'
                f'<td>{rate_txt}</td>'
                f'<td>{fmt_sec(r["eta"])}</td><td>{state}</td></tr>')

    cur_html = '<p class="idle">当前无活跃任务</p>'
    if cur:
        pct = cur['done'] * 100 / max(1, cur['total'])
        elapsed = now - cur_started if cur_started else None
        cur_html = f'''
        <div class="current">
          <div class="title">{html.escape(cur["book"])}</div>
          <div class="bar big"><i style="width:{pct:.1f}%"></i></div>
          <div class="meta">{cur["done"]} / {cur["total"]} 页 · {pct:.1f}% · 工具 <b>{tool}</b>
            · {f"{cur['rate']:.2f}s/页" if cur["rate"] else "-"}
            · 已用 {fmt_sec(elapsed) if elapsed else "-"}
            · 剩余 {fmt_sec(cur["eta"])}</div>
        </div>'''

    q_html = ''.join(
        f'<tr><td class="book">{html.escape(b["book"])}</td>'
        f'<td>{b["total"] - b["done"]}</td><td>{b["total"]}</td></tr>' for b in queue[:12])

    page = f'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="2">
<title>OCR 监控</title>
<style>
 body{{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;padding:24px;
       background:#f6f7f9;color:#1f2328}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#6b7280;font-size:13px;margin-bottom:18px}}
 .card{{background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px 18px;margin-bottom:16px}}
 .current .title{{font-weight:600;font-size:15px;margin-bottom:10px}}
 .bar{{background:#eef0f3;border-radius:6px;height:8px;overflow:hidden}}
 .bar.big{{height:12px}} .bar i{{display:block;height:100%;background:#2563eb;transition:width .4s}}
 .meta{{color:#4b5563;font-size:13px;margin-top:10px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 th,td{{padding:7px 8px;border-bottom:1px solid #f0f1f3;text-align:right;white-space:nowrap}}
 th:first-child,td:first-child{{text-align:left}} td.book{{max-width:520px;overflow:hidden;
   text-overflow:ellipsis}}
 tr.done{{color:#6b7280}} tr.warn{{color:#b45309}} tr.run{{color:#111}}
 .idle{{color:#6b7280}} pre{{background:#0f172a;color:#e5e7eb;padding:12px;border-radius:8px;
   font-size:12px;overflow:auto;max-height:220px}}
</style></head><body>
<h1>OCR 全量任务监控</h1>
<div class="sub">已运行 {fmt_sec(now - started) if started else "-"} · 完成 {done_n} 本 ·
 {total_done}/{total_all} 页 · 刷新于 {format_clock(now)}</div>
<div class="card">{cur_html}</div>
<div class="card"><table><thead><tr><th>书</th><th>进度</th><th></th><th>%</th><th>速率</th><th>剩余</th><th>状态</th></tr></thead>
<tbody>{''.join(row_html(r) for r in sorted(rows, key=lambda x: (x['complete'], -x['pct'])))}</tbody></table></div>
<div class="card"><b>队列（{len(queue)} 本）</b><table><thead><tr><th>书</th><th>待跑页</th><th>总页</th></tr></thead>
<tbody>{q_html}</tbody></table></div>
<div class="card"><b>日志尾部</b><pre>{html.escape(chr(10).join(log_tail[-8:]))}</pre></div>
</body></html>'''
    HTML_OUT.parent.mkdir(parents=True, exist_ok=True)
    HTML_OUT.write_text(page, encoding='utf-8')
    return HTML_OUT


def snapshot():
    rows = collect()
    cur = current_book(rows)
    tool = detect_tool()
    started, cur_started, tail = parse_log()
    queue = queue_books(rows, exclude=cur['book'] if cur else None)
    return rows, cur, tool, started, cur_started, queue, tail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--html', action='store_true')
    ap.add_argument('--interval', type=float, default=2.0)
    args = ap.parse_args()

    if args.html:
        print(f'HTML 看板已启动，每 {args.interval}s 刷新 → {HTML_OUT}')
        print('在浏览器中打开该文件即可（会自动 reload）。Ctrl+C 停止。')
        try:
            while True:
                render_html(*snapshot())
                if args.once:
                    break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print('\n已停止。')
        return

    try:
        from rich.console import Console
        from rich.live import Live
    except ImportError:
        print('未安装 rich，退回纯文本模式（pip install rich）')
        while True:
            rows, cur, tool, started, cur_started, queue, tail = snapshot()
            print(f'\n[{format_clock(time.time())}] 当前: {cur["book"][:40] if cur else "-"} '
                  f'{cur["done"]}/{cur["total"] if cur else 0} | 工具 {tool}')
            time.sleep(args.interval)
            if args.once:
                break
        return

    console = Console()
    if args.once:
        render_rich(*snapshot(), console)
        return

    with Live(console=console, refresh_per_second=1, screen=False) as live:
        try:
            while True:
                live.update(build_renderable())
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass


def build_renderable():
    from rich.console import Group
    rows, cur, tool, started, cur_started, queue, tail = snapshot()
    # 复用 render_rich 的绘制逻辑：临时 Console 捕获不可行，改为直接组装
    return _group(rows, cur, tool, started, cur_started, queue, tail)


def _group(rows, cur, tool, started, cur_started, queue, tail):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    now = time.time()
    done_n = sum(1 for r in rows if r['complete'])
    td = sum(r['done'] for r in rows)
    ta = sum(r['total'] for r in rows)
    head = Text()
    head.append('OCR 全量任务监控  ', style='bold cyan')
    head.append(f'已运行 {fmt_sec(now - started) if started else "-"}  ', style='dim')
    head.append(f'完成 {done_n} 本 | 已有缓存 {td}/{ta} 页', style='green')

    parts = [Panel(head, border_style='cyan')]

    if cur:
        pct = cur['done'] * 100 / max(1, cur['total'])
        bar = '█' * int(40 * pct / 100) + '░' * (40 - int(40 * pct / 100))
        g = Table.grid(padding=(0, 2))
        g.add_column(style='dim', width=8)
        g.add_column()
        g.add_row('当前书', f'[bold]{cur["book"][:56]}[/bold]')
        g.add_row('进度', f'[cyan]{bar}[/cyan] {cur["done"]}/{cur["total"]} ({pct:.1f}%)')
        g.add_row('工具', f'[magenta]{tool}[/magenta]')
        g.add_row('速度', f'{cur["rate"]:.2f}s/页' if cur['rate'] else '-')
        g.add_row('已用时', fmt_sec(now - cur_started) if cur_started else '-')
        g.add_row('剩余', fmt_sec(cur['eta']))
        parts.append(Panel(g, title='进行中', border_style='green'))
    else:
        parts.append(Panel('[yellow]当前无活跃 OCR 任务', border_style='yellow'))

    t = Table(box=None, pad_edge=False, header_style='bold')
    t.add_column('书', no_wrap=True, overflow='ellipsis', max_width=36)
    for c in ('进度', '%', '速率', '状态'):
        t.add_column(c, justify='right')
    for r in sorted(rows, key=lambda x: (x['complete'], -x['pct'])):
        st = '[green]✔[/green]' if r['complete'] else ('[red]⚠卡住[/red]' if r['stalled'] else '[cyan]运行中[/cyan]')
        t.add_row(r['book'][:32], f'{r["done"]}/{r["total"]}', f'{r["pct"]}%',
                  f'{r["rate"]:.2f}s' if r['rate'] else '-', st)
    parts.append(t)

    if queue:
        q = Table(box=None, pad_edge=False, header_style='bold',
                  title=f'队列 {len(queue)} 本 / {sum(b["total"]-b["done"] for b in queue)} 页')
        q.add_column('书', no_wrap=True, overflow='ellipsis', max_width=40)
        q.add_column('待跑', justify='right')
        for b in queue[:6]:
            q.add_row(b['book'][:38], str(b['total'] - b['done']))
        parts.append(q)

    if tail:
        parts.append(Panel('\n'.join(tail[-4:]), title='日志', border_style='dim'))
    return Group(*parts)


if __name__ == '__main__':
    main()
