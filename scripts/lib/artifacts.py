# -*- coding: utf-8 -*-
"""中间产物分层与清理。

目录约定：
  tmp/.staging/   临时缩放图、预览图 —— 进程结束即删；崩溃残留按 TTL 清
  tmp/.cache/     各工具原始输出 —— 可重算，按 TTL / 容量 LRU 清
  newfiles/_*_out/     单次运行产物 —— 可清（合并后可删）
  logs/                日志 —— 30 天
  library/ outputs/  —— **永不清理**

所有清理默认 dry-run，必须显式 apply 才删；删除前逐条列出路径与大小。
"""
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NEWFILES = ROOT / 'tmp'
STAGING = NEWFILES / '.staging'
CACHE = NEWFILES / '.cache'
LOGS = ROOT / 'logs'

PROTECTED = {'library', '.git', 'scripts', 'models', 'tools',
             '.venvs', 'outputs', 'newfiles'}


def staging_path(name: str) -> Path:
    STAGING.mkdir(parents=True, exist_ok=True)
    return STAGING / name


def clear_staging(keep: tuple = ()):
    """清空 staging（进程正常结束时调用）。"""
    if not STAGING.exists():
        return 0
    n = 0
    for p in STAGING.iterdir():
        if p.name in keep:
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            n += 1
        except OSError:
            pass
    return n


def _walk_size(p: Path):
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    return sum(f.stat().st_size for f in p.rglob('*') if f.is_file())


def _candidates(staging_ttl_h=24, cache_ttl_days=7, out_ttl_days=2, logs_ttl_days=30):
    now = time.time()
    items = []

    def add(p, keep_days, tag):
        if not p.exists():
            return
        age_d = (now - p.stat().st_mtime) / 86400
        if age_d > keep_days:
            items.append((p, _walk_size(p), tag, age_d))

    # staging：超 TTL 的残留
    if STAGING.exists():
        for p in STAGING.iterdir():
            age_h = (now - p.stat().st_mtime) / 3600
            if age_h > staging_ttl_h:
                items.append((p, _walk_size(p), 'staging', age_h / 24))

    # 缓存：按 TTL
    if CACHE.exists():
        for p in CACHE.iterdir():
            add(p, cache_ttl_days, 'cache')

    # 单次运行产物
    for d in NEWFILES.glob('_*_out'):
        add(d, out_ttl_days, 'out')

    # tmp 根下散落的临时物（_xxx.jpg / _xxx.zip / _backup 等）
    if NEWFILES.exists():
        for p in NEWFILES.iterdir():
            if not p.name.startswith('_'):
                continue
            if p.name in ('.cache', '.staging') or p.name.endswith('_out'):
                continue
            age_d = (now - p.stat().st_mtime) / 86400
            if age_d > out_ttl_days:
                items.append((p, _walk_size(p), 'temp', age_d))

    # 日志
    if LOGS.exists():
        for p in LOGS.iterdir():
            add(p, logs_ttl_days, 'logs')

    return items


def plan_clean(**kw):
    """返回 (待删列表, 总字节)。列表项：(path, size, tag, age_days)。"""
    items = _candidates(**kw)
    return items, sum(i[1] for i in items)


def clean(apply=False, **kw):
    """apply=False 只报告；True 才删除。受保护目录永不进入候选。"""
    items, total = plan_clean(**kw)
    if not items:
        return [], 0, 0
    deleted = 0
    if apply:
        for p, size, tag, age in items:
            if p.resolve().parent.name in PROTECTED or p.name in PROTECTED:
                continue
            try:
                if p.is_dir():
                    shutil.rmtree(p)
                else:
                    p.unlink()
                deleted += 1
            except OSError:
                pass
    return items, total, deleted


def disk_report():
    """返回 [(名称, 字节, 文件数)]，并单独给出「中间产物」合计。"""
    rows = []

    def add(name, p):
        if p.exists():
            rows.append((name, _walk_size(p), sum(1 for _ in p.rglob('*') if _.is_file())
                         if p.is_dir() else 1))

    add('library/assets', ROOT / 'library' / 'assets')
    add('library/ocr_text', ROOT / 'library' / 'ocr_text')
    add('models', ROOT / 'models')
    add('tools', ROOT / 'tools')
    add('tmp/.cache', CACHE)
    add('tmp/.staging', STAGING)
    for d in NEWFILES.glob('_*_out'):
        add(f'tmp/{d.name}', d)
    add('logs', LOGS)

    # 散落的临时文件
    loose = 0
    loose_n = 0
    if NEWFILES.exists():
        for p in NEWFILES.iterdir():
            if p.name.startswith('_') and p.name not in ('.cache', '.staging') \
                    and not p.name.endswith('_out') and p.is_file():
                loose += _walk_size(p)
                loose_n += 1
    if loose:
        rows.append(('tmp/_*.临时文件', loose, loose_n))

    rows.sort(key=lambda r: -r[1])
    return rows


def intermediate_total():
    """只统计「可清理的中间产物」，不含书库与模型。"""
    total = 0
    for name, size, _ in disk_report():
        if name.startswith('tmp') or name == 'logs':
            total += size
    return total


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f'{n:.1f}{unit}' if unit != 'B' else f'{int(n)}B'
        n /= 1024
    return f'{n:.1f}TB'
