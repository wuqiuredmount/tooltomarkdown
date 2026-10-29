# -*- coding: utf-8 -*-
"""
项目轻量检索工具（省 Token 专用）。
用法:
  python scripts/project_search.py --q "关键字"          # 全局搜索 .md 和 .py
  python scripts/project_search.py --q "def main" --dir scripts  # 只在 scripts 下搜
"""
import os, argparse, re
from pathlib import Path

EXCLUDE_DIRS = {'.git', '.obsidian', 'library', 'assets', '__pycache__', 'node_modules'}
EXTS = {'.md', '.py', '.json', '.txt'}

def search_files(query, target_dir='.'):
    root = Path(target_dir)
    matches = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for file in filenames:
            ext = os.path.splitext(file)[1]
            if ext in EXTS:
                p = Path(dirpath) / file
                try:
                    with open(p, 'r', encoding='utf-8', errors='replace') as f:
                        lines = f.readlines()
                    for i, line in enumerate(lines):
                        if pattern.search(line):
                            start = max(0, i-1)
                            end = min(len(lines), i+2)
                            snippet = ''.join(lines[start:end])
                            matches.append(f"[{p.as_posix()}:{i+1}]\n{snippet.strip()}\n---")
                except Exception:
                    pass
    return matches

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--q', required=True, help='搜索关键字')
    ap.add_argument('--dir', default='.', help='搜索目录，默认全仓（跳过 library 等）')
    args = ap.parse_args()

    results = search_files(args.q, args.dir)
    if not results:
        print(f"未找到包含 '{args.q}' 的内容。")
        return

    print(f"找到 {len(results)} 处匹配（最多显示前 20 处）:\n")
    for r in results[:20]:
        print(r)
    if len(results) > 20:
        print(f"\n... 还有 {len(results)-20} 处未显示。请缩小搜索范围。")

if __name__ == '__main__':
    main()
