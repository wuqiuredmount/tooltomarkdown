# -*- coding: utf-8 -*-
"""轨道三：GLM-OCR（0.9B, Q8_0 GGUF）单页识别，llama.cpp + Vulkan 后端。

设计要点：
  - 权重与二进制都放 D 盘（models/GLM-OCR、tools/llama.cpp），不占 C 盘；
  - 走 llama-cli 单次调用（无服务常驻），`--no-display-prompt` 便于纯文本截取；
  - 上下文默认 9000：社区实测 `-c 2000` 会明显掉点，9000 稳定。

用法:
  .venvs/glm/Scripts/python.exe scripts/ocr_glm.py --book 示例书名 --page 12
  .venvs/glm/Scripts/python.exe scripts/ocr_glm.py --image a.png --prompt "识别表格并输出 HTML"
"""
import os
import re
import sys
import time
import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'library' / 'assets'
MODEL_DIR = ROOT / 'models' / 'GLM-OCR'
LLAMA = ROOT / 'tools' / 'llama.cpp' / 'llama-cli.exe'
OUT = ROOT / 'tmp' / '_glm_out'

MODEL = MODEL_DIR / 'GLM-OCR-Q8_0.gguf'
MMPROJ = MODEL_DIR / 'mmproj-GLM-OCR-Q8_0.gguf'

DEFAULT_PROMPT = ('请识别这张图片中的全部文字，按原始版面顺序逐行输出；'
                  '表格请用 Markdown 表格表示，公式请用 LaTeX。不要添加任何解释。')


def resolve_image(book, page):
    dirs = [d for d in ASSETS.iterdir() if d.is_dir() and book in d.name]
    if not dirs:
        raise SystemExit(f'未找到书: {book}')
    pat = re.compile(rf'^p{page}-\d+\.(jpg|jpeg|png|bmp|webp)$', re.I)
    hits = [p for p in dirs[0].iterdir() if pat.match(p.name)]
    if not hits:
        raise SystemExit(f'p{page} 无图片')
    return hits[0]


def run_llama(img, prompt, max_tokens, ctx, ngl, device, timeout=900):
    if not LLAMA.exists():
        raise SystemExit(f'缺少 llama-cli: {LLAMA}')
    for f in (MODEL, MMPROJ):
        if not f.exists():
            raise SystemExit(f'缺少模型文件: {f}')

    cmd = [str(LLAMA), '-m', str(MODEL), '--mmproj', str(MMPROJ),
           '--image', str(img), '-p', prompt,
           '-n', str(max_tokens), '-c', str(ctx), '--temp', '0',
           '-ngl', str(ngl), '--no-display-prompt', '-st', '--log-disable']
    if device:
        cmd += ['--device', device]

    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', timeout=timeout)
    dt = time.time() - t0
    if p.returncode != 0:
        tail = (p.stderr or '').strip().splitlines()[-8:]
        print('[GLM] 失败：')
        for t in tail:
            print('  ', t)
        raise SystemExit(1)
    return p.stdout or '', dt


CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'


def clean_output(text: str) -> str:
    """严格截取：取最后一个 `> ` 提示行之后、`[ Prompt:` 之前的内容。
    llama-cli 会把 ASCII banner、build 信息、交互命令表都打到 stdout，必须剥掉。
    """
    t = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
    # 从最后一个提示行之后开始
    marks = [m.end() for m in re.finditer(r'^>\s.*$', t, flags=re.M)]
    if marks:
        t = t[marks[-1]:]
    # 到统计行之前结束
    t = re.split(r'^\[\s*Prompt:', t, flags=re.M)[0]
    # 去掉外层代码围栏
    t = re.sub(r'^\s*```[a-zA-Z]*\s*$', '', t, flags=re.M)
    t = t.replace('```', '')
    # GLM 的圈号写法 → ①
    def _circ(m):
        n = int(m.group(1))
        return CIRCLED[n-1] if 1 <= n <= len(CIRCLED) else f'({n})'
    t = re.sub(r'\$?\\textcircled\{(\d+)\}\$?', _circ, t)
    t = re.sub(r'\\textcircled\{(\d+)\}', _circ, t)
    lines = [ln.rstrip() for ln in t.splitlines()]
    out = '\n'.join(lines).strip()
    return re.sub(r'\n{3,}', '\n\n', out) + '\n'


def stage_image(img_path: Path) -> Path:
    """llama.cpp 的 --image 解析会被路径里的空格/括号截断，先复制到安全路径。"""
    safe = ROOT / 'tmp' / '.staging' / '_glm_in.png'
    safe.parent.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    Image.open(img_path).convert('RGB').save(safe)
    return safe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image')
    ap.add_argument('--book')
    ap.add_argument('--page', type=int)
    ap.add_argument('--prompt', default=DEFAULT_PROMPT)
    ap.add_argument('--max-tokens', type=int, default=2048)
    ap.add_argument('--ctx', type=int, default=9000)
    ap.add_argument('--ngl', type=int, default=99)
    ap.add_argument('--device', default='Vulkan0')
    ap.add_argument('--max-side', type=int, default=0)
    ap.add_argument('--tag')
    args = ap.parse_args()

    img_path = Path(args.image) if args.image else resolve_image(args.book, args.page)
    print(f'[GLM] 图片: {img_path}')

    img = stage_image(img_path)
    print(f'[GLM] 暂存副本: {img}')
    if args.max_side:
        from PIL import Image
        im = Image.open(img).convert('RGB')
        if max(im.size) > args.max_side:
            s = args.max_side / max(im.size)
            im = im.resize((max(1, int(im.size[0]*s)), max(1, int(im.size[1]*s))), Image.LANCZOS)
            im.save(img)
            print(f'[GLM] 预缩放至 {im.size}')

    raw, dt = run_llama(img, args.prompt, args.max_tokens, args.ctx, args.ngl, args.device)
    text = clean_output(raw)

    print(f'[GLM] 用时 {dt:.1f}s | 输出 {len(text)} 字')
    OUT.mkdir(parents=True, exist_ok=True)
    tag = args.tag or (f'{img_path.parent.name[:14]}_p{args.page}' if args.page else img_path.stem)
    tag = re.sub(r'[\\/:*?"<>|]', '_', tag)
    f = OUT / f'{tag}.md'
    f.write_text(text, encoding='utf-8')
    print('=' * 60)
    print(text[:1500] + ('\n...(截断)' if len(text) > 1500 else ''))
    print('=' * 60)
    print(f'[GLM] 已保存 {f}')


if __name__ == '__main__':
    main()
