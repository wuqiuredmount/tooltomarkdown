# -*- coding: utf-8 -*-
"""书级完成标记：library/.ocr_done.json。

为什么需要：批处理原先靠「缓存页数 < 总页数」临时判断。缓存一旦被误删、
改名或迁移，已跑完的书会被整本重新 OCR（一本数千页的书约需数小时）。
这里落一份显式标记，缓存只是证据之一，不是唯一依据。

结构：
  {
    "version": 1,
    "books": {
      "<书名 stem>": {"pages": 320, "cached": 320,
                      "merged": true, "at": "2026-01-01 12:00:00"}
    }
  }

判定：标记存在 且 pages 与当前图片页数一致 => 已完成。
页数变了（换了 PDF、重新导出）视为失效，会重新处理。
"""
import json
import time
from pathlib import Path

DONE = Path('library/.ocr_done.json')


def _load():
    if not DONE.exists():
        return {'version': 1, 'books': {}}
    try:
        d = json.loads(DONE.read_text(encoding='utf-8'))
    except Exception:
        return {'version': 1, 'books': {}}
    if not isinstance(d, dict):
        return {'version': 1, 'books': {}}
    d.setdefault('version', 1)
    if not isinstance(d.get('books'), dict):
        d['books'] = {}
    return d


def _save(d):
    DONE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DONE.with_name(DONE.name + '.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(DONE)


def is_done(stem, n_pages=None):
    """标记存在且页数一致才算完成；n_pages 为 None 时只看有无标记。"""
    rec = _load()['books'].get(stem)
    if not rec:
        return False
    if n_pages is not None and rec.get('pages') != n_pages:
        return False
    return True


def mark(stem, n_pages, cached, merged=True):
    d = _load()
    d['books'][stem] = {
        'pages': int(n_pages),
        'cached': int(cached),
        'merged': bool(merged),
        'at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    _save(d)


def unmark(stem):
    d = _load()
    if stem in d['books']:
        del d['books'][stem]
        _save(d)
        return True
    return False


def all_done():
    return dict(_load()['books'])
