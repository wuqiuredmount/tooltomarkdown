# -*- coding: utf-8 -*-
"""图书版式的统一解析层（配置驱动，换书不改代码）。

所有「分页 / 页码 / 分区 / 回写」的格式差异都收敛到 `config/book_profiles.json`。
本模块只做一件事：把一份 Markdown 按 profile 切成结构化的页，并提供
「外科式换页」与「整本重建」两个原语。

支持的分页/页码形态（见配置）：
  center-pageno : `<p align="center">· 12 ·</p>` 位于分页符之后（= 上一页的页脚）
  formfeed      : `\f` 分页，页码在页尾独立成行
  page-heading  : `## Page 12 / 300` 作为页眉
  bare          : 只有 `---`，没有页码（不猜）

术语：
  phys     物理页序号（1 起，与 assets/p<N>-* 对齐）
  number   页内印刷页码（可能重复：目录 1–4 与正文 1–715 各成体系）
  label    页码原文写法（`· 12 ·`），用于回写时保持原样
"""
import re
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / 'config' / 'book_profiles.json'

_CACHE = {}


# ---------------- 配置 ----------------
def load_config(path=None):
    p = Path(path) if path else DEFAULT_CONFIG
    key = str(p)
    if key in _CACHE:
        return _CACHE[key]
    if not p.exists():
        raise SystemExit(f'配置文件不存在：{p}')
    cfg = json.loads(p.read_text(encoding='utf-8'))
    _CACHE[key] = cfg
    return cfg


def docx_style(cfg=None):
    """DOCX 版式参数（供 md_to_docx 使用）。"""
    cfg = cfg or load_config()
    return cfg.get('docx', {})


def _compiled(prof):
    """把 profile 里的字符串编译成正则，结果缓存在 profile 内（'__re' 键）。"""
    if '__re' in prof:
        return prof['__re']
    brk = [re.compile(p, re.M) for p in prof.get('break', [])]
    mk = prof.get('marker') or {}
    mk_re = re.compile(mk['pattern'], re.M) if mk.get('pattern') else None
    nl = prof.get('number_line')
    nl_re = re.compile(nl) if nl else None
    clean = [(re.compile(p, re.S | re.I), p) for p in prof.get('body_clean', [])]
    sec = {k: [re.compile(p, re.I) for p in v]
           for k, v in (prof.get('sections') or {}).items()}
    prof['__re'] = {'break': brk, 'marker': mk_re, 'number_line': nl_re,
                    'clean': clean, 'sections': sec}
    return prof['__re']


# ---------------- 自动识别 ----------------
def guess_profile(md_text, cfg=None):
    """按「标记命中数」自动选 profile，返回 name。

    不是猜风格，而是实测哪种页标记在文中真实存在：
      center-pageno 看 `<p align=center>` 且内含数字
      page-heading  看 `## Page N / total`
      formfeed      看 \\f 或「短行纯页码」
    命中为 0 时退回 bare（不猜页码，只按 --- 分页）。
    """
    cfg = cfg or load_config()
    text = md_text

    def count(pat, flags=re.M):
        return len(re.findall(pat, text, flags))

    score = {
        'page-heading': count(r'^##\s*Page\s+\d+\s*/\s*\d+\s*$') * 3,
        'center-pageno': count(r'<p\s+align="center"\s*>\s*[·•・]?\s*\d{1,4}\s*[·•・]?\s*</p>') * 3,
        'formfeed': count(r'\f', 0) * 3 + count(r'^\s*[·•]\s*\d{1,4}\s*[·•]\s*$'),
    }
    best = max(score, key=score.get)
    if score[best] == 0:
        return 'bare'
    return best


def get_profile(name=None, md_text=None, cfg=None):
    """返回 (name, profile)。name=None/'auto' 时用 md 内容判定（其次用配置的 active_profile）。"""
    cfg = cfg or load_config()
    profs = cfg['profiles']
    if name in (None, 'auto', ''):
        name = cfg.get('active_profile', 'auto')
        if name in (None, 'auto', ''):
            name = guess_profile(md_text, cfg) if md_text else 'bare'
    if name not in profs:
        raise SystemExit(f'未知 profile：{name}（可选：{", ".join(profs)}）')
    return name, profs[name]


# ---------------- 分页解析 ----------------
def _clean_body(text, prof):
    r = _compiled(prof)
    t = text
    if r['marker']:                 # 先剔除页标记，避免它被 body_clean 当普通居中块保留
        t = r['marker'].sub('', t)
    for pat, _ in r['clean']:
        t = pat.sub(lambda m: (m.groupdict().get('inner') or '').strip()
                    if 'inner' in m.groupdict() else '', t)
    for pat in r['break']:
        t = pat.sub('', t)
    # 压缩空行，但保留原稿的段间空行数（默认最多 2 个空行）
    max_blank = int(prof.get('max_blank_lines', 2))
    limit = max_blank + 1
    return re.sub(r'\n{%d,}' % (limit + 1), '\n' * limit, t).strip()


def _mark_info(m, prof):
    """从页标记里取 (number, label)。"""
    if m is None:
        return None, None
    gd = m.groupdict()
    label = (gd.get('label') or '').strip() or None
    num = None
    if gd.get('n'):
        num = int(gd['n'])
    elif label:
        pat = (prof.get('marker') or {}).get('number_pattern')
        if pat:
            mm = re.search(pat, label)
            if mm:
                num = int(mm.group(1))
        if num is None:
            mm = re.search(r'(\d{1,4})', label)
            if mm:
                num = int(mm.group(1))
    if label is None and num is not None:
        label = prof.get('label_fmt', '· {n} ·').format(n=num)
    return num, label


def _number_from_body(text, prof):
    """页脚页码在正文首尾独立成行时，取出 (number, label) 并返回去掉该行后的正文。"""
    r = _compiled(prof)
    if not r['number_line']:
        return None, None, text
    lines = text.splitlines()
    idx = [i for i, l in enumerate(lines) if l.strip()]
    if not idx:
        return None, None, text
    for pos in (idx[-1], idx[0]):          # 先看页尾，再看页首
        m = r['number_line'].match(lines[pos].strip())
        if m:
            label = lines[pos].strip()
            body = '\n'.join(lines[:pos] + lines[pos + 1:])
            return int(m.group('n')), label, body
    return None, None, text


def pages(md_text, prof, skip_title=True):
    """切成页。返回 [{phys, number, label, text, start, end}]。

    start/end 是该页**正文**在原始 md 中的字符区间（不含页码标记本身），
    因此可以被 `patch_page` 用来做「外科式换页」而不伤到相邻页的页码标签。
    """
    r = _compiled(prof)
    mk = prof.get('marker') or {}
    head_end = 0
    if skip_title:
        m = re.match(r'\s*#\s+[^\n]*\n', md_text)
        if m:
            head_end = m.end()

    marks = list(r['marker'].finditer(md_text)) if r['marker'] else []
    belongs = mk.get('number_belongs', 'previous')

    # ---- 无页标记：只按分页符切，页码从正文首尾行取 ----
    if not marks:
        cuts = sorted({m.start() for p in r['break'] for m in p.finditer(md_text)})
        spans, prev = [], head_end
        for c in cuts:
            spans.append((prev, c))
            prev = c
        spans.append((prev, len(md_text)))
        out = []
        for i, (s, e) in enumerate(spans, 1):
            body = _clean_body(md_text[s:e], prof)
            num, label, body = _number_from_body(body, prof)
            if not body and not label:
                continue
            out.append({'phys': len(out) + 1, 'number': num, 'label': label,
                        'text': body, 'start': s, 'end': e})
        return out

    # ---- 有页标记：正文区间 = 前一个标记之后 .. 本页所属标记之前 ----
    spans, nums = [], []
    if belongs == 'previous':
        # 标记是「它前面那一页」的页脚：正文在 前一标记之后 ~ 本标记之前
        prev_end = head_end
        for i, m in enumerate(marks):
            spans.append((prev_end, m.start()))
            nums.append(m)
            prev_end = m.end()
        if md_text[prev_end:].strip():          # 末页之后若还有正文（无页脚）
            spans.append((prev_end, len(md_text)))
            nums.append(None)
    else:
        # 标记是页眉：正文在 本标记之后 ~ 下一标记之前
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(md_text)
            spans.append((m.end(), end))
            nums.append(m)

    out = []
    for (s, e), m in zip(spans, nums):
        num, label = _mark_info(m, prof)
        body = _clean_body(md_text[s:e], prof)
        if mk.get('number_from') == 'body':
            nb, lb, body = _number_from_body(body, prof)
            num, label = (nb if nb is not None else num), (lb or label)
        if not body and not label:
            continue
        out.append({'phys': len(out) + 1, 'number': num, 'label': label,
                    'text': body, 'start': s, 'end': e})
    return out


def page_texts(md_text, prof):
    """[(label|None, text)]，供渲染/导入使用。"""
    return [(p['label'], p['text']) for p in pages(md_text, prof)]


# ---------------- 回写原语 ----------------
def patch_page(md_text, prof, phys, new_text):
    """外科式换页：只替换「真正变化的那一段」。

    保留区一律不动：页首空行、页尾空行、页尾分页线（`---`）、相邻页的页码标签。
    正文基线取 md 中的现有文本（而非 JSON），因此 JSON 的空白规范化不会污染原稿。
    """
    ps = pages(md_text, prof)
    if not (1 <= phys <= len(ps)):
        raise SystemExit(f'该 md 只有 {len(ps)} 页，找不到物理页 {phys}')

    page = ps[phys - 1]
    s, e = page['start'], page['end']
    seg = md_text[s:e]

    # 1) 页尾：先剥连续换行，再把「页尾分页线」并入保留区
    n_nl = len(seg) - len(seg.rstrip('\n'))
    body, tail = seg[:len(seg) - n_nl], seg[len(seg) - n_nl:]
    sep = re.search(r'\n\s*-{2,}\s*$', body)
    if sep:
        tail = body[sep.start():] + tail
        body = body[:sep.start()]

    # 2) 页首：连续换行
    lead = body[:len(body) - len(body.lstrip('\n'))]
    core = body[len(lead):]

    # 3) 最小差异替换
    new = new_text.strip()
    i = 0
    lim = min(len(core), len(new))
    while i < lim and core[i] == new[i]:
        i += 1
    j = 0
    while j < min(len(core) - i, len(new) - i) and core[-1 - j] == new[-1 - j]:
        j += 1
    patched = core[:i] + new[i:len(new) - j] + (core[len(core) - j:] if j else '')

    return md_text[:s] + lead + patched + tail + md_text[e:], page.get('number')


def build_markdown(records, prof, title):
    """由单页 JSON 重建整本 Markdown（按 profile 的模板）。"""
    tpl = (prof.get('build') or {})
    out = [tpl.get('title', '# {title}\n\n').format(title=title)]
    for r in records:
        text = (r.get('text') or '').strip()
        if r.get('status') not in ('success', 'corrected') or not text:
            text = '*(本页内容待补充)*'
        num = r.get('real_page_num')
        label = r.get('real_page_label') or (f'· {num} ·' if num else '')
        page_tpl = tpl.get('page', '{text}\n\n---\n\n')
        out.append(page_tpl.format(text=text, n=num if num is not None else '',
                                   total=r.get('meta', {}).get('total', ''),
                                   label=label))
    return ''.join(out).rstrip() + '\n'


# ---------------- 分区 ----------------
def detect_sections(pages_list, prof, front_ratio=0.06):
    """判定 front / toc / body，并写入 section_type / page_key。

    规则：
      1. 命中 toc 正则的连续页 → toc（允许中间夹 1 页未命中，容忍目录续页）
      2. toc 之前 → front
      3. 无 toc 时，前 front_ratio 页里命中 front 正则的页 → front
      4. 其余 → body
    """
    r = _compiled(prof)
    sec = r['sections']
    n = len(pages_list)
    types = ['body'] * n

    toc_hit = [i for i, p in enumerate(pages_list)
               if any(x.search(p['text'][:400]) for x in sec.get('toc', []))]
    if toc_hit:
        start = min(toc_hit)
        miss = 0
        for i in range(start, n):
            t = pages_list[i]['text'][:400]
            if any(x.search(t) for x in sec.get('toc', [])) or re.search(r'\.{3,}\s*\d+', t):
                types[i] = 'toc'
                miss = 0
            else:
                miss += 1
                if miss >= 2:
                    break
        for i in range(start):
            types[i] = 'front'
    else:
        limit = max(1, int(n * front_ratio))
        for i in range(min(limit, n)):
            if any(x.search(pages_list[i]['text'][:200]) for x in sec.get('front', [])):
                types[i] = 'front'

    counters = {'front': 0, 'toc': 0, 'body': 0}
    for i, p in enumerate(pages_list):
        st = types[i]
        counters[st] += 1
        width = 2 if st in ('front', 'toc') else 3
        p['section_type'] = st
        p['page_key'] = f'{st}_{counters[st]:0{width}d}'
    return pages_list


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        raise SystemExit('用法: python scripts/lib/bookfmt.py <markdown文件> [profile]')
    md = Path(sys.argv[1]).read_text(encoding='utf-8')
    name, prof = get_profile(sys.argv[2] if len(sys.argv) > 2 else None, md)
    ps = pages(md, prof)
    detect_sections(ps, prof)
    print(f'profile={name} | 页数 {len(ps)}')
    for p in ps[:3] + ps[-2:]:
        print(f"  p{p['phys']} number={p['number']} label={p['label']!r} "
              f"{p.get('page_key')} {len(p['text'])}字 | {p['text'][:36]!r}")
