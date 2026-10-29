# -*- coding: utf-8 -*-
"""轨道二备选：GOT-OCR 2.0（580M）单页识别，CPU 推理。

独立环境：.venvs/got（torch 2.4.1 CPU + transformers 4.44.2，与 .venvs/vlm 完全隔离）
用法:
  .venvs/got/Scripts/python.exe scripts/ocr_got.py --book 示例书名 --page 12
  .venvs/got/Scripts/python.exe scripts/ocr_got.py --image a.png --max-new-tokens 512
"""
import os
import re
import time
import argparse
from pathlib import Path

os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')

import torch
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / 'library' / 'assets'
MODEL = Path(os.environ.get('GOT_MODEL_DIR', ROOT / 'models' / 'GOT-OCR2_0'))
OUT = ROOT / 'tmp' / '_got_out'


def resolve_image(book, page):
    dirs = [d for d in ASSETS.iterdir() if d.is_dir() and book in d.name]
    if not dirs:
        raise SystemExit(f'未找到书: {book}')
    pat = re.compile(rf'^p{page}-\d+\.(jpg|jpeg|png|bmp|webp)$', re.I)
    hits = [p for p in dirs[0].iterdir() if pat.match(p.name)]
    if not hits:
        raise SystemExit(f'p{page} 无图片')
    return hits[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image')
    ap.add_argument('--book')
    ap.add_argument('--page', type=int)
    ap.add_argument('--max-new-tokens', type=int, default=1024)
    ap.add_argument('--ocr-type', default='format', choices=['format', 'ocr'],
                    help='format=保留版面换行（便于与 Paddle 对比）；ocr=纯文本')
    ap.add_argument('--max-side', type=int, default=0, help='预缩放长边（0=不缩放）')
    ap.add_argument('--tag')
    args = ap.parse_args()

    img_path = Path(args.image) if args.image else resolve_image(args.book, args.page)
    print(f'[GOT] 图片: {img_path}')
    print(f'[GOT] 模型目录: {MODEL}')

    from transformers import AutoTokenizer, AutoModel

    t0 = time.time()
    tokenizer = AutoTokenizer.from_pretrained(str(MODEL), trust_remote_code=True)
    model = AutoModel.from_pretrained(str(MODEL), trust_remote_code=True,
                                      low_cpu_mem_usage=True, use_safetensors=True)
    model = model.eval().to('cpu')
    print(f'[GOT] 加载完成 {time.time()-t0:.1f}s | 参数量 '
          f'{sum(p.numel() for p in model.parameters())/1e6:.0f}M')

    img = Image.open(img_path).convert('RGB')
    chat_input = str(img_path)
    if args.max_side and max(img.size) > args.max_side:
        s = args.max_side / max(img.size)
        img = img.resize((max(1, int(img.size[0]*s)), max(1, int(img.size[1]*s))), Image.LANCZOS)
        tmp = ROOT / 'tmp' / '.staging' / '_got_scaled.png'
        tmp.parent.mkdir(parents=True, exist_ok=True)
        img.save(tmp)
        chat_input = str(tmp)
        print(f'[GOT] 预缩放至 {img.size}')

    t1 = time.time()
    with torch.no_grad():
        # 官方入口：ocr_type='format' 保留版面换行；'ocr' 为纯文本
        text = model.chat(tokenizer, chat_input, ocr_type=args.ocr_type)
    dt = time.time() - t1
    text = (text or '').replace('<|im_end|>', '').strip()

    print(f'[GOT] 用时 {dt:.1f}s | 输出 {len(text)} 字')
    OUT.mkdir(parents=True, exist_ok=True)
    tag = args.tag or (f'{img_path.parent.name[:14]}_p{args.page}' if args.page else img_path.stem)
    tag = re.sub(r'[\\/:*?"<>|]', '_', tag)
    f = OUT / f'{tag}.md'
    f.write_text(text, encoding='utf-8')
    print('=' * 60)
    print(text[:1500] + ('\n...(截断)' if len(text) > 1500 else ''))
    print('=' * 60)
    print(f'[GOT] 已保存 {f}（{len(text)} 字）')


if __name__ == '__main__':
    main()
