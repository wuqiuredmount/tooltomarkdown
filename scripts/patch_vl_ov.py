# -*- coding: utf-8 -*-
"""把 paddleocr_vl_openvino 的旧版 API 引用改为新版（可重复执行）。

背景：该包（0.3.0）依赖 `openvino.runtime`，而 OpenVINO 2026 已移除该兼容层。
只涉及 3 处 import，替换后即可在 2026.x 上运行。

用法:
  .venvs/vlm/Scripts/python.exe scripts/patch_vl_ov.py
"""
import re
import sys
from pathlib import Path

import paddleocr_vl_openvino

PKG = Path(paddleocr_vl_openvino.__file__).parent
TARGET = PKG / 'paddleocr_vl' / 'ov_paddleocr_vl.py'

SUBS = [
    (r'from openvino\.runtime import Core, Type', 'from openvino import Core, Type'),
    (r'from openvino\.runtime\.passes import', 'from openvino.passes import'),
    (r'from openvino\.runtime import opset10 as ops', 'from openvino import opset10 as ops'),
]


def main():
    if not TARGET.exists():
        sys.exit(f'找不到目标文件: {TARGET}')
    src = TARGET.read_text(encoding='utf-8')
    out = src
    hits = []
    for pat, rep in SUBS:
        out, n = re.subn(pat, rep, out)
        if n:
            hits.append(f'{pat} -> {rep} ({n})')
    if out == src:
        print('[patch] 已是新 API，无需修改')
        return
    TARGET.write_text(out, encoding='utf-8')
    print(f'[patch] 修改 {TARGET.name}:')
    for h in hits:
        print('  ', h)


if __name__ == '__main__':
    main()
