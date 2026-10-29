# Tool to Markdown

把 PDF、EPUB、Word、HTML、纯文本等文件转成**可检索、可引用、按页保真**的 Markdown，并附带书籍校对、Word 生成、OCR 与文献检索工具。

> 这是一个**基础设施副本**：不含任何书籍、Markdown 成果、OCR 缓存、日志、模型权重、虚拟环境或原项目记忆。
> 拿到后按下面的“快速开始”装好依赖即可使用。

---

## 1. 快速开始

### 环境要求

- Windows 10/11（脚本按 Windows 路径与进程管理编写）
- Python 3.11（64 位），安装时勾选 **Add Python to PATH**
- 约 1 GB 磁盘空间（不含可选的 OCR 依赖）

### 安装

```powershell
cd D:\tooltomarkdown
python -m pip install -r requirements.txt
```

如果下载慢，可使用国内镜像：

```powershell
python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

处理扫描书时，再装 OCR 依赖（只提供 RapidOCR）：

```powershell
python -m pip install -r requirements-ocr.txt
```

### 转第一本书

1. 把 PDF / EPUB / DOCX / HTML / TXT 放进 `newfiles\`；
2. 在项目根目录执行：

```powershell
python scripts\to_md.py
```

3. 转换结果出现在 `library\`，原文件在成功后自动清走；失败的文件会留在 `newfiles\` 并显示原因。

批量转换：

```powershell
python scripts\to_md_parallel.py -j 5
```

只预览、不写盘：

```powershell
python scripts\to_md.py --dry-run
```

---

## 2. 包含什么

```
tooltomarkdown\
├─ README.md                 ← 本文件
├─ 使用说明书.md              ← 零代码操作指南
├─ requirements.txt          ← 核心依赖
├─ requirements-ocr.txt      ← 可选：RapidOCR 轨道依赖
├─ StartConsole.vbs          ← 双击启动网页控制台
├─ StopConsole.bat           ← 双击停止控制台
├─ OCR暂停恢复开关.bat        ← OCR 后台任务暂停/恢复
├─ newfiles\                 ← 投递区：待转换文件
├─ library\                  ← 资料库：生成的 .md 与按页图片
├─ outputs\                  ← 产出区：Word、看板
├─ logs\                     ← 日志
├─ tmp\                      ← 临时文件
├─ config\
│  └─ book_profiles.json     ← 书籍版式配置（分页、页码、Word 排版）
└─ scripts\
   ├─ README.md              ← 每个脚本的用途与命令
   ├─ lib\                   ← 公共模块（缓存、GPU 锁、版式解析）
   └─ *.py                   ← 转换、OCR、校对、出书、检索脚本
```

### 核心能力

| 能力 | 入口 | 说明 |
| --- | --- | --- |
| 任意格式转 Markdown | `python scripts\to_md.py` | PDF 按页保真；支持 docx/xlsx/pptx/epub/html/rtf/odt/csv/json/xml/代码等 |
| 批量并行转换 | `python scripts\to_md_parallel.py -j 5` | 以文件为粒度，大文件优先 |
| Markdown 转 Word | `python scripts\md_to_docx.py --book 书名` | 真实分页、页码、脚注悬挂缩进、目录点线 |
| 网页控制台 | 双击 `StartConsole.vbs` | 默认 <http://127.0.0.1:17321>，可看分组、进度、校对 |
| 文献检索 | `python scripts\research.py all "关键词"` | arXiv / Crossref / Semantic Scholar，免密钥 |
| 网页正文转 md | `python scripts\research.py fetch "URL"` | 静态抓取 |
| 项目内搜索 | `python scripts\project_search.py --q "关键词"` | 不打开大文件也能搜 |
| OCR（可选） | `python scripts\ocr_all_batch.py` | 只提供 RapidOCR；装 `requirements-ocr.txt` 即用 |

更细的脚本清单见 [scripts/README.md](scripts/README.md)。

---

## 3. 转换质量：什么算“转成 Markdown”

**“转 Markdown”产出的是可读、可检索、可引用的文本文档，不是把 PDF 逐页截成图片再编号。**

| 档位 | 特征 | 定性 |
| --- | --- | --- |
| 合格 | 每页有可读正文 | 已转 Markdown |
| 图像编目 | 只有整页图、没有文字层 | **未转**，是扫描图像版，需要 OCR |
| 失败 | 正文为空或只有占位符 | 待重转 |

硬规则：

- 抽不到正文必须显式报错或标记，不得静默判成功；
- 0 字节输出一律视为失败；
- PDF 每页输出 `## Page N / total` + 该页文本 + 该页内嵌图片；
- 图片存 `library/assets/<书名>/p<页>-<序号>.<扩展名>`，Markdown 内用相对路径引用。

> 纯文字页只输出文字，不会额外渲染整页图；只有 PDF 里真实内嵌的图像才会导出到 `assets/`。

---

## 4. OCR（可选，只提供 RapidOCR）

本包只提供轨道一 **RapidOCR**，用于把扫描书页图识别成可搜索文字。识别模型由 `rapidocr-openvino` 自带（约 16 MB），不需要另下模型：

```powershell
python -m pip install -r requirements-ocr.txt
```

默认用 CPU 识别；机器有独立显卡时，可在 PowerShell 先执行 `$env:OCR_DEVICE="GPU"` 再运行。

```powershell
python scripts\ocr_all_batch.py --only 书名
```

识别完成后会自动回填 `library\<书名>.md`。PaddleOCR-VL、GOT-OCR、GLM-OCR 等其他视觉模型**不随包提供，也不需要安装**；相关脚本保留在 `scripts/`，但未配套模型、虚拟环境和密钥。控制台里它们显示“未安装”属正常，不影响 RapidOCR、转换与出书。

---

## 5. 版式配置

一本书的分页符、页码位置、分区规则、Word 字体字号页边距，全部在 `config/book_profiles.json`。

```powershell
python scripts\import_md.py --list-profiles
python scripts\import_md.py --book 书名 --dry-run
```

内置四种版式：`center-pageno`、`formfeed`、`page-heading`、`bare`；`auto` 会实测选择。新增书种只加一段配置，不改代码。

---

## 6. 数据与隐私

本包**不包含**：

- 任何书籍、论文、Markdown 内容、按页图片；
- 任何 OCR 结果与缓存；
- 任何日志、模型权重、虚拟环境；
- 原项目的记忆文件、研究产出与会话记录。

`newfiles/`、`library/`、`outputs/`、`logs/`、`tmp/` 已通过 `.gitignore` 排除，避免误把资料提交到 Git。
分享或提交前可自查：

```powershell
git status --short
```

---

## 7. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `python` 不是命令 | 未安装或未加入 PATH | 重装 Python 3.11 并勾选 Add to PATH |
| `ModuleNotFoundError: rapidocr_openvino` | OCR 依赖未装 | `python -m pip install -r requirements-ocr.txt` |
| `ModuleNotFoundError`（其他模块） | 核心依赖未装 | `python -m pip install -r requirements.txt` |
| 转换出的 md 只有图片 | 扫描书没有文字层 | 按第 4 节安装 RapidOCR 后运行 OCR |
| 控制台里其他 OCR 显示“未安装” | 本包不提供其他视觉模型 | 正常，不影响 RapidOCR、转换与出书 |
| 转换出的 md 是空的 | 源文件损坏或格式特殊 | 文件会留在 `newfiles/`，看报错 |
| 控制台打不开 | 端口被占用或旧进程未退 | 先双击 `StopConsole.bat`，再启动 |
| 中文页码/字体异常 | 版式配置不匹配 | 调整 `config/book_profiles.json` 的 profile |
| 旧版 `.doc/.xls/.ppt` 报错 | 二进制旧格式不支持 | 另存为 `.docx/.xlsx/.pptx` |

---

## 8. 已知限制

- 网页抓取只做静态 HTML，不执行 JavaScript；
- Semantic Scholar 免密钥接口常限流；
- 扫描书必须 OCR 才能得到正文；本包只提供 RapidOCR，复杂表格与公式需人工复核；
- 其他视觉模型不随包提供，不需要安装；
- Word 排版依赖 Windows 中文字体（宋体、黑体）。

第三方依赖与模型遵循各自的开源许可证。
