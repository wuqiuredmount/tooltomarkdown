# -*- coding: utf-8 -*-
"""
批量 OCR 扫描版书籍并回填 md（CPU/GPU 可切换 + 3分块版）。

策略:
  1. 每页上下切 3 块（重叠 15%），每块长边缩放到 960（32 倍数）
  2. 强制文字行宽 480（消除 GPU 动态形状重编译）
  3. 按 Y 坐标全局排序；位置接近且文本相似才判为重复行
  4. 断点续传：每页结果写 .ocr_text/pN.txt
  5. 全部完成后统一回填 md

已知限制：
  - 脚注（约 8px 小字）在 960 约束下难以识别
  - 正文准确率约 95%+，适合检索；精确引用建议用 CPU 原图逐页复核
"""
import os, sys, re, glob, time, argparse
from difflib import SequenceMatcher
from pathlib import Path
from PIL import Image

LIB = Path('library')
ASSETS = LIB / 'assets'
TEXT_CACHE = LIB / 'ocr_text'
OV_CACHE = LIB / '.ov_cache'
LOG_PATH = LIB / '.ocr_log.txt'

def plog(msg, echo=True):
    """进度同时落盘，避免终端断开后进度不可见。echo=False 时只写文件。"""
    line = f'[{time.strftime("%H:%M:%S")}] {msg}'
    if echo:
        print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass

# ---------- GPU 引擎 ----------
def _pick_device(ov):
    """OCR_DEVICE=GPU 显式启用独显；默认 CPU（集显上 CPU 更快且无需驱动）。"""
    want = os.environ.get('OCR_DEVICE', 'CPU').upper()
    if want == 'GPU':
        return 'GPU'
    if want == 'AUTO':
        try:
            return 'GPU' if 'GPU' in ov.Core().available_devices else 'CPU'
        except Exception:
            return 'CPU'
    return 'CPU'


def build_engine():
    try:
        from rapidocr_openvino.ch_ppocr_rec.text_recognize import TextRecognizer
        import openvino.runtime as ov
    except ImportError as e:
        raise SystemExit(
            f'RapidOCR 未安装（缺少 {e.name}）。请运行：python -m pip install -r requirements-ocr.txt'
        ) from None
    _orig_resize = TextRecognizer.resize_norm_img
    def _patched(self, img, max_wh_ratio):
        return _orig_resize(self, img, 480.0 / 48.0)
    TextRecognizer.resize_norm_img = _patched

    OV_CACHE.mkdir(parents=True, exist_ok=True)
    device = _pick_device(ov)
    _orig_compile = ov.Core.compile_model
    def _force_device(self, model, device_name='CPU', config=None, **kw):
        cfg = dict(config or {})
        cfg['CACHE_DIR'] = str(OV_CACHE.absolute())
        return _orig_compile(self, model, device_name=device, config=cfg, **kw)
    ov.Core.compile_model = _force_device

    from rapidocr_openvino import RapidOCR
    rec_batch = int(os.environ.get('OCR_REC_BATCH', '1'))
    plog(f'RapidOCR 设备: {device}')
    return RapidOCR(use_cls=False, rec_img_shape=[3,48,480], rec_batch_num=rec_batch)

# ---------- 3 分块 OCR ----------
def merge_lines(items):
    """按 Y 排序；仅当位置接近且文本相似时才视为重叠重复行。"""
    merged = []
    for cy, txt in sorted(items, key=lambda x: x[0]):
        if merged and abs(cy - merged[-1][0]) < 15:
            prev = re.sub(r'\s+', '', merged[-1][1])
            cur = re.sub(r'\s+', '', txt)
            if prev == cur or (prev and cur and SequenceMatcher(None, prev, cur).ratio() > 0.8):
                continue
        merged.append((cy, txt))
    return [t for _, t in merged]


def ocr_page(engine, img):
    W, H = img.size
    ov = int(H * 0.15)

    def ocr_region(crop, offset_y):
        w, h = crop.size
        scale = 960.0 / max(w, h)
        nw = max(32, int(w*scale)//32*32)
        nh = max(32, int(h*scale)//32*32)
        im2 = crop.resize((nw, nh), Image.LANCZOS)
        res, _ = engine(im2)
        items = []
        if res:
            for box, text, score in res:
                cy = sum(pt[1] for pt in box) / 4.0
                cy_global = cy / (nh / h) + offset_y
                items.append((cy_global, text))
        return items

    h1_end = H//3 + ov
    h2_start = H//3 - ov
    h2_end = 2*H//3 + ov
    h3_start = 2*H//3 - ov

    items = []
    items += ocr_region(img.crop((0, 0, W, h1_end)), 0)
    items += ocr_region(img.crop((0, h2_start, W, h2_end)), h2_start)
    items += ocr_region(img.crop((0, h3_start, W, H)), h3_start)

    return merge_lines(items)

# ---------- 单本处理 ----------
def page_index(name):
    """从文件名解析真实页号：p12-1.bmp -> 12；异常名 Im125.bmp 兜底取数字。"""
    m = re.search(r'p(\d+)-', name)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)', name)
    return int(m.group(1)) if m else None

def image_pages(asset_dir):
    """返回 [(页号, 路径)]，按页号升序；同页多图只取第一张。"""
    seen, out = set(), []
    for p in asset_dir.iterdir():
        if p.suffix.lower() not in ('.jpg','.jpeg','.bmp','.png'):
            continue
        pg = page_index(p.name)
        if pg is None or pg in seen:
            continue
        seen.add(pg)
        out.append((pg, p))
    out.sort(key=lambda x: x[0])
    return out

def process_book(md_path, engine, force=False):
    book_stem = md_path.stem
    asset_dir = ASSETS / book_stem
    if not asset_dir.exists():
        print(f'  [SKIP] 无 assets'); return

    pages = image_pages(asset_dir)
    if not pages:
        print(f'  [SKIP] assets 为空'); return

    # 覆盖率闸门：图片页数远少于 md 页数 => 图片已损坏（JBIG2 空壳），OCR 无意义
    md_pages = len(re.findall(r'^## Page \d+', md_path.read_text(encoding='utf-8', errors='replace'), re.M))
    if md_pages and len(pages) < md_pages * 0.5:
        print(f'  [SKIP] 图片仅 {len(pages)} 张 / {md_pages} 页，覆盖率不足 50%（疑似空壳），需重新下载源 PDF')
        return

    text_dir = TEXT_CACHE / book_stem
    text_dir.mkdir(parents=True, exist_ok=True)
    total = len(pages)
    t_start = time.time()
    idx = 0

    for pg, img_path in pages:
        idx += 1
        txt_file = text_dir / f'p{pg}.txt'   # 以真实页号命名，保证与 md 的 ## Page N 对齐
        if txt_file.exists() and not force:
            continue
        try:
            t_page = time.time()
            img = Image.open(img_path).convert('RGB')
            lines = ocr_page(engine, img)
            txt_file.write_text('\n'.join(lines), encoding='utf-8')
            plog(f'    [{book_stem[:14]}] p{pg} {idx}/{total} '
                 f'{time.time()-t_page:.2f}s {len("".join(lines))}字', echo=False)
        except Exception as e:
            plog(f'    [ERR] p{pg}: {e}')
            txt_file.write_text('', encoding='utf-8')

        if idx <= 3 or idx % 50 == 0 or idx == total:
            elapsed = time.time() - t_start
            remain = (total - idx) * (elapsed / idx)
            plog(f'    [{book_stem[:18]}] {idx}/{total} (p{pg})  {elapsed/idx:.2f}s/页  剩余~{remain/60:.0f}min')

    plog(f'  [TEXT] {book_stem[:24]} 完成 {total} 页')

# ---------- 回填 ----------
def merge_to_md(md_path):
    book_stem = md_path.stem
    text_dir = TEXT_CACHE / book_stem
    if not text_dir.exists():
        print('  [MERGE-SKIP] 无缓存'); return

    content = md_path.read_text(encoding='utf-8')
    pat = re.compile(r'^(## Page (\d+) / \d+)$', re.M)
    matches = list(pat.finditer(content))
    if not matches:
        print('  [MERGE-SKIP] 无分页'); return

    merged = 0
    kept_imgs = 0
    segments = []
    for i, m in enumerate(matches):
        end = matches[i+1].start() if i+1 < len(matches) else len(content)
        pg = int(m.group(2))
        body = content[m.end():end]
        tf = text_dir / f'p{pg}.txt'
        if tf.exists():
            txt = tf.read_text(encoding='utf-8').strip()
            if txt and '未导出' not in txt:
                # 保留本页「全部」图片引用（多图页按原顺序），只替换文字部分。
                # 注意：书名/路径常含括号，不能用 \(...\) 收尾，必须锚定到图片扩展名，
                # 否则会在路径内的第一个 ')' 处截断，生成坏链接。
                refs = re.findall(r'!\[[^\]]*\]\([^\n]*?\.(?:jpe?g|png|bmp|webp|gif)\)', body)
                kept_imgs += len(refs)
                img_block = '\n'.join(refs)
                body = f'\n\n{txt}\n\n' + (f'{img_block}\n\n' if refs else '') + '---\n\n'
                merged += 1
        segments.append((m.group(1), body))

    out = [content[:matches[0].start()]]
    for t, b in segments:
        out.append(t); out.append(b)
    md_path.write_text(''.join(out), encoding='utf-8')
    plog(f'  [MERGE] {book_stem[:24]} 回填 {merged} 页，保留图引用 {kept_imgs} 处')

# ---------- 链接修复 ----------
def repair_image_refs(md_path):
    """按 assets 目录重建每页图片引用。

    用途：早期 merge 用 \(...\) 抓图，书名含括号时会在路径内第一个 ')' 处截断，
    生成坏链接（如「世界航空史 (（英）...」221 页全坏）。assets 是权威来源，
    从中按 p{页}-{序}.ext 重建，可完全复原。
    """
    book_stem = md_path.stem
    asset_dir = ASSETS / book_stem
    if not asset_dir.exists():
        print('  [REPAIR-SKIP] 无 assets'); return 0

    by_page = {}
    for p in asset_dir.iterdir():
        if p.suffix.lower() not in ('.jpg', '.jpeg', '.bmp', '.png', '.webp'):
            continue
        m = re.match(r'p(\d+)-(\d+)\.', p.name)
        if not m:
            continue
        by_page.setdefault(int(m.group(1)), []).append((int(m.group(2)), p.name))

    content = md_path.read_text(encoding='utf-8')
    pat = re.compile(r'^(## Page (\d+) / \d+)$', re.M)
    matches = list(pat.finditer(content))
    if not matches:
        print('  [REPAIR-SKIP] 无分页'); return 0

    fixed = 0
    segments = []
    for i, m in enumerate(matches):
        end = matches[i+1].start() if i+1 < len(matches) else len(content)
        pg = int(m.group(2))
        body = content[m.end():end]
        files = by_page.get(pg)
        if not files:
            segments.append((m.group(1), body))
            continue
        if re.search(r'!\[[^\]]*\]\([^\n]*?\.(?:jpe?g|png|bmp|webp|gif)\)', body):
            segments.append((m.group(1), body))      # 链接完整，不动
            continue
        # 剥掉旧的坏引用行，按序重建
        kept = [l for l in body.splitlines() if not l.lstrip().startswith('![')]
        text = '\n'.join(kept).strip()
        if text.startswith('*(本页无可抽取文本)*'):
            text = text.replace('*(本页无可抽取文本)*', '').strip()
        text = text.rstrip('-').strip()
        refs = '\n'.join(f'![page {pg} image {k}](assets/{book_stem}/{name})'
                         for k, name in sorted(files))
        segments.append((m.group(1), f'\n\n{text}\n\n{refs}\n\n---\n\n' if text else
                         f'\n\n{refs}\n\n---\n\n'))
        fixed += 1

    out = [content[:matches[0].start()]]
    for t, b in segments:
        out.append(t); out.append(b)
    md_path.write_text(''.join(out), encoding='utf-8')
    plog(f'  [REPAIR] {book_stem[:24]} 修复 {fixed} 页图片引用')
    return fixed


# ---------- 扫描版识别 ----------
def find_scanned_books():
    res = []
    for md in LIB.glob('*.md'):
        c = md.read_text(encoding='utf-8', errors='replace')
        pages = len(re.findall(r'^## Page \d+', c, re.M))
        if pages == 0: continue
        chars = 0
        for line in c.splitlines():
            s = line.strip()
            if not s or s == '---' or s.startswith('## Page '): continue
            if s == '*(本页无可抽取文本)*' or s.startswith('!['): continue
            if s.startswith(('source:','format:','共 ')): continue
            if s.startswith(('[','书名=','作者=','页数=')): continue
            chars += len(s)
        if chars / pages < 50:
            res.append(md)
    return res

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--merge-only', action='store_true')
    ap.add_argument('--repair', action='store_true', help='按 assets 重建坏掉的图片引用')
    args = ap.parse_args()

    if args.book:
        targets = [m for m in LIB.glob('*.md') if args.book in m.stem]
        if not targets: print('未找到'); return
    elif args.all:
        targets = find_scanned_books()
        print(f'找到 {len(targets)} 本扫描图像版')
    else:
        ap.print_help(); return

    if args.repair:
        for md in targets:
            repair_image_refs(md)
        return
    engine = None if args.merge_only else build_engine()
    for md in targets:
        print(f'\n=== {md.stem[:60]} ===')
        if not args.merge_only:
            process_book(md, engine, force=args.force)
        merge_to_md(md)
    print('\n全部完成。')

if __name__ == '__main__':
    main()
