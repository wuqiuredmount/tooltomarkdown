#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""start_pipeline.py - 控制台「启动任务」的一键流水线。

背景（修复的 bug）：
  旧版 review_ui.start_batch() 只拉起 ocr_all_batch.py。而 OCR 只能处理
  已经在 library/assets 里切好图的书——newfiles/ 里新投递的 PDF 根本不在
  它的视野内，于是每轮都是「本轮需处理 0 本」并瞬间退出，界面随即回到
  「任务未运行」，用户观感就是「点了启动没动」。

本脚本把两段串起来：
  阶段 1  to_md_parallel.py   newfiles/ -> library/*.md + library/assets/<名>/
  阶段 2  ocr_all_batch.py     对扫描书做 OCR（断点续传）

两段都幂等：已完成的书自动跳过，重复点击安全。
转换失败的源文件会保留在 newfiles/，不影响后续 OCR 阶段。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
LOG = ROOT / 'tmp' / '_pipeline_run.log'
INBOX = ROOT / 'newfiles'


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except OSError:
        pass


def run(script: str, *args: str) -> int:
    cmd = [PY, str(ROOT / 'scripts' / script), *args]
    log('$ ' + ' '.join(cmd))
    try:
        # 把子进程的 stdout/stderr 全部重定向到日志，加上 CREATE_NO_WINDOW 防止弹黑窗
        with open(LOG, 'a', encoding='utf-8') as f:
            p = subprocess.run(
                cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
            )
        rc = p.returncode
    except Exception as exc:  # noqa: BLE001
        log(f'  {script} 启动失败：{type(exc).__name__}: {exc}')
        return 1
    log(f'  {script} 退出码 {rc}')
    return rc


def inbox_files() -> list[Path]:
    if not INBOX.is_dir():
        return []
    return [p for p in INBOX.rglob('*')
            if p.is_file()
            and not any(part.startswith('.') for part in p.relative_to(INBOX).parts)]


def main() -> int:
    log('=== 流水线启动 ===')

    files = inbox_files()
    if files:
        log(f'阶段 1/2：投递区转 Markdown（{len(files)} 个文件，含切图）')
        rc = run('to_md_parallel.py', '-j', '5')
        if rc != 0:
            log('  转换存在失败项，失败文件保留在 newfiles/，继续 OCR 已入库的书')
    else:
        log('阶段 1/2：投递区为空，跳过转换')

    log('阶段 2/2：OCR 批处理（断点续传）')
    run('ocr_all_batch.py')

    log('=== 流水线结束 ===')
    return 0


if __name__ == '__main__':
    sys.exit(main())
