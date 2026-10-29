# -*- coding: utf-8 -*-
"""轨道二：PaddleOCR-VL 1.5 单页 / 小批识别（OpenVINO，独立 venv 运行）。

必须用隔离环境执行，不能污染 RapidOCR 的全局环境（全局是 OpenVINO 2024）：
  .venvs/vlm/Scripts/python.exe scripts/ocr_vlm.py --book 世界航空史 --page 20

环境要点：
  - 全局 2024（RapidOCR，依赖 openvino.runtime 老 API）
  - .venvs/vlm 2025.4（VLM，paddleocr_vl_openvino 使用 openvino.runtime 兼容层）
  - iGPU 必设 PADDLEOCR_VL_GPU_KV_CACHE_PRECISION=f16，否则默认压缩 KV 路径会崩
"""
import os
import sys
import time
import argparse
import re
from pathlib import Path

# iGPU 稳定性开关：必须在导入 openvino 之前设置
os.environ.setdefault('PADDLEOCR_VL_GPU_KV_CACHE_PRECISION', 'f16')

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / 'library'
ASSETS = LIB / 'assets'
OUT = ROOT / 'tmp' / '_vlm_out'

VL_DIR = Path(os.environ.get(
    'PADDLEOCR_VL_DIR', ROOT / 'models' / 'PaddleOCR-VL-1.5-ov'))
LAYOUT_DIR = Path(os.environ.get(
    'PP_DOCLAYOUT_DIR', ROOT / 'models' / 'PP-DoclayoutV3-ov'))


def resolve_image(book, page):
    dirs = [d for d in ASSETS.iterdir() if d.is_dir() and book in d.name]
    if not dirs:
        raise SystemExit(f'未找到书: {book}')
    pat = re.compile(rf'^p{page}-\d+\.(jpg|jpeg|png|bmp|webp)$', re.I)
    hits = [p for p in dirs[0].iterdir() if pat.match(p.name)]
    if not hits:
        raise SystemExit(f'{dirs[0].name} 无 p{page} 图片')
    return hits[0]


# 句末标点：出现这些字符说明该行本身已经收尾，不应与下一行硬拼
SENT_END = '。！？；：!?;:…”’」』）)】》'


def merge_short_lines(md, min_len=18):
    """把被版面「逐行硬切」的短行合并成自然段。

    规则保守：仅当上一行不以句末标点结尾、且长度≥min_len、且下一行不是
    标题/图片/空行时才拼接，避免把图注、标题、脚注吞进正文。
    """
    out, buf = [], ''
    for line in md.splitlines():
        s = line.strip()
        if not s:
            if buf:
                out.append(buf); buf = ''
            out.append('')
            continue
        # 结构性行直接落盘，不参与合并
        if s.startswith(('#', '|', '![', '<', '-', '*', '>')) or re.match(r'^\d+[.、]', s):
            if buf:
                out.append(buf); buf = ''
            out.append(s)
            continue
        if buf and len(buf) >= min_len and buf[-1] not in SENT_END:
            buf += s                      # 续接被硬切的同一段
        else:
            if buf:
                out.append(buf)
            buf = s
    if buf:
        out.append(buf)
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image')
    ap.add_argument('--book')
    ap.add_argument('--page', type=int)
    ap.add_argument('--device', default='CPU', choices=['GPU', 'CPU', 'AUTO'],
                    help='默认 CPU：Intel Iris Xe 跑 GPU 必崩（见 PROJECT_MEMO「坑」）')
    ap.add_argument('--layout-device', default='CPU')
    ap.add_argument('--no-layout', action='store_true', help='跳过版面检测，整页直接识别')
    ap.add_argument('--quant', action='store_true',
                    help='启用 INT8/INT4 压缩（更快更省内存，但 iGPU 上易崩，默认关闭）')
    ap.add_argument('--max-side', type=int, default=0,
                    help='预缩放：长边缩到该像素值（方案A 降显存，0=不缩放）')
    ap.add_argument('--max-tokens', type=int, default=1024,
                    help='单块最大生成 token（方案A 限长）')
    ap.add_argument('--no-merge', dest='merge_lines', action='store_false',
                    help='关闭短行合并后处理（默认开启）')
    ap.set_defaults(merge_lines=True)
    ap.add_argument('--tag', default=None, help='输出文件名标签')
    args = ap.parse_args()

    if args.image:
        img = Path(args.image)
    elif args.book and args.page:
        img = resolve_image(args.book, args.page)
    else:
        raise SystemExit('需要 --image 或 --book + --page')
    if not img.exists():
        raise SystemExit(f'图片不存在: {img}')

    # 方案A：预缩放降显存（GPU 崩溃排查用）
    if args.max_side:
        from PIL import Image
        im = Image.open(img).convert('RGB')
        w, h = im.size
        if max(w, h) > args.max_side:
            s = args.max_side / max(w, h)
            im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
        tmp = ROOT / 'tmp' / '.staging' / '_vlm_scaled.jpg'
        tmp.parent.mkdir(parents=True, exist_ok=True)
        im.save(tmp, quality=92)
        print(f'[VLM] 预缩放 {w}x{h} → {im.size[0]}x{im.size[1]}')
        img = tmp

    print(f'[VLM] 图片: {img}')
    print(f'[VLM] 设备: VLM={args.device} layout={args.layout_device}')
    print(f'[VLM] KV缓存精度: {os.environ.get("PADDLEOCR_VL_GPU_KV_CACHE_PRECISION")}')

    from paddleocr_vl_openvino.paddleocr_vl_pipeline import PaddleOCRVL

    t0 = time.time()
    pipe = PaddleOCRVL(
        layout_model_path=None if args.no_layout else str(LAYOUT_DIR / 'DocLayoutV3.xml'),
        vlm_model_path=str(VL_DIR),
        vlm_device=args.device,
        layout_device=args.layout_device,
        use_layout_detection=not args.no_layout,
        # 关键：本版本 llm_int8_compress 默认 True，在线 INT8 压缩在 Intel iGPU 上会段错误。
        # 默认全部关闭，走原始精度权重，稳定性优先。
        llm_int4_compress=False,
        llm_int8_compress=args.quant,
        llm_int8_quant=args.quant,
        vision_int8_quant=args.quant,
    )
    print(f'[VLM] 初始化 {time.time()-t0:.1f}s')

    t1 = time.time()
    output = pipe.predict(str(img), max_new_tokens=args.max_tokens)
    results = list(output)
    dt = time.time() - t1
    print(f'[VLM] 推理 {dt:.1f}s，返回 {len(results)} 个结果')

    OUT.mkdir(parents=True, exist_ok=True)
    if args.tag:
        tag = args.tag
    elif args.page:
        tag = f'{img.parent.name[:14]}_p{args.page}'
    else:
        tag = img.stem
    tag = re.sub(r'[\\/:*?"<>|]', '_', tag)

    for i, res in enumerate(results, 1):
        # 兼容不同版本的属性名
        md = getattr(res, 'markdown', None)
        if isinstance(md, dict):
            md = md.get('markdown_texts') or md.get('markdown') or str(md)
        if not md:
            for attr in ('md', 'text', 'content'):
                v = getattr(res, attr, None)
                if isinstance(v, str) and v.strip():
                    md = v
                    break
        md = md or ''
        raw_md = md
        if args.merge_lines:
            md = merge_short_lines(md)
            if md != raw_md:
                (OUT / f'{tag}.raw.md').write_text(raw_md, encoding='utf-8')
        out_md = OUT / f'{tag}.md'
        out_md.write_text(md, encoding='utf-8')

        print('=' * 60)
        print(f'[结果 #{i}] 字数 {len(md)} → {out_md}')
        print(md[:1500] + ('\n...(截断)' if len(md) > 1500 else ''))
        print('=' * 60)

        try:
            res.save_to_json(save_path=str(OUT / f'{tag}.json'))
        except Exception as e:
            print(f'[WARN] json 保存失败: {e}')


if __name__ == '__main__':
    main()
