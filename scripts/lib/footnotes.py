# -*- coding: utf-8 -*-
"""脚注切分：多信号打分 + 正文对账，替代原来的单关键词正则。

原实现只有一个判据（行首标记 + 《|页|版社|第），两个方向都会错：
  - 正文里出现「参见《…》第 3 页」→ 正文被从中间切断；
  - 脚注不含上述字样（如「Ibid.」「op. cit.」）→ 整段丢失。

现改为 6 个信号加权，≥3 分才认定为脚注区；正文标记与脚注编号对不上时，
不静默合并，返回 status='review' 交由上层写 REVIEW 文件。

用法:
    from lib.footnotes import split
    r = split(got_text)
    r.body, r.footnotes, r.status, r.reason
"""
import re
from dataclasses import dataclass, field

CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
CIRCLED_IDX = {c: i + 1 for i, c in enumerate(CIRCLED)}

# 行首标记：① / (1) / （1） / [1] / 1. / 1、
MARKER_RE = re.compile(r'^\s*(?:([①-⑳])|[（(]\s*(\d{1,2})\s*[)）]|\[(\d{1,2})\]|(\d{1,2})\s*[.、])\s*')
CITE_HINT = re.compile(r'《[^》]{1,60}》|第\s*\d+[\d~\-—]*\s*页|\d{4}\s*年|出版社|Press|pp?\.\s*\d|Ibid|op\.\s*cit|参见|转引')


@dataclass
class FootnoteSplit:
    body: str
    footnotes: list = field(default_factory=list)
    score: int = 0
    status: str = 'none'          # ok / review / none
    reason: str = ''
    signals: dict = field(default_factory=dict)


def marker_of(line):
    """返回 (marker_value:int|None, rest:str)。"""
    m = MARKER_RE.match(line)
    if not m:
        return None, line
    for i, g in enumerate(m.groups()):
        if g:
            if i == 0:
                return CIRCLED_IDX.get(g), line[m.end():]
            return int(g), line[m.end():]
    return None, line


def body_markers(body: str):
    """正文中的上标标记：①、`$ ^{①} $`、`^1`，以及句末标点后的 `(1)`。"""
    found = []
    for c in body:
        if c in CIRCLED_IDX:
            found.append(CIRCLED_IDX[c])
    for m in re.finditer(r'\^\{?\s*(\d{1,2})\s*\}?', body):
        found.append(int(m.group(1)))
    # GOT/GLM 常在句末写成「过时了。(1)」——只认紧跟句读的括号数字，避免误吃年份
    for m in re.finditer(r'(?<=[。！？；，、”’])[（(]\s*(\d{1,2})\s*[)）]', body):
        found.append(int(m.group(1)))
    return found


def _trailing_block(lines):
    """从末尾往前收集连续的「标记行」，返回 (起始下标, 条目列表)。

    允许条目内部有换行续行（不含标记的行），但整体必须位于文本后 60%。
    """
    idx = len(lines) - 1
    while idx >= 0 and not lines[idx].strip():
        idx -= 1
    if idx < 0:
        return None, []

    end = idx
    items = []
    i = idx
    while i >= 0:
        val, rest = marker_of(lines[i])
        if val is not None:
            items.append((i, val, rest.strip()))
            i -= 1
            continue
        # 非标记行：可能是上一条的续行，允许 1 行；否则停止
        if items and i > 0 and marker_of(lines[i - 1])[0] is not None:
            i -= 1
            continue
        break
    if not items:
        return None, []
    items.reverse()
    start = items[0][0]
    return start, items


def split(text: str, *, layout=None, page_height=None, strict=False) -> FootnoteSplit:
    """切分正文与脚注。

    layout: 可选，Paddle 一类有 bbox 的工具传入 [{'text','y0','y1','size'}]，
            可额外启用纵向位置 / 分隔线 / 字号三个几何信号。
    """
    lines = text.splitlines()
    start, items = _trailing_block(lines)
    signals = {}
    score = 0

    if not items or start is None:
        # 没有尾部标记块：只有正文
        return FootnoteSplit(body=text.strip(), status='none',
                            reason='未发现尾部编号块', signals={'S4': 0})

    body = '\n'.join(lines[:start]).strip()
    foot_lines = [it[2] for it in items if it[2]]
    vals = [it[1] for it in items]

    # S4 编号序列（0–2）
    ascending = all(b >= a for a, b in zip(vals, vals[1:]))
    starts_low = vals[0] <= 3
    s4 = 2 if (ascending and starts_low and len(vals) >= 2) else (1 if ascending else 0)
    signals['S4'] = s4
    score += s4

    # S5 引文特征（0–1）
    s5 = 1 if sum(1 for l in foot_lines if CITE_HINT.search(l)) >= max(1, len(foot_lines) // 2) else 0
    signals['S5'] = s5
    score += s5

    # S6 与正文标记对账（0–2）
    bm = body_markers(body)
    bset, fset = set(bm), set(vals)
    if bset and fset and bset == fset:
        s6 = 2
    elif bset and fset and (bset <= fset or fset <= bset):
        s6 = 1
    elif not bset and not fset:
        s6 = 0
    else:
        s6 = 0
    signals['S6'] = s6
    score += s6

    # 几何信号（仅当传入 layout）
    reason_bits = []
    if layout:
        y0 = layout[0].get('y0', 0)
        if page_height and y0 > page_height * 0.65:
            signals['S1'] = 1
            score += 1
        sizes = [b.get('size', 0) for b in layout if b.get('size')]
        if sizes:
            median = sorted(sizes)[len(sizes) // 2]
            if median and max(sizes) / median > 1.15:
                signals['S3'] = 2
                score += 2
                reason_bits.append('脚注字号显著偏小')

    ok = score >= 3
    status = 'ok' if ok else ('review' if (strict or True) else 'none')
    if not ok:
        reason_bits.append(f'信号不足（{score} 分）')
    if bset and fset and bset != fset:
        status = 'review'
        reason_bits.append(f'标记不对账：正文 {sorted(bset)} / 脚注 {sorted(fset)}')

    return FootnoteSplit(body=body,
                         footnotes=_renumber(foot_lines),
                         score=score,
                         status=status,
                         reason='；'.join(reason_bits) or '多信号一致',
                         signals=signals)


def _renumber(items):
    """把条目统一成 ① ② ③ 形式（保持原顺序）。"""
    out = []
    for i, t in enumerate(items):
        t = t.strip()
        if not t:
            continue
        out.append(f'{CIRCLED[i]} {t}' if i < len(CIRCLED) else f'({i+1}) {t}')
    return out


if __name__ == '__main__':
    demo = '''正文第一段，讲某件事。①
正文第二段，参见《某书》第 3 页。②
① 《甲书》，1950，第 1 页。
② 《乙书》，1951，第 9 页。'''
    r = split(demo)
    print(r.status, r.score, r.signals)
    print('BODY:', r.body)
    print('FOOT:', r.footnotes)
