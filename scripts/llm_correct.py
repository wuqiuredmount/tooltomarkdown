# -*- coding: utf-8 -*-
"""大模型定点修改引擎：一句话指令 → 改单页 JSON → 重写 MD → 刷新 DOCX。

三种引擎（用 `--engine` 选，默认 auto）：
  cloud  : OpenAI 兼容云端接口（qwen-vl / DeepSeek 等），需 VLM_API_KEY
  ollama : 本机 Ollama（OpenAI 兼容 /v1/chat/completions），无需密钥
  agent  : 本会话主模型（DeepSeek）。脚本产出「修改请求包」→ 主模型改 →
           用 `--apply` 回灌。用于没有云端密钥、但要让强模型动笔的场景。

引擎 auto 的顺序：cloud（有密钥）→ ollama（服务在）→ 报错并提示用 agent。

安全约束：
  * 原文完整传入，要求「除指定处外逐字保留」；
  * 输出若短于原文 60% 或有明显前言（「修改后：」）→ 判为不合格，拒绝写入；
  * 每次修改前把原 JSON 备份到 `outputs/corrections/backup/`；
  * 变更摘要（difflib）写入 `*.diff.md`，并记入该页 JSON 的 history。

用法:
  # 列出可编辑的页
  python scripts/llm_correct.py --book 示例书名 --list

  # 云端/本地模型直接改（自动重建 MD+DOCX）
  python scripts/llm_correct.py --book 示例书名 --page 306 \
      --prompt "本页『据为』疑为 OCR 误识别，应为『认为』"

  # 主模型改（agent 引擎）：先出请求包，改完回灌
  python scripts/llm_correct.py --book ... --page 306 --prompt "..." --engine agent
  python scripts/llm_correct.py --book ... --page 306 --apply outputs/corrections/xxx.answer.md

  # 给了原图：用视觉模型整页重识别（模块三）
  python scripts/llm_correct.py --book 示例书名 --page 12 --image --prompt "整页重识别"
"""
import re
import os
import sys
import json
import time
import shutil
import difflib
import argparse
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from lib import store as S

LIB = ROOT / 'library'
WORK = ROOT / 'outputs' / 'corrections'
BACKUP = WORK / 'backup'
PY = sys.executable

SYSTEM_PROMPT = """你是中文学术书稿的资深校对编辑。用户会给你「某页原文全文」和一条「修改建议」。

铁律：
1. 只修改建议明确指出的问题，其余文字必须**逐字保留**（包括标点、圈号①②③、脚注行、换行）。
2. 不得改写文风、不得增删段落、不得"顺手润色"、不得合并或拆分句子。
3. 脚注行（以 > 或 ①②③ 开头）保持原有顺序与编号，不得重排。
4. 直接输出修改后的**整页纯文本**，不要任何解释、不要 Markdown 代码块、不要"修改后："之类的前缀。
5. 如果建议指向的问题在原文中找不到，原样返回原文，不要臆造。"""


# ---------------- 页定位 ----------------
def resolve_page(book: str, page: int, physical=False):
    """页码解析：默认按「页内印刷页码」，歧义时取正文页；physical=True 则按物理序号。"""
    recs = S.load_book(book)
    if not recs:
        raise SystemExit(f'没有单页 JSON：{book}\n先运行 python scripts/import_md.py --book "{book}"')

    if physical:
        by_phys = [r for r in recs if r.get('physical_index') == page]
        if not by_phys:
            raise SystemExit(f'找不到物理序号 p{page}（共 {len(recs)} 页）')
        return by_phys[0]

    hits = [r for r in recs if r.get('real_page_num') == page]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        body = [r for r in hits if r.get('section_type') == 'body']
        pick = (body or hits)[0]
        print(f'  [WARN] 页码 {page} 有 {len(hits)} 页命中（目录/正文各一套编号），'
              f'取 {pick["page_key"]}（物理 p{pick["physical_index"]}）')
        return pick
    by_phys = [r for r in recs if r.get('physical_index') == page]
    if by_phys:
        print(f'  [WARN] 没有印刷页码为 {page} 的页，按物理序号 p{page} 处理')
        return by_phys[0]
    raise SystemExit(f'找不到第 {page} 页（共 {len(recs)} 页）')


# ---------------- 引擎 ----------------
def _post_json(url, payload, headers, timeout=180):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', **headers}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def call_cloud(text: str, prompt: str):
    key = os.environ.get('VLM_API_KEY') or os.environ.get('LLM_API_KEY')
    if not key:
        return None
    base = os.environ.get('VLM_API_BASE', 'https://dashscope.aliyuncs.com/compatible-mode/v1').rstrip('/')
    model = os.environ.get('VLM_API_MODEL', 'qwen-vl-max')
    timeout = int(os.environ.get('VLM_API_TIMEOUT', '180'))
    data = _post_json(f'{base}/chat/completions', {
        'model': model, 'temperature': 0,
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                     {'role': 'user', 'content': f'【原文】\n{text}\n\n【修改建议】\n{prompt}'}],
    }, {'Authorization': f'Bearer {key}'}, timeout)
    return data['choices'][0]['message']['content']


def ollama_alive():
    try:
        with urllib.request.urlopen('http://127.0.0.1:11434/api/tags', timeout=2) as r:
            return bool(json.loads(r.read().decode('utf-8')).get('models'))
    except Exception:
        return False


def call_ollama(text: str, prompt: str, model=None):
    if not ollama_alive():
        return None
    model = model or os.environ.get('OLLAMA_MODEL', 'qwen2.5-coder:7b')
    data = _post_json('http://127.0.0.1:11434/v1/chat/completions', {
        'model': model, 'temperature': 0, 'stream': False,
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT},
                     {'role': 'user', 'content': f'【原文】\n{text}\n\n【修改建议】\n{prompt}'}],
    }, {}, timeout=600)
    return data['choices'][0]['message']['content']


# ---------------- 输出清洗与校验 ----------------
def clean_answer(raw: str) -> str:
    t = raw.strip()
    t = re.sub(r'^```[a-zA-Z]*\s*\n', '', t)
    t = re.sub(r'\n```\s*$', '', t)
    t = re.sub(r'^\s*(修改后(的)?(全文|文本|内容)?\s*[:：]\s*)', '', t)
    return t.strip()


def validate(old: str, new: str):
    """返回 (ok, reason)。防止模型偷工减料或大改。"""
    if not new.strip():
        return False, '输出为空'
    ratio = len(new) / max(1, len(old))
    if ratio < 0.6:
        return False, f'输出长度仅为原文 {ratio:.0%}，疑似截断或擅自删改'
    if ratio > 1.6:
        return False, f'输出长度达原文 {ratio:.0%}，疑似扩写'
    # 脚注行数必须一致
    n_old = sum(1 for l in old.splitlines() if re.match(r'^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]', l.strip()))
    n_new = sum(1 for l in new.splitlines() if re.match(r'^[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳]', l.strip()))
    if n_old and n_old != n_new:
        return False, f'脚注行数从 {n_old} 变为 {n_new}，拒绝写入'
    return True, 'ok'


def diff_summary(old: str, new: str, max_items=8):
    """按句/段输出变更摘要，用于审计「只改了指定处」。"""
    a = [x for x in re.split(r'(?<=[。！？；])', old) if x.strip()]
    b = [x for x in re.split(r'(?<=[。！？；])', new) if x.strip()]
    sm = difflib.SequenceMatcher(None, a, b)
    items = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        items.append({
            'op': tag,
            'old': ''.join(a[i1:i2])[:200],
            'new': ''.join(b[j1:j2])[:200],
        })
        if len(items) >= max_items:
            break
    return items


# ---------------- 回写 ----------------
def apply_text(book, rec, new_text, prompt, engine, rebuild=True):
    old_text = rec.get('text') or ''
    ok, why = validate(old_text, new_text)
    if not ok:
        print(f'  [REJECT] {why}')
        return False

    phys = rec['physical_index']
    BACKUP.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    src = S.page_path(book, phys)
    if src.exists():
        shutil.copy2(src, BACKUP / f'p{phys:04d}.{stamp}.json')

    diffs = diff_summary(old_text, new_text)
    rec['text'] = new_text.strip()
    rec['status'] = 'corrected'
    rec['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    rec['meta'] = {'chars': len(rec['text']),
                   'changed_segments': len(diffs)}
    rec.setdefault('history', []).append({
        'at': rec['updated_at'], 'engine': engine, 'prompt': prompt,
        'chars_before': len(old_text), 'chars_after': len(rec['text']),
        'changed_segments': len(diffs),
    })
    S.atomic_write(src, rec)

    print(f'  [OK] {rec["page_key"]}（物理 p{phys} / 印刷页 {rec.get("real_page_num")}）')
    print(f'       字数 {len(old_text)} → {len(rec["text"])} | 变更片段 {len(diffs)}')

    WORK.mkdir(parents=True, exist_ok=True)
    diff_md = WORK / f'p{phys:04d}.{stamp}.diff.md'
    lines = [f'# 修改记录 {book} p{phys}', '',
             f'- 引擎：{engine}', f'- 指令：{prompt}',
             f'- 时间：{rec["updated_at"]}',
             f'- 字数：{len(old_text)} → {len(rec["text"])}', '', '## 变更片段', '']
    for d in diffs:
        lines += [f'**{d["op"]}**', '', '```diff', f'- {d["old"]}', f'+ {d["new"]}', '```', '']
    diff_md.write_text('\n'.join(lines), encoding='utf-8')
    print(f'       审计：{diff_md.relative_to(ROOT)}')

    # 回写 Markdown：只替换这一页，其余字节不动
    md_path = LIB / f'{book}.md'
    if md_path.exists():
        try:
            bak, pageno = patch_md_page(md_path, phys, rec['text'])
            print(f'  [MD] 已换页 p{phys}（页脚 · {pageno} ·），备份 {bak.name}')
        except SystemExit as e:
            print(f'  [MD] 跳过换页：{e}')

    if rebuild:
        rebuild_outputs(book)
    return True


def patch_md_page(md_path: Path, phys: int, new_text: str):
    """外科式换页：只替换该页正文，分页符/页码标签/其余字节原样保留。

    具体分页规则由 `lib.bookfmt` 按 `config/book_profiles.json` 的 profile 决定，
    因此换任何书都不用改这里。
    """
    from lib import bookfmt as F
    md_text = md_path.read_text(encoding='utf-8')
    pname, prof = F.get_profile(None, md_text)
    new_md, pageno = F.patch_page(md_text, prof, phys, new_text)
    backup = md_path.with_suffix(f'.bak-{time.strftime("%Y%m%d%H%M%S")}.md')
    shutil.copy2(md_path, backup)
    md_path.write_text(new_md, encoding='utf-8')
    return backup, pageno


def rebuild_outputs(book):
    """整本重排 DOCX（Markdown 已在 apply_text 里外科式换页）。"""
    import subprocess
    print('  [REBUILD] DOCX ...')
    r = subprocess.run([PY, str(ROOT / 'scripts' / 'md_to_docx.py'), '--book', book],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    tail = [l for l in (r.stdout or '').strip().splitlines() if l.startswith('[DOCX]')]
    for l in tail:
        print('   ', l)
    if r.returncode != 0:
        err = (r.stderr or '').strip().splitlines()
        print(f'   [FAIL] DOCX 重建失败（exit={r.returncode}）：{err[-1] if err else "unknown"}')
        return False
    return True


def rebuild_via_image(book, rec, prompt):
    """模块三：有原图时用视觉模型整页重识别。"""
    imgs = rec.get('images') or []
    if not imgs:
        raise SystemExit('该页没有关联图片，无法整页重识别（assets 里找不到）')
    img = ROOT / 'library' / imgs[0]
    if not img.exists():
        raise SystemExit(f'图片不存在：{img}')
    import subprocess
    r = subprocess.run([PY, str(ROOT / 'scripts' / 'api_vlm.py'),
                        '--image', str(img), '--tag', f'reocr_p{rec["physical_index"]}'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = ROOT / 'tmp' / '_api_vlm_out' / f'reocr_p{rec["physical_index"]}.md'
    if not out.exists():
        raise SystemExit(f'视觉重识别失败：{(r.stdout or r.stderr)[-300:]}')
    return out.read_text(encoding='utf-8')


# ---------------- 交互/请求包 ----------------
def emit_request(book, rec, prompt):
    WORK.mkdir(parents=True, exist_ok=True)
    phys = rec['physical_index']
    req = WORK / f'p{phys:04d}.request.md'
    ans = WORK / f'p{phys:04d}.answer.md'
    body = [f'# 定点修改请求：{book} p{phys}', '',
            f'- 印刷页码：{rec.get("real_page_num")}',
            f'- 分区：{rec.get("section_type")} / {rec.get("page_key")}',
            f'- 指令：{prompt}', '',
            '## 系统约束', '', '```', SYSTEM_PROMPT, '```', '',
            '## 原文（整页）', '', '```text', rec.get('text', ''), '```', '',
            '## 交付方式', '',
            f'把修改后的整页纯文本写入：`{ans.relative_to(ROOT)}`，然后执行：', '',
            f'```bash\npython scripts/llm_correct.py --book "{book}" --physical {phys} '
            f'--apply "{ans.relative_to(ROOT)}" --prompt "{prompt}"\n```']
    req.write_text('\n'.join(body), encoding='utf-8')
    print(f'  [AGENT] 请求包：{req.relative_to(ROOT)}')
    print(f'  [AGENT] 请把修改后的整页文本写入：{ans.relative_to(ROOT)}')
    print(f'  [AGENT] 再执行 --apply 回灌（会自动重建 MD 与 DOCX）')
    return 3


def list_pages(book, limit=40):
    recs = S.load_book(book)
    if not recs:
        raise SystemExit(f'没有单页 JSON：{book}')
    from collections import Counter
    st = Counter(r.get('status') for r in recs)
    print(f'{book}：{len(recs)} 页 | {dict(st)}')
    print(f'{"物理":>5} {"印刷":>5} {"分区":<6} {"字数":>6}  开头')
    rows = [r for r in recs if (r.get('text') or '').strip()]
    for r in rows[:limit]:
        head = re.sub(r'\s+', ' ', (r.get('text') or ''))[:36]
        print(f'{r["physical_index"]:>5} {str(r.get("real_page_num")):>5} '
              f'{r.get("section_type", ""):<6} {r["meta"].get("chars", 0):>6}  {head}')
    if len(rows) > limit:
        print(f'… 共 {len(rows)} 页有文本（用 --list 配合 --limit 查看更多）')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--book', required=True)
    ap.add_argument('--page', type=int, help='页号：默认按页内印刷页码')
    ap.add_argument('--physical', type=int, help='物理序号（与 assets/p<N>-* 一致，无歧义）')
    ap.add_argument('--prompt', default='')
    ap.add_argument('--engine', choices=['auto', 'cloud', 'ollama', 'agent'], default='auto')
    ap.add_argument('--apply', help='把该文件内容作为修改结果回灌')
    ap.add_argument('--image', action='store_true', help='用视觉模型整页重识别后覆盖')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--limit', type=int, default=40)
    ap.add_argument('--no-rebuild', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if args.list:
        list_pages(args.book, args.limit)
        return

    if args.page is None and args.physical is None:
        ap.error('需要 --page / --physical（或 --list）')

    if args.physical is not None:
        rec = resolve_page(args.book, args.physical, physical=True)
    else:
        rec = resolve_page(args.book, args.page)
    old = rec.get('text') or ''
    if not old.strip():
        raise SystemExit(f'{rec["page_key"]} 无文本（status={rec.get("status")}），先用 OCR 管线填充')
    print(f'页面：{args.book} p{rec["physical_index"]}（印刷页 {rec.get("real_page_num")}）'
          f' | {len(old)} 字 | status={rec.get("status")}')

    # 回灌模式
    if args.apply:
        new = clean_answer(Path(args.apply).read_text(encoding='utf-8'))
        ok = apply_text(args.book, rec, new, args.prompt or '(来自请求包)',
                        'agent(DeepSeek)', rebuild=not args.no_rebuild)
        sys.exit(0 if ok else 1)

    # 整页重识别
    if args.image:
        new = clean_answer(rebuild_via_image(args.book, rec, args.prompt))
        ok = apply_text(args.book, rec, new, args.prompt or '整页重识别',
                        'api_vlm', rebuild=not args.no_rebuild)
        sys.exit(0 if ok else 1)

    if not args.prompt:
        ap.error('需要 --prompt（或 --apply / --image）')

    if args.dry_run:
        diff = diff_summary(old, old)
        print('  [DRY] 引擎顺序：cloud → ollama → agent')
        print(f'  [DRY] 上下文 {len(old)} 字；指令：{args.prompt}')
        return

    engine = args.engine
    order = ['cloud', 'ollama'] if engine == 'auto' else [engine]
    for eng in order:
        try:
            if eng == 'cloud':
                out = call_cloud(old, args.prompt)
                if out is None:
                    print('  [SKIP] 无 VLM_API_KEY')
                    continue
            elif eng == 'ollama':
                out = call_ollama(old, args.prompt)
                if out is None:
                    print('  [SKIP] 本机 Ollama 未运行或无模型')
                    continue
            else:
                sys.exit(emit_request(args.book, rec, args.prompt))
            new = clean_answer(out)
            print(f'  [ENGINE] {eng} 返回 {len(new)} 字')
            ok = apply_text(args.book, rec, new, args.prompt, eng,
                            rebuild=not args.no_rebuild)
            sys.exit(0 if ok else 1)
        except (urllib.error.URLError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            print(f'  [FAIL] {eng}: {type(e).__name__}: {str(e)[:120]}')
            continue

    print('  [AGENT] 云端与本地模型均不可用 → 转主模型模式')
    sys.exit(emit_request(args.book, rec, args.prompt))


if __name__ == '__main__':
    main()
