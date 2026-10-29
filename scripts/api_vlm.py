# -*- coding: utf-8 -*-
"""云端视觉通道（OpenAI 兼容协议），作为本地工具全部失败后的最高级兜底。

支持任何 OpenAI 兼容的视觉端点（通义千问 VL / DeepSeek-VL / 自建网关）：
  VLM_API_KEY    必填
  VLM_API_BASE   默认 https://dashscope.aliyuncs.com/compatible-mode/v1
  VLM_API_MODEL  默认 qwen-vl-max
  VLM_API_TIMEOUT 默认 120 秒

用法:
  python scripts/api_vlm.py --book 示例书名 --page 12
  python scripts/api_vlm.py --image a.jpg --prompt "只识别脚注部分"
  python scripts/api_vlm.py --book X --page N --store     # 结果同时写入单页 JSON

退出码：0 成功；2 未配置密钥（调用方据此跳过，不算失败）。
"""
import os
import re
import sys
import json
import time
import base64
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
ASSETS = ROOT / 'library' / 'assets'
OUT = ROOT / 'tmp' / '_api_vlm_out'

DEFAULT_PROMPT = (
    '请逐字识别这张书页图片中的全部文字，保持原段落顺序；'
    '脚注请保留编号并单独列在末尾；表格用 Markdown 表格，公式用 LaTeX。'
    '不要添加任何解释、不要总结、不要遗漏文字。'
)


def resolve_image(book, page):
    dirs = [d for d in ASSETS.iterdir() if d.is_dir() and book in d.name]
    if not dirs:
        raise SystemExit(f'未找到书: {book}')
    pat = re.compile(rf'^p{page}-\d+\.(jpg|jpeg|png|bmp|webp)$', re.I)
    hits = sorted(p for p in dirs[0].iterdir() if pat.match(p.name))
    if not hits:
        raise SystemExit(f'p{page} 无图片')
    return hits[0]


def to_data_url(path: Path, max_side=1600) -> str:
    from PIL import Image
    import io
    im = Image.open(path).convert('RGB')
    if max(im.size) > max_side:
        s = max_side / max(im.size)
        im = im.resize((max(1, int(im.size[0] * s)), max(1, int(im.size[1] * s))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format='JPEG', quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode('ascii')
    return f'data:image/jpeg;base64,{b64}'


def call_api(data_url: str, prompt: str, key: str, base: str, model: str, timeout: int):
    import urllib.request
    import urllib.error

    payload = {
        'model': model,
        'temperature': 0,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'image_url', 'image_url': {'url': data_url}},
                {'type': 'text', 'text': prompt},
            ],
        }],
    }
    req = urllib.request.Request(
        f'{base.rstrip("/")}/chat/completions',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json',
                 'Authorization': f'Bearer {key}'},
        method='POST')

    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = json.loads(r.read().decode('utf-8'))
            return body['choices'][0]['message']['content']
        except urllib.error.HTTPError as e:
            detail = e.read().decode('utf-8', 'replace')[:300]
            last_err = f'HTTP {e.code}: {detail}'
            if e.code in (400, 401, 403, 404):
                break
        except Exception as e:
            last_err = f'{type(e).__name__}: {e}'
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last_err or '未知错误')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book')
    ap.add_argument('--page', type=int)
    ap.add_argument('--image')
    ap.add_argument('--prompt', default=DEFAULT_PROMPT)
    ap.add_argument('--tag')
    ap.add_argument('--store', action='store_true', help='同时写入 library/.ocr_json')
    ap.add_argument('--max-side', type=int, default=1600)
    args = ap.parse_args()

    key = os.environ.get('VLM_API_KEY', '').strip()
    if not key:
        print('[API-VLM] 未配置 VLM_API_KEY（设置后才会启用云端通道）', file=sys.stderr)
        sys.exit(2)

    base = os.environ.get('VLM_API_BASE', 'https://dashscope.aliyuncs.com/compatible-mode/v1')
    model = os.environ.get('VLM_API_MODEL', 'qwen-vl-max')
    timeout = int(os.environ.get('VLM_API_TIMEOUT', '120'))

    img = Path(args.image) if args.image else resolve_image(args.book, args.page)
    print(f'[API-VLM] {img.name} | endpoint={base} | model={model}')

    t0 = time.time()
    data_url = to_data_url(img, args.max_side)
    try:
        text = call_api(data_url, args.prompt, key, base, model, timeout)
    except RuntimeError as e:
        print(f'[API-VLM] 失败：{e}', file=sys.stderr)
        sys.exit(1)
    dt = time.time() - t0
    text = (text or '').strip()

    OUT.mkdir(parents=True, exist_ok=True)
    tag = args.tag or (f'{img.parent.name[:14]}_p{args.page}' if args.page else img.stem)
    tag = re.sub(r'[\\/:*?"<>|]', '_', tag)
    f = OUT / f'{tag}.md'
    f.write_text(text + '\n', encoding='utf-8')
    print(f'[API-VLM] {dt:.1f}s | {len(text)} 字 → {f}')

    if args.store and args.book and args.page:
        from lib.store import upsert
        upsert(args.book, args.page, text, tool='api_vlm', status='success')
        print('[API-VLM] 已写入单页 JSON')


if __name__ == '__main__':
    main()
