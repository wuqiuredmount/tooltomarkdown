# -*- coding: utf-8 -*-
"""批量把 library 下所有 Markdown 导入为单页 JSON（自动识别 profile）。

通常在全量 OCR 跑完后使用。
用法:
  python scripts/batch_import.py
  python scripts/batch_import.py --force
"""
import sys
import argparse
import subprocess
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / 'library'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', action='store_true', help='覆盖已存在的 JSON')
    args = ap.parse_args()

    mds = [p for p in LIB.glob('*.md') if not p.name.startswith('.') and not 'bak' in p.name]
    print(f"找到 {len(mds)} 个 Markdown 文件，开始批量导入...\n")

    for md in mds:
        book = md.stem
        print(f"[{book}] 正在解析...")
        cmd = [sys.executable, str(ROOT / 'scripts' / 'import_md.py'), '--book', book]
        if args.force:
            cmd.append('--force')

        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
        
        if res.returncode == 0:
            out = res.stdout.strip()
            # 只提取核心行
            for line in out.splitlines():
                if '页数' in line or '跳过' in line or 'auto' in line or 'profile=' in line:
                    print(f"  {line.strip()}")
            print("  ✅ 成功\n")
        else:
            print(f"  ❌ 失败: {res.stderr.strip()}\n")

    print("全部处理完毕。运行 python scripts/review_ui.py 可以开启交互校对。")


if __name__ == '__main__':
    main()
