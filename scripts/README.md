# scripts/ 脚本地图

> 所有命令都在项目根目录执行。
> 不确定该用哪个：先看项目根目录的《使用说明书.md》「常见任务速查」。

## 一、检索

| 脚本 | 作用 | 常用命令 |
| --- | --- | --- |
| `project_search.py` | 不打开大文件也能搜代码/文档 | `python scripts/project_search.py --q "关键词"` |

## 二、投递 → 转换 → 入库

| 脚本 | 作用 | 常用命令 |
| --- | --- | --- |
| `to_md.py` | 任意格式转 Markdown（PDF 按页保真） | `python scripts/to_md.py` |
| `to_md_parallel.py` | 同上，多进程，大批量用 | `python scripts/to_md_parallel.py -j 5` |
| `import_md.py` | 已有 Markdown → 单页 JSON | `python scripts/import_md.py --book 书名` |
| `build_book.py` | 单页 JSON → 整本 Markdown | `python scripts/build_book.py --book 书名` |
| `batch_import.py` | 把 library 全部 md 批量导入 JSON | `python scripts/batch_import.py` |
| `start_pipeline.py` | 控制台「启动任务」的一键流水线 | 一般由控制台调用 |

## 三、OCR（可选：扫描书转文字）

本包只预置轨道一 **RapidOCR**：`python -m pip install -r requirements-ocr.txt`（模型由 rapidocr-openvino 自带）；默认 CPU，独立显卡可设 `OCR_DEVICE=GPU`。
轨道二/三/四与云端兜底的脚本保留，但模型、虚拟环境和密钥均不随包提供，不需要安装或执行。
不装任何一条也能正常完成「文字层 PDF → Markdown」的全部工作。

| 脚本 | 作用 | 常用命令 |
| --- | --- | --- |
| `ocr_all_batch.py` | RapidOCR 全量批处理（断点续传） | `python scripts/ocr_all_batch.py --only 书名` |
| `ocr_scanned.py` | RapidOCR 单本扫描书 OCR | `python scripts/ocr_scanned.py --book 书名` |
| `ocr_status.py` | 只读进度（JSON/表格） | `python scripts/ocr_status.py --json` |
| `dashboard.py` | 终端/HTML 实时看板 | `python scripts/dashboard.py --once` |
| `ocr_selfcheck.py` | 随机抽页导出原图做人工比对 | `python scripts/ocr_selfcheck.py --count 3` |
| `ocr_switch.ps1` | OCR 后台任务暂停/恢复 | 由 `OCR暂停恢复开关.bat` 调用 |

原项目其他 OCR 轨道与合并/补丁脚本仍保留在 `scripts/`，但模型、虚拟环境、密钥均不随包提供，不需要执行。

## 四、校对与出书

| 脚本 | 作用 | 常用命令 |
| --- | --- | --- |
| `review_ui.py` | Web 控制台 + 校对界面（端口 17321） | `python scripts/review_ui.py` |
| `md_to_docx.py` | Markdown → 校对预览 DOCX | `python scripts/md_to_docx.py --book 书名` |
| `batch_docx.py` | 批量生成 DOCX | `python scripts/batch_docx.py` |
| `llm_correct.py` | 一句话定点修改一页 | `python scripts/llm_correct.py --book X --physical N --prompt "..."` |

## 五、外部检索

| 脚本 | 作用 | 常用命令 |
| --- | --- | --- |
| `research.py` | arXiv / Crossref / Semantic Scholar / 网页抓取（免密钥） | `python scripts/research.py all "关键词"` |

## 六、公共库 `scripts/lib/`

| 模块 | 作用 |
| --- | --- |
| `gpu_lock.py` | 核显跨进程互斥（OCR 引擎不能同时用 GPU） |
| `cache.py` | 结果缓存、断点续传、原子写 |
| `artifacts.py` | 中间产物分层与清理（默认 dry-run） |
| `footnotes.py` | 脚注切分与对账 |
| `bookfmt.py` | 书籍版式解析（配合 `config/book_profiles.json`） |
| `store.py` | 单页 JSON 的读写与主键规则 |
| `done.py` | 书级完成标记（`library/.ocr_done.json`） |

## 约定

- 新脚本直接放 `scripts/`，不要再加 `_test_`、`_gpu_` 这类一次性前缀。
- 一次性实验做完就删；需要留证的自行归档。
- 新增/改名脚本后，同步更新本文件。
