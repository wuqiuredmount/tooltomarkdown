# -*- coding: utf-8 -*-
"""单页合并：PaddleOCR-VL 出正文 + GOT-OCR 2.0 补脚注。

为什么要跨两个解释器：
  两个模型跑在互不兼容的隔离环境里（.venvs/vlm 用 OpenVINO 2025.4，
  .venvs/got 用 torch 2.4 CPU）。本脚本只做调度与合并，用子进程分别调用。

用法:
  python scripts/merge_vlm_page.py --book 示例书名 --page 12          # 跑两个模型再合并
  python scripts/merge_vlm_page.py --book 示例书名 --page 12 --no-run  # 复用已有输出
  python scripts/merge_vlm_page.py --book X --page N --skip-paddle     # 只补脚注
"""
import re
import sys
import time
import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from postprocess_got import clean as clean_got, normalize_markers
from lib.footnotes import split as split_footnotes
from lib.cache import atomic_write

VLM_PY = ROOT / '.venvs' / 'vlm' / 'Scripts' / 'python.exe'
GOT_PY = ROOT / '.venvs' / 'got' / 'Scripts' / 'python.exe'
VLM_OUT = ROOT / 'tmp' / '_vlm_out'
GOT_OUT = ROOT / 'tmp' / '_got_out'
MERGED = ROOT / 'outputs' / 'merged'

def slug(book: str, page: int) -> str:
    s = re.sub(r'[\\/:*?"<>|\s]', '_', book)[:24]
    return f'{s}_p{page}'


def run_model(py: Path, script: str, book: str, page: int, tag: str):
    if not py.exists():
        print(f'  [SKIP] 找不到解释器 {py}')
        return False
    t0 = time.time()
    p = subprocess.run([str(py), '-u', f'scripts/{script}', '--book', book,
                        '--page', str(page), '--tag', tag],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    dt = time.time() - t0
    ok = p.returncode == 0
    print(f'  [{"OK" if ok else "FAIL"}] {script} {dt:.1f}s')
    if not ok:
        tail = (p.stderr or p.stdout or '').strip().splitlines()[-4:]
        for t in tail:
            print(f'    {t}')
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--page', type=int, required=True)
    ap.add_argument('--no-run', action='store_true', help='复用已有输出，不重新推理')
    ap.add_argument('--skip-paddle', action='store_true')
    ap.add_argument('--skip-got', action='store_true')
    ap.add_argument('--force', action='store_true', help='忽略已有合并结果，强制重跑模型')
    args = ap.parse_args()

    tag = slug(args.book, args.page)
    out = MERGED / f'{tag}.md'
    vlm_md = VLM_OUT / f'{tag}.md'
    got_md = GOT_OUT / f'{tag}.md'

    # 断点续传：合并结果已存在且两路输入都在 → 直接复用，不碰模型
    if out.exists() and not args.force and (args.no_run or (vlm_md.exists() and got_md.exists())):
        print(f'[SKIP] 已存在合并结果（{out.stat().st_size} 字节）：{out}')
        print('       如需重算请加 --force')
        return

    print(f'=== 合并 {args.book} p{args.page}（tag={tag}）===')

    if not args.no_run:
        if not args.skip_paddle and not vlm_md.exists():
            print('[1/2] PaddleOCR-VL（正文，CPU）')
            run_model(VLM_PY, 'ocr_vlm.py', args.book, args.page, tag)
        elif vlm_md.exists():
            print('[1/2] Paddle 输出已存在，跳过推理')
        if not args.skip_got and not got_md.exists():
            print('[2/2] GOT-OCR 2.0（脚注，CPU）')
            run_model(GOT_PY, 'ocr_got.py', args.book, args.page, tag)
        elif got_md.exists():
            print('[2/2] GOT 输出已存在，跳过推理')

    if not vlm_md.exists():
        sys.exit(f'缺少 Paddle 输出: {vlm_md}')
    if not got_md.exists():
        sys.exit(f'缺少 GOT 输出: {got_md}')

    body = normalize_markers(vlm_md.read_text(encoding='utf-8')).strip()
    got_clean = clean_got(got_md.read_text(encoding='utf-8'))

    # 多信号切分 + 正文对账（替代旧的关键词正则）
    fp = split_footnotes(got_clean)
    got_body, foot = fp.body, fp.footnotes
    print(f'  脚注判定：score={fp.score} {fp.signals} → {fp.status}（{fp.reason}）')

    # 正文以 Paddle 为准；若 Paddle 正文异常短（<50 字）则回退用 GOT 正文
    if len(re.findall(r'[\u4e00-\u9fff]', body)) < 50:
        print('  [WARN] Paddle 正文过短，回退使用 GOT 正文')
        body = got_body

    parts = [body]
    if foot and fp.status == 'ok':
        parts.append('\n\n---\n\n## 脚注\n\n' + '\n\n'.join(foot))
    elif foot:
        # 不确定就不并入正式稿，另存 REVIEW 供人工核对
        review = MERGED / f'{tag}.REVIEW.md'
        atomic_write(review, '\n'.join([
            f'# 待复核：{args.book} p{args.page}',
            f'- 判定：{fp.status}（score={fp.score}，{fp.reason}）',
            f'- 信号：{fp.signals}',
            '', '## GOT 侧切出的候选脚注', '', '\n\n'.join(foot),
            '', '## GOT 全文（清洗后）', '', got_clean,
        ]))
        print(f'  [REVIEW] 脚注不确定，已写 {review}')

    MERGED.mkdir(parents=True, exist_ok=True)
    atomic_write(out, '\n'.join(parts).strip() + '\n')

    cjk_n = len(re.findall(r'[\u4e00-\u9fff]', body))
    print(f'  正文：{cjk_n} 汉字 | 脚注：{len(foot)} 条（{fp.status}）')
    print(f'  输出：{out}')
    print('=' * 60)
    print(out.read_text(encoding='utf-8')[:1400])
    print('=' * 60)


if __name__ == '__main__':
    main()
