# -*- coding: utf-8 -*-
"""GOT-OCR 输出清洗：去汉字间多余空格 + LaTeX 残留转 Markdown。

GOT-OCR 2.0 的 `ocr_type='format'` 会产出两类噪声：
  1. 汉字被拆开并插入空格（如「那 著名」「统 地位」）；
  2. LaTeX 片段（如 `\\section{多次终结的现代性}`、`第 \\(165 \\sim 167\\) 页`）。

本模块把这些规整成可直接入库的中文 Markdown。

用法:
  python scripts/postprocess_got.py --in a.md --out b.md
  python scripts/postprocess_got.py --in a.md            # 打印到 stdout
"""
import re
import argparse
from pathlib import Path

CJK = r'\u4e00-\u9fff\u3400-\u4dbf'
CJK_PUNCT = '，。、；：！？）》”’」』…—～'
OPEN_PUNCT = '（《“‘「『'

# ---------- 1. 空格清洗 ----------
DASH_BETWEEN_CJK = re.compile(rf'(?<=[{CJK}])[ \t]*([-–—])[ \t]*(?=[{CJK}])')


def clean_cjk_spaces(text: str) -> str:
    t = text
    # 汉字之间
    t = re.sub(rf'(?<=[{CJK}])[ \t]+(?=[{CJK}])', '', t)
    # 汉字 ↔ 全角标点
    t = re.sub(rf'(?<=[{CJK}])[ \t]+(?=[{re.escape(CJK_PUNCT)}])', '', t)
    t = re.sub(rf'(?<=[{re.escape(OPEN_PUNCT)}])[ \t]+(?=[{CJK}])', '', t)
    # 汉字 ↔ 阿拉伯数字（「第 165 页」→「第165页」）
    t = re.sub(rf'(?<=[{CJK}])[ \t]+(?=\d)', '', t)
    t = re.sub(rf'(?<=\d)[ \t]+(?=[{CJK}])', '', t)
    # 汉字之间的连接号两侧去空格（「奥斯瓦尔德 - 斯本格勒」→「奥斯瓦尔德-斯本格勒」）
    t = DASH_BETWEEN_CJK.sub(r'\1', t)
    # 行尾空格、连续空格
    t = re.sub(r'[ \t]+$', '', t, flags=re.M)
    t = re.sub(r'[ \t]{2,}', ' ', t)
    return t


# ---------- 1.5 标点与连接号 ----------
PUNCT_MAP = {',': '，', ':': '：', ';': '；', '?': '？', '!': '！'}


def normalize_punct(text: str) -> str:
    """半角标点若紧邻汉字/中文语境，转全角；`165 ~ 167` 去空格。"""
    t = text
    for half, full in PUNCT_MAP.items():
        # 前一个字符是汉字或中文标点 → 全角（只吃空格/制表符，绝不能吃掉换行）
        t = re.sub(rf'(?<=[{CJK}{re.escape(CJK_PUNCT)}])[ \t]*{re.escape(half)}[ \t]*', full, t)
        # 后一个字符是汉字 → 全角（形如「, 宣告」）
        t = re.sub(rf'{re.escape(half)}[ \t]*(?=[{CJK}])', full, t)
    # 数字区间的波浪号两侧去空格：165 ~ 167 → 165~167
    t = re.sub(rf'(?<=[\d{CJK}])\s*~\s*(?=[\d{CJK}])', '~', t)
    return t


# ---------- 2. LaTeX → Markdown ----------
MATH_SYMBOLS = {
    r'\sim': '~', r'\times': '×', r'\leq': '≤', r'\geq': '≥',
    r'\le': '≤', r'\ge': '≥', r'\ldots': '…', r'\dots': '…',
    r'\cdot': '·', r'\%': '%', r'\&': '&', r'\_': '_', r'\#': '#',
    r'\alpha': 'α', r'\beta': 'β', r'\pi': 'π',
}


def latex_to_md(text: str) -> str:
    t = text
    # 圈号：\textcircled{1} / $\textcircled{1}$ → ①（必须在通用命令剥离之前）
    def _circ(m):
        n = int(m.group(1))
        return CIRCLED[n - 1] if 1 <= n <= len(CIRCLED) else f'({n})'
    t = re.sub(r'\\textcircled\{(\d+)\}', _circ, t)
    # 标题类
    t = re.sub(r'\\subsubsection\*?\{([^{}]*)\}', r'#### \1', t)
    t = re.sub(r'\\subsection\*?\{([^{}]*)\}', r'### \1', t)
    t = re.sub(r'\\section\*?\{([^{}]*)\}', r'## \1', t)
    t = re.sub(r'\\chapter\*?\{([^{}]*)\}', r'# \1', t)
    t = re.sub(r'\\title\*?\{([^{}]*)\}', r'# \1', t)
    # 行内强调
    t = re.sub(r'\\textbf\{([^{}]*)\}', r'**\1**', t)
    t = re.sub(r'\\textit\{([^{}]*)\}', r'*\1*', t)
    t = re.sub(r'\\emph\{([^{}]*)\}', r'*\1*', t)
    # 行间公式块标记
    t = t.replace(r'\[', '').replace(r'\]', '')
    # 行内公式：去掉 \( \) 外壳，命令换成字符
    def _inline(m):
        s = m.group(1)
        for k, v in MATH_SYMBOLS.items():
            s = s.replace(k, v)
        s = re.sub(r'\\[a-zA-Z]+', '', s)      # 剩余命令去掉反斜杠
        s = s.replace('{', '').replace('}', '')
        return s.strip()
    t = re.sub(r'\\\((.*?)\\\)', _inline, t)
    t = re.sub(r'\$(.*?)\$', _inline, t)
    # 残留命令（\quad 等）与转义
    t = re.sub(r'\\[a-zA-Z]+\s*', '', t)
    t = t.replace('\\', '')
    # 删除空标题行
    t = re.sub(r'^#{1,6}\s*$', '', t, flags=re.M)
    return t


# ---------- 3. 脚注标记统一 ----------
CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'


def normalize_markers(text: str) -> str:
    """把 `$ ^{①} $` / `(1)` / `[1]` 之类统一成 ①②③…（仅正文里的上标写法）。"""
    t = re.sub(r'\$[ \t]*\^[ \t]*\{?[ \t]*([①-⑳\d]+)[ \t]*\}?[ \t]*\$', r'\1', text)
    t = re.sub(r'\^[ \t]*\{?([①-⑳])\}?', r'\1', t)
    t = re.sub(r'\$[ \t]*\{?[ \t]*([①-⑳])[ \t]*\}?[ \t]*\$', r'\1', t)
    # 上标标记要紧贴前一个字（「过时了。 ①」→「过时了。①」）
    t = re.sub(r'[ \t]+(?=[①-⑳])', '', t)
    return t


def renumber_footnotes(lines):
    """把 `(1)`/`（1）`/`1.` 开头的脚注行重编号为 ①…，保持顺序。"""
    out, idx = [], 0
    for ln in lines:
        m = re.match(r'^\s*[（(]?(\d{1,2})[）)]?\s*[.、]?\s*(.+)$', ln)
        if m and idx < len(CIRCLED):
            out.append(f'{CIRCLED[idx]} {m.group(2).strip()}')
            idx += 1
        else:
            out.append(ln)
    return out


def clean(text: str) -> str:
    t = latex_to_md(text)
    t = normalize_markers(t)
    t = clean_cjk_spaces(t)
    t = normalize_punct(t)
    t = re.sub(r'\n{3,}', '\n\n', t)
    return t.strip() + '\n'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='inp', required=True)
    ap.add_argument('--out')
    args = ap.parse_args()

    raw = Path(args.inp).read_text(encoding='utf-8')
    out = clean(raw)
    if args.out:
        Path(args.out).write_text(out, encoding='utf-8')
        print(f'[CLEAN] {args.inp} → {args.out}（{len(raw)} → {len(out)} 字）')
    else:
        print(out)


if __name__ == '__main__':
    main()
