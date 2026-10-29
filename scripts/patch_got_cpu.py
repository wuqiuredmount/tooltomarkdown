# -*- coding: utf-8 -*-
"""把 GOT-OCR2.0 的官方实现改成设备无关，以便在 CPU 上推理（可重复执行）。

官方 `chat()` 硬编码 `.cuda()` 与 `torch.autocast("cuda", bfloat16)`，在纯 CPU 版
torch 上直接报 "Torch not compiled with CUDA enabled"。这里做两处等价替换：
  .cuda()                                -> .to(self.device)
  torch.autocast("cuda", bfloat16)       -> torch.no_grad()（CPU 不做 bf16 自动混合）

用法: .venvs/got/Scripts/python.exe scripts/patch_got_cpu.py
"""
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = [ROOT / 'models' / 'GOT-OCR2_0' / 'modeling_GOT.py']
CACHE = Path.home() / '.cache' / 'huggingface' / 'modules' / 'transformers_modules'
TARGETS += list(CACHE.glob('GOT*/modeling_GOT.py'))


def patch(path: Path) -> int:
    s = path.read_text(encoding='utf-8')
    orig = s
    s = s.replace('.cuda()', '.to(self.device)')
    s = re.sub(r'torch\.autocast\("cuda",\s*dtype=torch\.bfloat16\)', 'torch.no_grad()', s)
    # CPU 上权重是 fp32，输入若 .half() 会报 dtype 不一致
    s = s.replace('.half()', '.float()')
    if s != orig:
        path.write_text(s, encoding='utf-8')
        for pc in path.parent.glob('__pycache__'):
            shutil.rmtree(pc, ignore_errors=True)
        return len(re.findall(r'\.to\(self\.device\)', s))
    return 0


if __name__ == '__main__':
    for p in TARGETS:
        if p.exists():
            n = patch(p)
            print(f'[patch] {p} -> {n} 处设备化')
        else:
            print(f'[skip] 不存在 {p}')
