# -*- coding: utf-8 -*-
"""批量生成校对预览 DOCX（跳过已是最新的、跳过未完成的书）。"""
import sys, subprocess, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from lib import store as S
ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
OUT = ROOT / 'outputs' / 'docx'
books = []
for d in sorted((ROOT/'library'/'ocr_json').iterdir()):
    if not d.is_dir(): continue
    n = len(list(d.glob('p*.json')))
    md = ROOT/'library'/f'{d.name}.md'
    if n < 20 or not md.exists(): continue
    tgt = OUT / f'{d.name.split(" (")[0]}.docx'
    books.append((n, d.name, md, tgt))
books.sort()
for n, name, md, tgt in books:
    if tgt.exists() and tgt.stat().st_mtime > md.stat().st_mtime:
        print(f'skip {name[:30]}（已最新）', flush=True); continue
    t0=time.time()
    r = subprocess.run([PY, str(ROOT/'scripts'/'md_to_docx.py'), '--book', name],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    tail = [l for l in (r.stdout or '').splitlines() if l.startswith('[DOCX] 页') or l.startswith('[DOCX] 含扉页') or l.startswith('[DOCX] 体积')]
    print(f'{name[:30]:<30} {time.time()-t0:5.0f}s {" | ".join(tail) or (r.stderr or "")[-100:]}', flush=True)
print('DONE', flush=True)
