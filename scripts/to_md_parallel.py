#!/usr/bin/env python3
"""to_md_parallel.py - 并行版 to_md（控制并发，避免卡机）

用法:
    python scripts/to_md_parallel.py            # 默认 4 并发
    python scripts/to_md_parallel.py -j 3       # 指定并发数
    python scripts/to_md_parallel.py --keep     # 保留源文件
    python scripts/to_md_parallel.py --dry-run  # 只报告

设计:
- 以「文件」为并行粒度（单个 PDF 内部不拆页），避免图像导出目录互相踩踏。
- 默认并发 4：本机 16 逻辑核 / 32GB 内存下留足余量，CPU 与内存占用温和。
- 大文件优先（按体积降序），减少尾部长任务拖时间。
- 单文件失败不影响其他文件；失败项保留在 newfiles/。
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import to_md  # noqa: E402


def _work(task):
    src_str, out_str, dry = task
    src = Path(src_str)
    out_dir = None if dry else Path(out_str)
    try:
        body, fmt = to_md.convert(src, out_dir)
        if not dry:
            target = Path(out_str) / (src.stem + ".md")
            target.write_text(
                to_md.front_matter(src, fmt) + body.rstrip() + "\n",
                encoding="utf-8",
            )
        return (str(src.relative_to(to_md.INBOX)), True, len(body), "")
    except Exception as exc:  # noqa: BLE001
        return (
            str(src.relative_to(to_md.INBOX)),
            False,
            0,
            type(exc).__name__ + ": " + str(exc),
        )


def main():
    ap = argparse.ArgumentParser(description="并行版全格式转 Markdown")
    ap.add_argument("-j", "--jobs", type=int, default=4, help="并发进程数（默认 4）")
    ap.add_argument("--out", default=str(to_md.DEFAULT_OUT), help="输出资料库（默认 library/）")
    ap.add_argument("--dry-run", action="store_true", help="只报告，不写入、不删除")
    ap.add_argument("--keep", action="store_true", help="转换后保留 newfiles 原文件")
    args = ap.parse_args()

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    files = to_md.iter_inbox()
    if not files:
        print("to_md_parallel: newfiles/ 为空")
        return 0

    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    tasks = [(str(f), str(out_dir), args.dry_run) for f in files]
    jobs = max(1, min(args.jobs, len(tasks)))

    print("to_md_parallel: " + str(len(tasks)) + " files, jobs=" + str(jobs), flush=True)

    ok, failed = [], []
    with ProcessPoolExecutor(max_workers=jobs) as ex:
        futures = {ex.submit(_work, t): t for t in tasks}
        for fut in as_completed(futures):
            rel, good, size, err = fut.result()
            if good:
                ok.append(rel)
                print("OK   " + rel + "  (" + str(size) + " chars)", flush=True)
            else:
                failed.append((rel, err))
                print("FAIL " + rel + "  " + err, flush=True)

    removed = 0
    if not args.dry_run and not args.keep:
        ok_set = set(ok)
        for f in files:
            rel = str(f.relative_to(to_md.INBOX))
            if rel in ok_set:
                try:
                    f.unlink()
                    removed += 1
                except OSError:
                    pass

    tail = "kept source" if (args.keep or args.dry_run) else ("cleared " + str(removed))
    print("\nto_md_parallel: ok=" + str(len(ok)) + " failed=" + str(len(failed))
          + " (" + tail + ")", flush=True)
    for rel, err in failed:
        print("  FAILED: " + rel + "  " + err, flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
