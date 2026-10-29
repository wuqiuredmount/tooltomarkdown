# -*- coding: utf-8 -*-
"""Tool to Markdown 统一控制台与沉浸式校对器。"""
import http.server
import socketserver
import urllib.parse
import subprocess
import html
import sys
import os
import socket
import threading
import time
import webbrowser
import json
import glob
import re
import importlib.util
from pathlib import Path
import signal
import psutil
import shutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))
from lib import store as S
from llm_correct import apply_text

# 默认端口：选一个冷门端口，避开 8000/8001 这类常被开发工具抢占的「公共端口」。
# 可用环境变量 CONSOLE_PORT 覆盖。
DEFAULT_PORT = 17321
PORT = DEFAULT_PORT

# 全局转换任务状态
CONVERT_TASK = {'running': False, 'progress': 0, 'msg': '', 'is_error': False}
CONVERT_LOCK = threading.Lock()

def run_convert_task(book, fmt):
    global CONVERT_TASK
    try:
        with CONVERT_LOCK:
            CONVERT_TASK = {'running': True, 'progress': 5, 'msg': f"正在准备转换 [{book}]...", 'is_error': False}
        
        # 1. 转 DOCX
        CONVERT_TASK['progress'] = 10
        CONVERT_TASK['msg'] = "正在解析 Markdown 并生成 Word 文档..."
        cmd = [sys.executable, str(ROOT / 'scripts' / 'md_to_docx.py'), '--book', book]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        out_msg = res.stdout + res.stderr
        
        if res.returncode != 0:
            CONVERT_TASK['msg'] = f"❌ 转 Word 失败：\n{out_msg}"
            CONVERT_TASK['is_error'] = True
            # 必须置 100，前端只在 progress==100 时才展示错误信息
            CONVERT_TASK['progress'] = 100
            CONVERT_TASK['running'] = False
            return
            
        CONVERT_TASK['progress'] = 50
        
        # 2. 转 PDF
        if fmt == 'pdf':
            CONVERT_TASK['msg'] = "Word 生成完毕，正在调用系统 Word 渲染 PDF（可能需要几十秒）..."
            CONVERT_TASK['progress'] = 60
            docx_path = ROOT / 'outputs' / 'docx' / f'{book}.docx'
            if docx_path.exists():
                pdf_path = docx_path.with_suffix('.pdf')
                ps_code = f'''
                $word = New-Object -ComObject Word.Application
                $word.Visible = $false
                $doc = $word.Documents.Open("{docx_path}")
                $doc.SaveAs("{pdf_path}", 17)
                $doc.Close()
                $word.Quit()
                '''
                ps_res = subprocess.run(['powershell', '-NoProfile', '-Command', ps_code], capture_output=True, text=True)
                if pdf_path.exists():
                    out_msg += f"\n✅ PDF 已生成：outputs/docx/{book}.pdf"
                else:
                    out_msg += f"\n❌ PDF 生成失败：\n{ps_res.stderr}"
                    CONVERT_TASK['is_error'] = True
            else:
                out_msg += "\n❌ 找不到生成的 DOCX 文件，无法转 PDF。"
                CONVERT_TASK['is_error'] = True

        CONVERT_TASK['progress'] = 100
        CONVERT_TASK['msg'] = out_msg
        CONVERT_TASK['running'] = False
        
    except Exception as e:
        CONVERT_TASK['msg'] = f"发生异常：{e}"
        CONVERT_TASK['is_error'] = True
        CONVERT_TASK['progress'] = 100
        CONVERT_TASK['running'] = False

def rename_book_fully(old_name, new_name):
    """重命名一本书的所有关联数据。"""
    old_name = old_name.strip()
    new_name = new_name.strip()
    if not old_name or not new_name or old_name == new_name:
        return False, "无效的书名"
        
    # 检查新文件名是否已存在
    if (ROOT / 'library' / f'{new_name}.md').exists():
        return False, f"目标书名《{new_name}》已存在"

    # 定义需要改名/处理的路径
    pairs = [
        (ROOT / 'library' / f'{old_name}.md', ROOT / 'library' / f'{new_name}.md'),
        (ROOT / 'library' / 'assets' / old_name, ROOT / 'library' / 'assets' / new_name),
        (ROOT / 'library' / 'ocr_json' / old_name, ROOT / 'library' / 'ocr_json' / new_name),
        (ROOT / 'library' / 'ocr_text' / old_name, ROOT / 'library' / 'ocr_text' / new_name),
        (ROOT / 'library' / 'backups' / old_name, ROOT / 'library' / 'backups' / new_name),
        (ROOT / 'outputs' / 'docx' / f'{old_name}.docx', ROOT / 'outputs' / 'docx' / f'{new_name}.docx'),
        (ROOT / 'outputs' / 'docx' / f'{old_name}.pdf', ROOT / 'outputs' / 'docx' / f'{new_name}.pdf'),
        (ROOT / 'newfiles' / f'{old_name}.pdf', ROOT / 'newfiles' / f'{new_name}.pdf'),
        (ROOT / 'newfiles' / f'{old_name}.epub', ROOT / 'newfiles' / f'{new_name}.epub'),
    ]

    # 执行改名
    for src, dst in pairs:
        if src.exists():
            try:
                src.rename(dst)
            except Exception as e:
                return False, f"重命名失败 {src.name}: {e}"

    # 替换 MD 文件内容里的图片引用路径 (assets/旧名 -> assets/新名)
    md_path = ROOT / 'library' / f'{new_name}.md'
    if md_path.exists():
        try:
            content = md_path.read_text(encoding='utf-8')
            content = content.replace(f'assets/{old_name}/', f'assets/{new_name}/')
            md_path.write_text(content, encoding='utf-8')
        except Exception:
            pass

    # 更新所有 JSON 里的 book 字段
    json_dir = ROOT / 'library' / 'ocr_json' / new_name
    if json_dir.exists():
        for jf in json_dir.glob('*.json'):
            try:
                jdata = json.loads(jf.read_text(encoding='utf-8'))
                if jdata.get('book') == old_name:
                    jdata['book'] = new_name
                    jf.write_text(json.dumps(jdata, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception:
                pass

    # 更新 .book_groups.json
    try:
        groups = load_groups()
        changed = False
        for g in groups:
            if old_name in groups[g]:
                groups[g].remove(old_name)
                groups[g].append(new_name)
                changed = True
        if changed:
            save_groups(groups)
    except Exception:
        pass

    # 更新 .ocr_done.json
    done_file = ROOT / 'library' / '.ocr_done.json'
    if done_file.exists():
        try:
            data = json.loads(done_file.read_text(encoding='utf-8'))
            if 'books' in data and old_name in data['books']:
                data['books'][new_name] = data['books'].pop(old_name)
                done_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass

    return True, "重命名成功"

def delete_book_fully(book_name):
    """彻底删除一本书的所有关联数据。"""
    # 安全红线：禁止删除系统级文件
    if book_name.lower() in ('readme', 'index', 'config', 'readme.md'):
        raise ValueError("⛔ 安全限制：禁止删除系统核心说明文件。")
    
    # 从分组中移除该书
    try:
        groups = load_groups()
        changed = False
        for g in groups:
            if book_name in groups[g]:
                groups[g].remove(book_name)
                changed = True
        if changed:
            save_groups(groups)
    except Exception:
        pass
    # 可能包含该书的目录或文件路径
    targets = [
        ROOT / 'library' / f'{book_name}.md',
        ROOT / 'library' / 'assets' / book_name,
        ROOT / 'library' / 'ocr_json' / book_name,
        ROOT / 'library' / 'ocr_text' / book_name,
        ROOT / 'library' / 'backups' / book_name,
        ROOT / 'outputs' / 'docx' / f'{book_name}.docx',
        ROOT / 'outputs' / 'docx' / f'{book_name}.pdf',
        ROOT / 'outputs' / 'merged' / book_name,
        ROOT / 'newfiles' / f'{book_name}.pdf',
        ROOT / 'newfiles' / f'{book_name}.epub',
        ROOT / 'newfiles' / f'{book_name}.mobi',
        ROOT / 'newfiles' / f'{book_name}.txt',
    ]
    
    deleted = []
    for p in targets:
        if p.exists():
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            deleted.append(p.name)
            
    # 从完成标记中移除
    done_file = ROOT / 'library' / '.ocr_done.json'
    if done_file.exists():
        try:
            data = json.loads(done_file.read_text(encoding='utf-8'))
            if 'books' in data and book_name in data['books']:
                del data['books'][book_name]
                done_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        except Exception:
            pass
            
    return deleted

def load_groups():
    groups_file = ROOT / 'library' / '.book_groups.json'
    if groups_file.exists():
        try:
            return json.loads(groups_file.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}

def save_groups(groups):
    groups_file = ROOT / 'library' / '.book_groups.json'
    groups_file.write_text(json.dumps(groups, ensure_ascii=False, indent=2), encoding='utf-8')

def get_pdf_total_pages(pdf_path):
    """极速安全获取 PDF 总页数（基于本地缓存文件，绝不阻塞主线程）。"""
    cache_file = ROOT / 'tmp' / ('_pages_' + pdf_path.stem + '.txt')
    if cache_file.exists():
        try:
            return int(cache_file.read_text(encoding='utf-8').strip())
        except Exception:
            pass
    # 如果没有缓存，默认返回已知的总页数或 8298（资治通鉴特定）
    return 8298

def is_paused():
    return (ROOT / 'scripts' / '.ocr_paused_state').exists()

def monitor_resources_loop():
    """后台守护线程：系统负载过高时自动挂起 OCR，负载恢复后自动继续。"""
    while True:
        time.sleep(5)
        # 检查系统保护开关是否被显式关闭
        monitor_disabled = ROOT / 'scripts' / '.sys_monitor_disabled'
        if monitor_disabled.exists():
            continue

        try:
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory().percent
        except Exception:
            continue

        state = ROOT / 'scripts' / '.ocr_paused_state'
        auto_state = ROOT / 'scripts' / '.ocr_auto_paused'
        
        procs = ocr_procs()
        if not procs:
            continue

        # 阈值：CPU > 95% 或 内存 > 90%
        if (cpu > 95 or mem > 90) and not state.exists():
            paused = 0
            for pr in procs:
                try:
                    pr.suspend()
                    paused += 1
                except Exception:
                    pass
            if paused:
                state.touch()
                auto_state.touch()
                msg = f"\n[{time.strftime('%H:%M:%S')}] [系统保护] 负载过高 (CPU {cpu}%, MEM {mem}%)，自动挂起 {paused} 个后台进程。"
                with open(ROOT / 'library' / '.ocr_log.txt', 'a', encoding='utf-8') as f:
                    f.write(msg + '\n')

        # 恢复阈值：CPU < 80% 且 内存 < 80%
        elif cpu < 80 and mem < 80 and state.exists() and auto_state.exists():
            resumed = 0
            for pr in procs:
                try:
                    pr.resume()
                    resumed += 1
                except Exception:
                    pass
            if resumed:
                state.unlink(missing_ok=True)
                auto_state.unlink(missing_ok=True)
                msg = f"\n[{time.strftime('%H:%M:%S')}] [系统保护] 负载恢复 (CPU {cpu}%, MEM {mem}%)，自动恢复 {resumed} 个后台进程。"
                with open(ROOT / 'library' / '.ocr_log.txt', 'a', encoding='utf-8') as f:
                    f.write(msg + '\n')

def is_running():
    """是否真的有 OCR 任务在跑。

    不能只判断「有没有 python.exe」——看板自身、静态服务器都是 python 进程，
    那样会永远显示「任务运行中」并卡在最后一行日志上（曾因此误报明实录 100% 运行中）。
    """
    keys = ('ocr_scanned.py', 'ocr_all_batch.py', 'to_md.py', 'to_md_parallel.py',
            'start_pipeline.py')
    try:
        for pr in psutil.process_iter(['name', 'cmdline']):
            if (pr.info.get('name') or '').lower() not in ('python.exe', 'python'):
                continue
            cl = ' '.join(pr.info.get('cmdline') or [])
            if any(k in cl for k in keys) and 'review_ui.py' not in cl:
                return True
        return False
    except Exception:
        pass
    # psutil 不可用时保守返回 False（宁可显示空闲，也不假报运行中）
    return False


def ocr_procs():
    """当前真正在跑的后台重任务进程（OCR或转换）。"""
    keys = ('ocr_scanned.py', 'ocr_all_batch.py', 'to_md.py', 'to_md_parallel.py',
            'start_pipeline.py')
    out = []
    for pr in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            cl = ' '.join(pr.info.get('cmdline') or [])
            if any(k in cl for k in keys) and 'review_ui.py' not in cl:
                out.append(pr)
        except Exception:
            pass
    return out


def launch_process(script_name, args=None):
    """以独立进程静默启动指定的脚本，输出写入日志。"""
    cmd = [sys.executable, str(ROOT / 'scripts' / script_name)] + (args or [])
    log = ROOT / 'tmp' / '_ocr_run.log'
    log.parent.mkdir(exist_ok=True)
    base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    with open(log, 'a', encoding='utf-8') as f:
        f.write("\n=== %s 启动 %s %s ===\n" % (time.strftime('%H:%M:%S'), script_name, ' '.join(args or [])))
        f.flush()
        try:
            # CREATE_BREAKAWAY_FROM_JOB：脱离父进程的作业对象，避免连带终止
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                                    creationflags=base | 0x01000000)
        except OSError:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                                    creationflags=base)
    return proc.pid


def start_batch(args=None):
    """兼容旧调用：启动一键流水线（切图+OCR）。"""
    return launch_process('start_pipeline.py', args)


def toggle_ocr():
    """暂停 / 恢复。

    旧实现（外部 ps1）只会挂起/解冻现存进程：
    进程已经不在时直接退出、**不清理暂停标记**，于是看板永远显示「已暂停」，
    「恢复任务」按了没反应。现在：能解冻就解冻，进程没了就清标记并重启批处理。
    """
    state = ROOT / 'scripts' / '.ocr_paused_state'
    auto_state = ROOT / 'scripts' / '.ocr_auto_paused'
    procs = ocr_procs()

    if state.exists():
        state.unlink(missing_ok=True)
        auto_state.unlink(missing_ok=True)
        resumed = 0
        for pr in procs:
            try:
                pr.resume()
                resumed += 1
            except Exception:
                pass
        if resumed:
            return {'action': 'resume', 'processes': resumed,
                    'msg': '已解冻 %d 个后台进程' % resumed}
        pid = start_batch()
        return {'action': 'restart', 'pid': pid,
                'msg': '没有可解冻的进程，已重新启动批处理（断点续传）'}

    if not procs:
        # 没有在跑的任务：直接启动批处理（幂等，已完成的书会自动跳过）
        pid = start_batch()
        return {'action': 'start', 'pid': pid,
                'msg': '当前没有在跑的任务，已启动流水线（转换+OCR，断点续传）'}

    paused = 0
    for pr in procs:
        try:
            pr.suspend()
            paused += 1
        except Exception:
            pass
    state.touch()
    auto_state.unlink(missing_ok=True)
    return {'action': 'pause', 'processes': paused,
            'msg': '已冻结 %d 个后台进程' % paused}


def get_dashboard_data():
    # 0. 获取硬件状态
    sys_stats = {
        'cpu': int(psutil.cpu_percent()), 
        'mem': int(psutil.virtual_memory().percent),
        'gpu': 0
    }
    # 通过 WMI 性能类获取所有 GPU 引擎的实时占用率并累加
    try:
        ps_cmd = [
            "powershell", "-NoProfile", "-Command",
            "(Get-CimInstance Win32_PerfFormattedData_GPUPerformanceCounters_GPUEngine | Measure-Object -Property UtilizationPercentage -Sum).Sum"
        ]
        out = subprocess.check_output(ps_cmd, text=True, stderr=subprocess.DEVNULL, timeout=3)
        gpu_val = float(out.strip())
        sys_stats['gpu'] = min(100, int(gpu_val))
    except Exception:
        pass

    # 1. 基础状态
    running = is_running()
    paused = is_paused()
    
    # 2. 解析日志与当前任务类型
    current_book = "无"
    progress_text = "空闲中"
    progress_percent = 0
    speed = "-"
    active_engine = "无"
    
    # 区分正在跑的是切图还是 OCR
    running_tomd = False
    running_ocr = False
    for pr in ocr_procs():
        try:
            cl = ' '.join(pr.cmdline() or [])
            if 'to_md.py' in cl or 'to_md_parallel.py' in cl:
                running_tomd = True
            if 'ocr_scanned.py' in cl or 'ocr_all_batch.py' in cl:
                running_ocr = True
        except Exception:
            pass

    if running_tomd:
        active_engine = "PDF 转换"
        current_book = "新书入库..."
        progress_text = "正在切图"
        
        # 优先读取进度打点
        prog_file = ROOT / 'tmp' / '_tomd_progress.json'
        if prog_file.exists():
            try:
                pdata = json.loads(prog_file.read_text(encoding='utf-8'))
                current_book = pdata.get('book', current_book)[:30] + '...'
                curr = pdata.get('curr', 0)
                total = pdata.get('total', 1)
                progress_percent = int(curr / total * 100) if total > 0 else 0
                progress_text = f"{curr} / {total} 页 ({progress_percent}%)"
            except Exception:
                pass
        else:
            # 兜底：如果是旧代码启动的切图进程，通过 assets 目录里的最大页号来估算
            pdfs = list((ROOT / 'newfiles').glob('*.pdf'))
            if pdfs:
                current_book = pdfs[0].name[:30] + '...'
                # 尝试从 assets 目录读取已切出的最大页码
                img_dir = ROOT / 'library' / 'assets' / pdfs[0].stem
                max_page = 0
                if img_dir.exists():
                    import re
                    for f in img_dir.glob('p*.*'):
                        m = re.match(r'p(\d+)-', f.name)
                        if m:
                            max_page = max(max_page, int(m.group(1)))
                if max_page > 0:
                    total_est = get_pdf_total_pages(pdfs[0])
                    if total_est > 0:
                        progress_percent = min(99, int(max_page / total_est * 100))
                        progress_text = f"{max_page} / {total_est} 页 ({progress_percent}%)"
    
    log_path = ROOT / 'library' / '.ocr_log.txt'
    if log_path.exists() and running_ocr:
        try:
            with open(log_path, 'rb') as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 4000))
                lines = f.read().decode('utf-8', errors='ignore').splitlines()
            for line in reversed(lines):
                # 修复正则：剥离前面的时间戳，正确抓取书名
                m = re.search(r'\]\s+\[(.*?)\]\s+p(\d+)\s+(\d+)/(\d+)\s+([\d.]+s)', line)
                if m:
                    current_book = m.group(1)
                    curr = int(m.group(3))
                    total = int(m.group(4))
                    speed = m.group(5)
                    progress_percent = int(curr / total * 100) if total > 0 else 0
                    progress_text = f"{curr} / {total} 页 ({progress_percent}%)"
                    active_engine = "RapidOCR"
                    break
                
                m2 = re.search(r'\]\s+--- \[\d+/\d+\] (.*?) \(\d+页\) ---', line)
                if m2:
                    current_book = m2.group(1)[:30] + '...' if len(m2.group(1)) > 30 else m2.group(1)
                    progress_text = "准备中 (子进程启动)"
                    active_engine = "RapidOCR"
                    break
        except Exception:
            pass

    # 3. 四轨引擎状态（检测最近是否在跑，并提取正在跑的书名）
    now = time.time()
    def get_active_task(folder_name):
        d = ROOT / 'tmp' / folder_name
        if not d.exists(): return None
        try:
            files = [f for f in d.rglob('*') if f.is_file()]
            if not files: return None
            latest = max(files, key=lambda f: f.stat().st_mtime)
            if (now - latest.stat().st_mtime) < 180:
                # 比如 "明实录 明太祖实录1-5_p10.md"，把 _p 切掉，截取前 10 个字
                name = latest.stem.split('_p')[0]
                return name[:12] + "..." if len(name) > 12 else name
        except Exception:
            pass
        return None

    engines = [
        {"name": "RapidOCR", "ready": importlib.util.find_spec('rapidocr_openvino') is not None, "desc": "底盘打底", "active": active_engine == "RapidOCR", "task": current_book if active_engine == "RapidOCR" else "-"},
        {"name": "PaddleOCR-VL", "ready": (ROOT / '.venvs' / 'vlm').exists(), "desc": "精校双栏", "active": bool(get_active_task('_vlm_out')), "task": get_active_task('_vlm_out') or "-"},
        {"name": "GOT-OCR", "ready": (ROOT / '.venvs' / 'got').exists(), "desc": "脚注补盲", "active": bool(get_active_task('_got_out')), "task": get_active_task('_got_out') or "-"},
        {"name": "GLM-OCR", "ready": (ROOT / 'models' / 'GLM-OCR').exists(), "desc": "表格公式", "active": bool(get_active_task('_glm_out')), "task": get_active_task('_glm_out') or "-"}
    ]
    # 如果正在切图，插入一个虚拟的切图引擎展示状态
    if running_tomd:
        engines.insert(0, {"name": "PDF 切图", "ready": True, "desc": "文档解析", "active": True, "task": current_book})
    
    # 4. 书库统计
    md_files = list((ROOT / 'library').glob('*.md'))
    docx_files = list((ROOT / 'outputs' / 'docx').glob('*.docx')) if (ROOT / 'outputs' / 'docx').exists() else []
    # 排除 README.md，只计算真正的书籍文件
    newfiles_count = len([f for f in (ROOT / 'newfiles').glob('*.*') if f.name != 'README.md']) if (ROOT / 'newfiles').exists() else 0

    return {
        'running': running, 'paused': paused, 'active_engine': active_engine,
        'current_book': current_book, 'progress_text': progress_text, 'progress_percent': progress_percent, 'speed': speed,
        'engines': engines, 'sys_stats': sys_stats,
        'stats': {'md': len(md_files), 'docx': len(docx_files), 'queue': newfiles_count},
        'convert_status': CONVERT_TASK
    }

STATUS_HTML = """
<div style="display:flex; flex-direction: column; gap: 20px;">
    <!-- 主进程状态与进度 -->
    <div style="display:flex; justify-content: space-between; align-items: flex-end;">
        <div>
            <div style="font-size: 24px; font-weight: bold; margin-bottom: 5px;" id="dash-status">
                __STATUS_TEXT__
            </div>
            <div style="font-size: 15px; color: #aaa;" id="dash-details">
                当前任务: <span style="color:#fff; font-weight:bold;">__CURRENT_BOOK__</span> &nbsp;|&nbsp; 
                引擎: <span style="color:#e91e63; font-weight:bold;">__ACTIVE_ENGINE__</span> &nbsp;|&nbsp; 
                速度: <span style="color:#6cb6ff;">__SPEED__</span> &nbsp;|&nbsp; 
                进度: <span style="color:#28a745;">__PROGRESS_TEXT__</span>
            </div>
        </div>
        <div style="display: flex; gap: 15px; align-items: center;">
            <label style="display:flex; align-items:center; color:#aaa; cursor:pointer; font-size:14px;">
                <input type="checkbox" id="sys-monitor-toggle" onchange="toggleMonitor()" style="margin-right:6px; width:16px; height:16px;" __MONITOR_CHECKED__>
                🛡️ 系统保护 (防卡死)
            </label>
            <div id="start-actions" style="display: __START_ACTIONS_DISPLAY__; gap: 10px;">
                <button id="btn-start-tomd" class="btn btn-green" onclick="startTask('tomd')" style="padding: 12px 20px; font-size: 15px;">📄 启动切图</button>
                <button id="btn-start-ocr" class="btn btn-green" onclick="startTask('ocr')" style="padding: 12px 20px; font-size: 15px;">⚙️ 启动RapidOCR</button>
            </div>
            <button id="dash-btn" class="btn __BTN_CLASS__" onclick="toggleOCR()" style="padding: 12px 30px; font-size: 16px; display: __BTN_DISPLAY__;">__BTN_TEXT__</button>
        </div>
    </div>
    
    <!-- 进度条 -->
    <div style="width: 100%; background: #333; border-radius: 8px; height: 16px; overflow: hidden; box-shadow: inset 0 2px 4px rgba(0,0,0,0.5); margin-bottom: 5px;">
        <div id="dash-progress-bar" style="height: 100%; width: __PROGRESS_PERCENT__%; background: linear-gradient(90deg, #0066cc, #00cc66); transition: width 0.5s ease;"></div>
    </div>

    <!-- 硬件监控 -->
    <div style="display: flex; gap: 20px; margin-bottom: 10px;">
        <div style="flex: 1;">
            <div style="display: flex; justify-content: space-between; font-size: 12px; color: #aaa; margin-bottom: 4px;">
                <span>CPU</span><span id="sys-cpu-text">0%</span>
            </div>
            <div style="width: 100%; background: #333; height: 6px; border-radius: 3px; overflow: hidden;">
                <div id="sys-cpu-bar" style="height: 100%; width: 0%; background: #6cb6ff; transition: width 0.5s ease;"></div>
            </div>
        </div>
        <div style="flex: 1;">
            <div style="display: flex; justify-content: space-between; font-size: 12px; color: #aaa; margin-bottom: 4px;">
                <span>内存</span><span id="sys-mem-text">0%</span>
            </div>
            <div style="width: 100%; background: #333; height: 6px; border-radius: 3px; overflow: hidden;">
                <div id="sys-mem-bar" style="height: 100%; width: 0%; background: #b088f9; transition: width 0.5s ease;"></div>
            </div>
        </div>
        <div style="flex: 1;">
            <div style="display: flex; justify-content: space-between; font-size: 12px; color: #aaa; margin-bottom: 4px;">
                <span>GPU (Intel)</span><span id="sys-gpu-text">0%</span>
            </div>
            <div style="width: 100%; background: #333; height: 6px; border-radius: 3px; overflow: hidden;">
                <div id="sys-gpu-bar" style="height: 100%; width: 0%; background: #28a745; transition: width 0.5s ease;"></div>
            </div>
        </div>
    </div>

    <!-- 四轨引擎与统计 -->
    <div style="display: flex; gap: 15px; margin-top: 10px; flex-wrap: wrap;" id="dash-engines">
        __ENGINE_BLOCKS__
        <div style="flex: 1; min-width: 200px; background: #2a2a2a; border-radius: 8px; padding: 15px; border: 1px solid #333; position: relative;">
            <div style="font-size: 12px; color: #888; margin-bottom: 10px; text-transform: uppercase; letter-spacing: 1px;">📚 书库统计</div>
            <div style="position: absolute; right: 15px; top: 15px; font-size: 11px; color: #555;" id="last-update-time"></div>
            <div style="display: flex; gap: 20px;" id="dash-stats">
                <div><div style="font-size: 24px; color: #fff; font-weight: bold;">__STAT_MD__</div><div style="font-size: 12px; color: #666;">已解析 MD</div></div>
                <div><div style="font-size: 24px; color: #fff; font-weight: bold;">__STAT_DOCX__</div><div style="font-size: 12px; color: #666;">已生成 DOCX</div></div>
                <div><div style="font-size: 24px; color: #ff9800; font-weight: bold;">__STAT_QUEUE__</div><div style="font-size: 12px; color: #666;">待转 Markdown</div></div>
            </div>
        </div>
    </div>
</div>
"""

ENGINE_BLOCK_HTML = """
<div style="flex: 1; min-width: 140px; background: #2a2a2a; border-radius: 8px; padding: 15px; border: 1px solid #333; position: relative;" id="engine-__NAME__">
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px;">
        <span style="font-weight: bold; font-size: 14px;">__NAME__</span>
        <span id="engine-dot-__NAME__" style="font-size: 12px; color: __COLOR__;">●</span>
    </div>
    <div style="font-size: 12px; color: #888;">__DESC__</div>
    <div id="engine-status-__NAME__" style="font-size: 11px; color: __COLOR__; margin-top: 5px; font-weight: bold;">__STATUS__</div>
    <div id="engine-task-__NAME__" style="font-size: 11px; color: #aaa; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" title="__TASK__">任务: __TASK__</div>
</div>
"""

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Tool to Markdown 总览控制台</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background: #2b2b2b; color: #eee; margin: 0; padding: 40px; }
        .container { max-width: 1100px; margin: 0 auto; width: 100%; box-sizing: border-box; display: flex; flex-direction: column; }
        h1 { text-align: center; font-weight: 300; margin-bottom: 40px; color: #fff; letter-spacing: 1px; }
        .card { background: #1e1e1e; border-radius: 10px; padding: 25px; margin-bottom: 25px; box-shadow: 0 8px 16px rgba(0,0,0,0.2); border: 1px solid #333; resize: vertical; overflow-y: auto; min-height: 120px; display: flex; flex-direction: column; }
        .card::-webkit-scrollbar { width: 8px; }
        .card::-webkit-scrollbar-track { background: #1e1e1e; border-radius: 4px; }
        .card::-webkit-scrollbar-thumb { background: #555; border-radius: 4px; }
        .card::-webkit-scrollbar-thumb:hover { background: #777; }
        .card::-webkit-resizer { background: repeating-linear-gradient(135deg, transparent, transparent 2px, #555 2px, #555 4px); border-radius: 0 0 10px 0; border-bottom: 2px solid #555; border-right: 2px solid #555; }
        
        /* 分组列表内层容器的滚动条和拖拽样式 */
        .inner-scroll { resize: vertical; overflow-y: auto; }
        .inner-scroll::-webkit-scrollbar { width: 8px; }
        .inner-scroll::-webkit-scrollbar-track { background: #1a1a1a; border-radius: 4px; }
        .inner-scroll::-webkit-scrollbar-thumb { background: #555; border-radius: 4px; }
        .inner-scroll::-webkit-scrollbar-thumb:hover { background: #777; }
        .inner-scroll::-webkit-resizer { background: repeating-linear-gradient(135deg, transparent, transparent 2px, #555 2px, #555 4px); border-radius: 0 0 6px 0; border-bottom: 2px solid #555; border-right: 2px solid #555; }
        .card h2 { margin-top: 0; font-size: 20px; border-bottom: 1px solid #333; padding-bottom: 12px; color: #6cb6ff; font-weight: 500; }
        .btn { padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 500; color: #fff; text-decoration: none; display: inline-block; white-space: nowrap; transition: all 0.2s; }
        .btn-green { background: #28a745; box-shadow: 0 2px 5px rgba(40,167,69,0.3); }
        .btn-green:hover { background: #218838; }
        .btn-red { background: #dc3545; box-shadow: 0 2px 5px rgba(220,53,69,0.3); }
        .btn-red:hover { background: #c82333; }
        .btn-blue { background: #0066cc; box-shadow: 0 2px 5px rgba(0,102,204,0.3); }
        .btn-blue:hover { background: #005bb5; }
        
        .book-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 15px; }
        .book-item { background: #2a2a2a; padding: 15px; border-radius: 8px; text-decoration: none; color: #ddd; transition: all 0.2s; display: flex; align-items: center; border: 1px solid #333; line-height: 1.5; font-size: 15px; }
        .book-item:hover { background: #333; border-color: #555; color: #fff; transform: translateY(-2px); }
        
        .form-group { display: flex; gap: 12px; align-items: center; margin-top: 15px; flex-wrap: nowrap; }
        select { padding: 10px 15px; border-radius: 6px; border: 1px solid #555; background: #2a2a2a; color: #fff; font-size: 14px; flex: 1; min-width: 0; outline: none; transition: border 0.2s; }
        select:focus { border-color: #0066cc; }
        
        #convert-status { margin-top: 15px; color: #28a745; font-family: Consolas, monospace; white-space: pre-wrap; font-size: 13px; background: #111; padding: 10px; border-radius: 6px; display: none; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📖 Tool to Markdown 控制台</h1>

        <div class="card" data-resize-id="card-task-control">
            <h2>⚙️ 后台任务控制</h2>
            <div id="ocr-status-container">
                __STATUS_BLOCK__
            </div>
        </div>

        __GROUPS_BLOCK__

        <div class="card" data-resize-id="card-convert">
            <h2>📄 文档格式转换 (Markdown ➡️ Word/PDF)</h2>
            <div class="form-group">
                <select id="md-select">
                    __MD_OPTIONS__
                </select>
                <button class="btn btn-blue" onclick="convertMD('docx')">转 Word</button>
                <button class="btn btn-blue" onclick="convertMD('pdf')" style="background:#8e44ad; box-shadow: 0 2px 5px rgba(142,68,173,0.3);">转 PDF</button>
            </div>
            <!-- 转换进度条 -->
            <div id="convert-progress-container" style="display:none; margin-top:15px;">
                <div style="display:flex; justify-content:space-between; font-size:12px; color:#aaa; margin-bottom:5px;">
                    <span id="convert-status-text">准备中...</span>
                    <span id="convert-status-percent">0%</span>
                </div>
                <div style="width:100%; background:#333; height:8px; border-radius:4px; overflow:hidden;">
                    <div id="convert-progress-bar" style="height:100%; width:0%; background:linear-gradient(90deg, #0066cc, #00cc66); transition:width 0.3s;"></div>
                </div>
            </div>
            <div id="convert-status" style="display:none; align-items:flex-start; background:#1e1e1e; padding:12px; border-radius:6px; margin-top:15px;">
                <span id="convert-status-msg" style="flex:1; white-space:pre-wrap; font-family:Consolas, monospace; font-size:13px; line-height:1.5;"></span>
                <button class="btn btn-blue" id="btn-open-folder" onclick="openOutputFolder()" style="display:none; margin-left:15px; white-space:nowrap; padding:6px 12px; font-size:13px;">📂 打开所在文件夹</button>
            </div>
        </div>

        <div class="card" data-resize-id="card-editor">
            <h2>📚 沉浸式校对 (已有单页JSON的书)</h2>
            <div class="book-grid">
                __BOOK_LINKS__
            </div>
        </div>

        <div class="card" data-resize-id="card-danger" style="border-color: #dc3545;">
            <h2 style="color: #dc3545;">🗑️ 危险区域：彻底删除书籍</h2>
            <div class="form-group">
                <select id="delete-select">
                    __MD_OPTIONS__
                </select>
                <button class="btn" style="background: #dc3545; box-shadow: 0 2px 5px rgba(220,53,69,0.3);" onclick="deleteBook()">彻底删除</button>
            </div>
            <div style="font-size: 13px; color: #888; margin-top: 12px; line-height: 1.6;">
                此操作将永久抹除该书在系统里的一切痕迹：<br>
                包括 Markdown、原图扫描件、JSON 缓存、TXT 缓存、Word/PDF 导出文件，以及 <code>newfiles</code> 里的源 PDF。<br>
                <b>此操作不可逆！</b>
            </div>
        </div>
    </div>

    <script>
        // 实时状态轮询
        function fetchStatus() {
            fetch('/api/status')
                .then(r => r.json())
                .then(data => {
                    // 更新主状态
                    let statusEl = document.getElementById('dash-status');
                    let btnEl = document.getElementById('dash-btn');
                    let startActions = document.getElementById('start-actions');
                    if (data.paused) {
                        statusEl.innerHTML = '<span style="color:#d39e00;">🟡 任务已暂停</span>';
                        btnEl.innerHTML = '▶️ 恢复任务'; btnEl.className = 'btn btn-green';
                        btnEl.style.display = 'block';
                        if (startActions) startActions.style.display = 'none';
                    } else if (data.running) {
                        statusEl.innerHTML = '<span style="color:#28a745;">🟢 任务运行中</span>';
                        btnEl.innerHTML = '⏸️ 暂停任务'; btnEl.className = 'btn btn-red';
                        btnEl.style.display = 'block';
                        if (startActions) startActions.style.display = 'none';
                    } else {
                        statusEl.innerHTML = '<span style="color:#dc3545;">🔴 任务未运行</span>';
                        btnEl.style.display = 'none';
                        if (startActions) startActions.style.display = 'flex';
                    }
                    
                    // 更新文字详情
                    document.getElementById('dash-details').innerHTML = `当前任务: <span style="color:#fff; font-weight:bold;">${data.current_book}</span> &nbsp;|&nbsp; 引擎: <span style="color:#e91e63; font-weight:bold;">${data.active_engine}</span> &nbsp;|&nbsp; 速度: <span style="color:#6cb6ff;">${data.speed}</span> &nbsp;|&nbsp; 进度: <span style="color:#28a745;">${data.progress_text}</span>`;
                    
                    // 更新进度条
                    document.getElementById('dash-progress-bar').style.width = data.progress_percent + '%';
                    
                    // 更新硬件监控
                    document.getElementById('sys-cpu-bar').style.width = data.sys_stats.cpu + '%';
                    document.getElementById('sys-cpu-text').innerText = data.sys_stats.cpu + '%';
                    document.getElementById('sys-mem-bar').style.width = data.sys_stats.mem + '%';
                    document.getElementById('sys-mem-text').innerText = data.sys_stats.mem + '%';
                    document.getElementById('sys-gpu-bar').style.width = data.sys_stats.gpu + '%';
                    document.getElementById('sys-gpu-text').innerText = data.sys_stats.gpu + '%';

                    // 更新统计数字
                    let statsHtml = document.getElementById('dash-stats');
                    statsHtml.innerHTML = `
                        <div><div style="font-size: 24px; color: #fff; font-weight: bold;">${data.stats.md}</div><div style="font-size: 12px; color: #666;">已解析 MD</div></div>
                        <div><div style="font-size: 24px; color: #fff; font-weight: bold;">${data.stats.docx}</div><div style="font-size: 12px; color: #666;">已生成 DOCX</div></div>
                        <div><div style="font-size: 24px; color: #ff9800; font-weight: bold;">${data.stats.queue}</div><div style="font-size: 12px; color: #666;">待转 Markdown</div></div>
                    `;
                    
                    // 更新转换进度
                    if (data.convert_status) {
                        const cs = data.convert_status;
                        const pc = document.getElementById('convert-progress-container');
                        const st = document.getElementById('convert-status-text');
                        const sp = document.getElementById('convert-status-percent');
                        const pb = document.getElementById('convert-progress-bar');
                        const resDiv = document.getElementById('convert-status');
                        
                        if (cs.running) {
                            pc.style.display = 'block';
                            resDiv.style.display = 'none';
                            st.innerText = cs.msg;
                            sp.innerText = cs.progress + '%';
                            pb.style.width = cs.progress + '%';
                        } else if (cs.progress === 100 && cs.msg !== '') {
                            if (pc.style.display !== 'none') {
                                pc.style.display = 'none';
                                resDiv.style.display = 'flex';
                                const msgSpan = document.getElementById('convert-status-msg');
                                msgSpan.style.color = cs.is_error ? '#dc3545' : '#28a745';
                                msgSpan.innerText = cs.msg;
                                
                                const btnOpen = document.getElementById('btn-open-folder');
                                if (!cs.is_error && !cs.msg.includes('❌')) {
                                    btnOpen.style.display = 'block';
                                } else {
                                    btnOpen.style.display = 'none';
                                }
                            }
                        }
                    }

                    // 更新时间戳
                    document.getElementById('last-update-time').innerText = new Date().toLocaleTimeString();

                    // 更新引擎小方块状态
                    data.engines.forEach(eng => {
                        let dot = document.getElementById(`engine-dot-${eng.name}`);
                        let txt = document.getElementById(`engine-status-${eng.name}`);
                        let taskEl = document.getElementById(`engine-task-${eng.name}`);
                        if (dot && txt && taskEl) {
                            if (!eng.ready) { 
                                dot.style.color = '#dc3545'; txt.style.color = '#dc3545'; txt.innerText = '未安装'; taskEl.innerText = '任务: -'; 
                            } else if (eng.active) { 
                                dot.style.color = '#00cc66'; txt.style.color = '#00cc66'; txt.innerText = '🟢 正在运行'; taskEl.innerText = '任务: ' + eng.task; taskEl.title = eng.task;
                            } else { 
                                dot.style.color = '#666'; txt.style.color = '#666'; txt.innerText = '⚪ 空闲'; taskEl.innerText = '任务: -'; 
                            }
                        }
                    });
                    const ocrEng = data.engines.find(e => e.name === 'RapidOCR');
                    const ocrBtn = document.getElementById('btn-start-ocr');
                    if (ocrEng && ocrBtn) {
                        ocrBtn.disabled = !ocrEng.ready;
                        ocrBtn.title = ocrEng.ready ? '' : 'RapidOCR 未安装：python -m pip install -r requirements-ocr.txt';
                    }
                }).catch(e => console.error(e));
        }

        // 页面加载立刻拉取一次，然后每 2 秒自动轮询
        window.onload = fetchStatus;
        setInterval(fetchStatus, 2000);

        function toggleMonitor() {
            fetch('/api/toggle_monitor')
                .then(r => r.json())
                .then(data => console.log('Monitor toggled:', data.status));
        }

        function startTask(type) {
            const btn = document.getElementById(type === 'tomd' ? 'btn-start-tomd' : 'btn-start-ocr');
            if (btn.disabled) return;
            const oldText = btn.innerText;
            btn.innerText = '启动中...';
            btn.disabled = true;
            setTimeout(() => { btn.disabled = false; btn.innerText = oldText; }, 2000);

            fetch(`/api/start_${type}`, { method: 'POST' })
                .then(r => r.json())
                .then(d => {
                    if (d.status !== 'ok') alert('启动失败：' + d.msg);
                    fetchStatus();
                })
                .catch(e => alert('请求失败：' + e));
        }

        function toggleOCR() {
            // 按钮防抖
            const btn = document.getElementById('dash-btn');
            if (btn.disabled) return;
            btn.disabled = true;
            setTimeout(() => btn.disabled = false, 2000);

            fetch('/api/toggle_ocr', { method: 'POST' })
                .then(r => r.json())
                .then(d => {
                    if (d.msg) {
                        let el = document.getElementById('toggle-msg');
                        if (!el) {
                            el = document.createElement('div');
                            el.id = 'toggle-msg';
                            el.style.cssText = 'margin-top:8px;font-size:13px;color:#8ab4f8;';
                            (document.querySelector('.card') || document.body).appendChild(el);
                        }
                        el.innerText = d.msg;
                    }
                    fetchStatus();
                })
                .catch(e => alert('操作失败：' + e));
        }

        function deleteBook() {
            const book = document.getElementById('delete-select').value;
            if (!book) return alert('请先选择一本书');
            if (!confirm(`⚠️ 严重警告 ⚠️\n\n你确定要彻底删除《${book}》吗？\n\n这将永久删除：\n- 书籍正文 (Markdown)\n- 原始扫描图 (Assets)\n- OCR 单页缓存 (JSON)\n- 文本缓存 (TXT)\n- 导出的 Word / PDF\n- 投递区源文件 (PDF/EPUB)\n\n一旦删除，数据无法恢复！`)) {
                return;
            }
            
            const btn = event.target;
            btn.disabled = true;
            btn.innerText = '正在抹除...';
            
            fetch('/api/delete_book', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ book: book })
            }).then(r => r.json()).then(d => {
                if (d.status === 'ok') {
                    alert(`已彻底删除《${book}》\n共清理了 ${d.count} 个文件和目录。`);
                    window.location.reload();
                } else {
                    alert('删除失败: ' + d.msg);
                    btn.disabled = false;
                    btn.innerText = '彻底删除';
                }
            }).catch(e => {
                alert('请求失败: ' + e);
                btn.disabled = false;
                btn.innerText = '彻底删除';
            });
        }

        // --- 尺寸记忆与持久化逻辑 ---
        window.addEventListener('DOMContentLoaded', () => {
            // 恢复之前保存的高度
            document.querySelectorAll('[data-resize-id]').forEach(el => {
                const savedHeight = localStorage.getItem('resize_' + el.dataset.resizeId);
                if (savedHeight) {
                    el.style.height = savedHeight;
                }
            });

            // 恢复分组的折叠状态
            document.querySelectorAll('.group-container').forEach(groupEl => {
                const resizeId = groupEl.dataset.resizeId;
                if (resizeId && resizeId.startsWith('group-')) {
                    const gName = resizeId.substring(6); // 去掉 'group-'
                    const isCollapsed = localStorage.getItem('group_collapsed_' + gName) === 'true';
                    const contentEl = document.getElementById('content-' + gName);
                    const arrowEl = document.getElementById('arrow-' + gName);
                    if (isCollapsed && contentEl && arrowEl) {
                        contentEl.style.display = 'none';
                        arrowEl.innerText = '▶';
                    }
                }
            });

            // 监听全局鼠标松开事件，保存最终高度
            document.addEventListener('mouseup', () => {
                document.querySelectorAll('[data-resize-id]').forEach(el => {
                    // 只有当元素被赋予了内联高度（即被拖拽过）才保存
                    if (el.style.height) {
                        localStorage.setItem('resize_' + el.dataset.resizeId, el.style.height);
                    }
                });
            });
        });

        // --- 折叠与拖拽逻辑 ---
        function toggleGroup(gName) {
            const el = document.getElementById('content-' + gName);
            const arrow = document.getElementById('arrow-' + gName);
            if (el.style.display === 'none') {
                el.style.display = 'block';
                arrow.innerText = '▼';
                localStorage.setItem('group_collapsed_' + gName, 'false');
            } else {
                el.style.display = 'none';
                arrow.innerText = '▶';
                localStorage.setItem('group_collapsed_' + gName, 'true');
            }
        }

        let draggedType = null;
        let draggedName = null;

        function dragStart(e, type, name) {
            // 阻止事件冒泡，防止拖拽书本时把整个组也拖起来
            e.stopPropagation();
            draggedType = type;
            draggedName = name;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', name);
        }

        function allowDrop(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
        }

        function dropBook(e, targetGroup, targetBook) {
            e.stopPropagation();
            if (draggedType !== 'book') return;
            e.preventDefault();
            if (draggedName === targetBook) return;

            fetch('/api/reorder', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ type: 'book', group: targetGroup, src: draggedName, dst: targetBook })
            }).then(r => r.json()).then(d => {
                if (d.status === 'ok') window.location.reload();
                else alert(d.msg);
            });
        }

        function dropGroup(e, targetGroup) {
            if (draggedType !== 'group') return;
            e.preventDefault();
            if (draggedName === targetGroup) return;

            fetch('/api/reorder', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ type: 'group', src: draggedName, dst: targetGroup })
            }).then(r => r.json()).then(d => {
                if (d.status === 'ok') window.location.reload();
                else alert(d.msg);
            });
        }

        function renameBook(oldName) {
            const newName = prompt('请输入新的书名（这将同步修改所有的 md、图片目录、JSON 缓存和 DOCX 等）：', oldName);
            if (!newName || newName === oldName) return;
            
            fetch('/api/rename_book', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ old: oldName, new: newName })
            }).then(r => r.json()).then(d => {
                if (d.status === 'ok') {
                    alert('重命名成功！');
                    window.location.reload();
                } else {
                    alert('重命名失败: ' + d.msg);
                }
            });
        }

        function createGroup() {
            const name = document.getElementById('new-group-name').value.trim();
            if (!name) return alert('请输入分组名称');
            fetch('/api/manage_group', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ action: 'create', group: name })
            }).then(r => r.json()).then(d => {
                if (d.status === 'ok') window.location.reload();
                else alert(d.msg);
            });
        }

        let lastSelectedItem = null;
        function handleSelectItem(e, el) {
            const cb = el.querySelector('.assign-cb');
            const items = Array.from(document.querySelectorAll('#assign-book-list .assign-item'));
            const isCtrl = e.ctrlKey || e.metaKey;
            const isShift = e.shiftKey;

            if (isShift && lastSelectedItem && items.includes(lastSelectedItem)) {
                const startIdx = items.indexOf(lastSelectedItem);
                const endIdx = items.indexOf(el);
                const min = Math.min(startIdx, endIdx);
                const max = Math.max(startIdx, endIdx);
                
                for (let i = min; i <= max; i++) {
                    items[i].querySelector('.assign-cb').checked = true;
                }
                window.getSelection().removeAllRanges(); // 防止拖拽时选中文字
            } else if (isCtrl) {
                cb.checked = !cb.checked;
                lastSelectedItem = el;
            } else {
                const shouldSelect = !cb.checked;
                items.forEach(item => item.querySelector('.assign-cb').checked = false);
                cb.checked = shouldSelect;
                lastSelectedItem = el;
            }
            updateAssignListUI();
        }

        function selectAllBooks() {
            document.querySelectorAll('#assign-book-list .assign-cb').forEach(cb => cb.checked = true);
            updateAssignListUI();
        }

        function selectNoneBooks() {
            document.querySelectorAll('#assign-book-list .assign-cb').forEach(cb => cb.checked = false);
            updateAssignListUI();
        }

        function updateAssignListUI() {
            document.querySelectorAll('#assign-book-list .assign-item').forEach(el => {
                if (el.querySelector('.assign-cb').checked) {
                    el.style.background = '#2a4a6a';
                } else {
                    el.style.background = 'transparent';
                }
            });
        }

        document.addEventListener('DOMContentLoaded', () => {
            const listEl = document.getElementById('assign-book-list');
            if (listEl) {
                listEl.addEventListener('keydown', function(e) {
                    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
                        e.preventDefault();
                        selectAllBooks();
                    }
                });
            }
            updateAssignListUI();
        });

        function assignBook() {
            const checkboxes = document.querySelectorAll('.assign-cb:checked');
            const books = Array.from(checkboxes).map(cb => cb.value);
            const group = document.getElementById('assign-group').value;
            if (books.length === 0 || !group) return alert('请至少选择一本书和一个目标分组');
            
            const params = new URLSearchParams();
            params.append('action', 'assign_multi');
            params.append('group', group);
            books.forEach(b => params.append('book', b));

            fetch('/api/manage_group', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: params
            }).then(r => r.json()).then(d => {
                if (d.status === 'ok') window.location.reload();
                else alert(d.msg);
            });
        }

        function removeFromGroup(group, book) {
            if (!confirm(`将《${book}》移出分组「${group}」？`)) return;
            fetch('/api/manage_group', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ action: 'remove', book: book, group: group })
            }).then(r => r.json()).then(d => {
                if (d.status === 'ok') window.location.reload();
                else alert(d.msg);
            });
        }

        function openOutputFolder() {
            const book = document.getElementById('md-select').value;
            if (!book) return;
            fetch('/api/open_folder?book=' + encodeURIComponent(book))
                .then(r => {
                    if (!r.ok) alert('无法打开文件夹，请手动前往 outputs/docx 查找。');
                })
                .catch(e => alert('调用失败: ' + e));
        }

        function convertMD(targetFmt) {
            const book = document.getElementById('md-select').value;
            if (!book) return alert('请先选择一本书');
            
            document.getElementById('convert-status').style.display = 'none';
            document.getElementById('convert-progress-container').style.display = 'block';
            document.getElementById('convert-progress-bar').style.width = '5%';
            document.getElementById('convert-status-text').innerText = '正在启动转换任务...';
            document.getElementById('convert-status-percent').innerText = '5%';
            
            fetch('/api/convert_md', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ book: book, format: targetFmt })
            }).then(r => {
                if (r.status === 429) {
                    alert('已有转换任务正在进行，请稍候');
                    document.getElementById('convert-progress-container').style.display = 'none';
                } else if (!r.ok) {
                    alert('转换请求失败（HTTP ' + r.status + '）');
                    document.getElementById('convert-progress-container').style.display = 'none';
                }
            }).catch(e => {
                alert('请求失败: ' + e + '\n（若提示 Failed to fetch，通常说明控制台进程已退出，请重新双击 StartConsole.vbs）');
            });
        }
    </script>
</body>
</html>
"""

EDITOR_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>沉浸式校对 - __BOOK__</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 0; padding: 0; background: #2b2b2b; color: #eee; height: 100vh; display: flex; flex-direction: column; overflow: hidden; }
        .header { background: #1e1e1e; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 5px rgba(0,0,0,0.5); z-index: 10; }
        .header h1 { margin: 0; font-size: 18px; font-weight: normal; display: flex; align-items: center; gap: 15px; }
        .header a.home-btn { color: #aaa; text-decoration: none; font-size: 14px; background: #333; padding: 5px 10px; border-radius: 4px; }
        .header a.home-btn:hover { background: #555; color: #fff; }
        .status { font-size: 14px; }
        .main-container { display: flex; flex: 1; overflow: hidden; }
        
        .left-pane { flex: 0 0 45%; background: #1a1a1a; overflow-y: auto; display: flex; justify-content: center; align-items: flex-start; padding: 20px; border-right: 1px solid #444; }
        .left-pane img { max-width: 100%; box-shadow: 0 0 10px rgba(0,0,0,0.8); background: #fff; }
        .no-image { color: #666; margin-top: 50px; font-size: 16px; }
        
        .right-pane { flex: 1; display: flex; flex-direction: column; background: #fdfdfd; color: #333; padding: 20px 40px; overflow-y: auto; }
        textarea { width: 100%; height: 100%; border: none; outline: none; resize: none; font-family: "Songti SC", "SimSun", "STSong", serif; font-size: 16px; line-height: 1.8; color: #222; background: transparent; padding: 0; margin-bottom: 20px; }
        
        .toolbar { background: #1e1e1e; padding: 10px 20px; display: flex; justify-content: space-between; align-items: center; border-top: 1px solid #444; z-index: 10; }
        .btn { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; color: #fff; text-decoration: none; display: inline-block; }
        .btn-blue { background: #0066cc; }
        .btn-green { background: #28a745; }
        .btn-blue:hover { background: #005bb5; }
        .btn-green:hover { background: #218838; }
        .page-info { font-size: 14px; color: #aaa; display: flex; align-items: center; gap: 15px; }
        .jump-input { width: 60px; padding: 5px; border-radius: 4px; border: 1px solid #555; background: #333; color: #fff; text-align: center; font-size: 14px; }
        .jump-input:focus { outline: none; border-color: #0066cc; }
        #toast { visibility: hidden; min-width: 250px; background-color: #4CAF50; color: #fff; text-align: center; border-radius: 4px; padding: 12px; position: fixed; z-index: 100; left: 50%; top: 30px; transform: translateX(-50%); font-size: 15px; opacity: 0; transition: opacity 0.3s; }
        #toast.show { visibility: visible; opacity: 1; }
    </style>
</head>
<body>
    <div id="toast">保存成功！</div>
    <div class="header">
        <h1>
            <a href="/" class="home-btn">🏠 返回首页</a>
            <span id="header-title">📖 __BOOK_TITLE__ | 物理页 __PAGE__ (书内页: __REAL_PAGE__)</span>
        </h1>
        <div class="status">
            __STATUS_BADGE__
        </div>
    </div>

    <div class="main-container">
        <div class="left-pane" id="img-container">
            __IMAGE_CONTENT__
        </div>
        <div class="right-pane">
            <div style="font-size: 14px; color: #888; margin-bottom: 15px; border-bottom: 1px solid #eee; padding-bottom: 10px; display: flex; justify-content: space-between;">
                <span>当前实体书页码：<strong id="real-page-display" style="font-size: 20px; color: #333; margin-left: 5px;">__REAL_PAGE__</strong></span>
                <span id="json-label" style="font-size:12px;color:#ccc;">JSON: p__PAGE__.json</span>
            </div>
            <textarea id="text-editor" spellcheck="false" placeholder="输入文字..."></textarea>
        </div>
    </div>

    <div class="toolbar">
        <div>
            <a href="#" id="btn-prev" class="btn btn-blue">⬅️ 上一页</a>
            <a href="#" id="btn-next" class="btn btn-blue">下一页 ➡️</a>
        </div>
        <div class="page-info">
            <span>跳转到物理页：</span>
            <input type="number" id="jump-box" class="jump-input" min="1" max="__TOTAL__" value="__PAGE__">
            <span>/ __TOTAL__</span>
            <span style="margin-left:20px; font-size:12px; color:#666;">快捷键：← → 翻页 | 长按连翻 | PgUp/PgDn 跨10页 | Ctrl+S 保存</span>
        </div>
        <div>
            <button id="btn-save" class="btn btn-green">💾 保存本页 (Ctrl+S)</button>
        </div>
    </div>

    <script>
        const book = __BOOK_JS__;
        let currentPage = parseInt("__PAGE__");
        let total = parseInt("__TOTAL__");
        let initialText = __TEXT_JS__;
        
        let isFetching = false;
        let holdTimer = null;
        let repeatTimer = null;

        const editor = document.getElementById('text-editor');
        const jumpBox = document.getElementById('jump-box');
        const toast = document.getElementById("toast");

        editor.value = initialText;
        editor.defaultValue = initialText;

        function showToast() {
            toast.className = "show";
            setTimeout(function(){ toast.className = toast.className.replace("show", ""); }, 2000);
        }

        function saveText() {
            const text = editor.value;
            fetch('/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
                body: new URLSearchParams({ book: book, page: currentPage, text: text })
            }).then(r => r.text()).then(res => {
                if (res === 'ok') {
                    editor.defaultValue = text;
                    showToast();
                } else {
                    alert('保存失败: ' + res);
                }
            });
        }

        function fetchPage(target) {
            target = parseInt(target);
            if (isNaN(target) || target < 1 || target > total) {
                alert('页码无效！请输入 1 到 ' + total + ' 之间的数字。');
                return;
            }
            if (isFetching) return;
            
            const currentText = editor.value;
            const originalText = editor.defaultValue;
            if (currentText !== originalText) {
                if (!confirm('当前页有未保存的修改，翻页将丢失这些修改，是否坚持翻页？(建议先按 Ctrl+S 保存)')) {
                    jumpBox.value = currentPage;
                    return;
                }
            }

            isFetching = true;
            fetch(`/api/page?book=${encodeURIComponent(book)}&page=${target}`)
                .then(r => r.json())
                .then(data => {
                    if (data.status === 'ok') {
                        currentPage = data.page;
                        editor.value = data.text;
                        editor.defaultValue = data.text;
                        document.getElementById('img-container').innerHTML = data.img_html;
                        document.getElementById('header-title').innerText = `📖 ${book} | 物理页 ${data.page} (书内页: ${data.real_page})`;
                        document.getElementById('real-page-display').innerText = data.real_page;
                        document.getElementById('json-label').innerText = `JSON: p${data.page}.json`;
                        jumpBox.value = data.page;
                        history.replaceState(null, '', `/?book=${encodeURIComponent(book)}&page=${data.page}`);
                    }
                    isFetching = false;
                })
                .catch(e => { console.error(e); isFetching = false; });
        }

        document.getElementById('btn-prev').addEventListener('click', function(e) { e.preventDefault(); fetchPage(currentPage - 1); });
        document.getElementById('btn-next').addEventListener('click', function(e) { e.preventDefault(); fetchPage(currentPage + 1); });
        document.getElementById('btn-save').addEventListener('click', saveText);
        
        jumpBox.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                fetchPage(this.value);
            }
        });

        document.addEventListener('keydown', function(e) {
            if (e.ctrlKey && e.key === 's') {
                e.preventDefault();
                saveText();
                return;
            }

            if (document.activeElement.tagName === 'INPUT') return;

            if (document.activeElement.tagName !== 'TEXTAREA' || e.altKey) {
                let delta = 0;
                if (e.key === 'ArrowLeft') delta = -1;
                else if (e.key === 'ArrowRight') delta = 1;
                else if (e.key === 'PageUp') { e.preventDefault(); fetchPage(currentPage - 10); return; }
                else if (e.key === 'PageDown') { e.preventDefault(); fetchPage(currentPage + 10); return; }

                if (delta !== 0) {
                    if (e.repeat) return;
                    fetchPage(currentPage + delta);

                    holdTimer = setTimeout(() => {
                        repeatTimer = setInterval(() => {
                            let next = currentPage + delta;
                            if (next >= 1 && next <= total) {
                                fetchPage(next);
                            } else {
                                clearInterval(repeatTimer);
                            }
                        }, 150);
                    }, 300);
                }
            }
        });

        document.addEventListener('keyup', function(e) {
            if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                if (holdTimer) clearTimeout(holdTimer);
                if (repeatTimer) clearInterval(repeatTimer);
            }
        });
    </script>
</body>
</html>
"""

def get_books():
    json_dir = ROOT / 'library' / 'ocr_json'
    if not json_dir.exists():
        return []
    return sorted([d.name for d in json_dir.iterdir() if d.is_dir()])

def build_index():
    # 1. 获取仪表盘数据
    data = get_dashboard_data()
    
    if data['paused']:
        status_text = '<span style="color:#d39e00;">🟡 任务已暂停</span>'
        btn_class = 'btn-green'
        btn_text = '▶️ 恢复任务'
        btn_display = 'block'
        start_actions_display = 'none'
    elif data['running']:
        status_text = '<span style="color:#28a745;">🟢 任务运行中</span>'
        btn_class = 'btn-red'
        btn_text = '⏸️ 暂停任务'
        btn_display = 'block'
        start_actions_display = 'none'
    else:
        status_text = '<span style="color:#dc3545;">🔴 任务未运行</span>'
        btn_class = 'btn-green'
        btn_text = '▶️ 启动任务'
        btn_display = 'none'
        start_actions_display = 'flex'

    # 组装四轨引擎
    engine_blocks = ""
    for eng in data['engines']:
        if not eng['ready']:
            color = '#dc3545'
            status = '未安装'
            task = '-'
        elif eng.get('active'):
            color = '#00cc66'
            status = '🟢 正在运行'
            task = eng.get('task', '-')
        else:
            color = '#666'
            status = '⚪ 空闲'
            task = '-'
            
        engine_blocks += ENGINE_BLOCK_HTML.replace('__NAME__', eng['name']).replace('__DESC__', eng['desc']).replace('__COLOR__', color).replace('__STATUS__', status).replace('__TASK__', task)

    status_block = STATUS_HTML
    status_block = status_block.replace('__STATUS_TEXT__', status_text)
    status_block = status_block.replace('__CURRENT_BOOK__', data['current_book'])
    status_block = status_block.replace('__ACTIVE_ENGINE__', data['active_engine'])
    status_block = status_block.replace('__SPEED__', data['speed'])
    status_block = status_block.replace('__PROGRESS_TEXT__', data['progress_text'])
    status_block = status_block.replace('__PROGRESS_PERCENT__', str(data['progress_percent']))
    status_block = status_block.replace('__BTN_CLASS__', btn_class)
    status_block = status_block.replace('__BTN_TEXT__', btn_text)
    status_block = status_block.replace('__BTN_DISPLAY__', btn_display)
    status_block = status_block.replace('__START_ACTIONS_DISPLAY__', start_actions_display)
    monitor_disabled = ROOT / 'scripts' / '.sys_monitor_disabled'
    status_block = status_block.replace('__MONITOR_CHECKED__', '' if monitor_disabled.exists() else 'checked')
    status_block = status_block.replace('__ENGINE_BLOCKS__', engine_blocks)
    status_block = status_block.replace('__STAT_MD__', str(data['stats']['md']))
    status_block = status_block.replace('__STAT_DOCX__', str(data['stats']['docx']))
    status_block = status_block.replace('__STAT_QUEUE__', str(data['stats']['queue']))

    # 2. MD 文件列表（按分组渲染）
    mds = [m for m in sorted(list((ROOT / 'library').glob('*.md'))) if m.name.lower() != 'readme.md']
    all_md_stems = {m.stem for m in mds}
    groups = load_groups()
    
    # 确保“一般库存”存在，并自动将新书纳入
    if '一般库存' not in groups:
        groups['一般库存'] = []
        
    grouped_stems = set()
    for items in groups.values():
        grouped_stems.update(items)
        
    groups_changed = False
    for m in mds:
        if m.stem not in grouped_stems:
            groups['一般库存'].append(m.stem)
            groups_changed = True
            
    if groups_changed:
        save_groups(groups)
        grouped_stems = set()
        for items in groups.values():
            grouped_stems.update(items)

    # --- 渲染转换下拉框 (按分组分类) ---
    md_options = ""
    idx = 1
    for g_name, items in groups.items():
        valid_items = [s for s in items if s in all_md_stems]
        if not valid_items: continue
        md_options += f'<optgroup label="📂 {html.escape(g_name)}">'
        for stem in valid_items:
            md_options += f'<option value="{html.escape(stem)}">{idx}. {html.escape(stem)}</option>'
            idx += 1
        md_options += '</optgroup>'
        
    # --- 渲染管理界面 HTML ---
    groups_html = ""
    for g_name, items in groups.items():
        valid_items = [s for s in items if s in all_md_stems]
        if not valid_items and g_name != '一般库存': continue
        
        # 组内书本，支持拖拽
        btns = ""
        for s in valid_items:
            btns += f'''<div class="book-item" draggable="true" ondragstart="dragStart(event, 'book', '{html.escape(s)}')" ondragover="allowDrop(event)" ondrop="dropBook(event, '{html.escape(g_name)}', '{html.escape(s)}')" style="background:#333; padding:8px 12px; border-radius:6px; font-size:13px; margin:6px 0; display:flex; justify-content:space-between; align-items:center; word-break: break-all; cursor: grab;">
                <span>☰ {html.escape(s)}</span> 
                <div>
                    <a href="#" onclick="renameBook('{html.escape(s)}')" style="text-decoration:none; margin-right:10px; font-size:16px;" title="重命名">✏️</a>
                    <a href="#" onclick="removeFromGroup('{html.escape(g_name)}', '{html.escape(s)}')" style="color:#dc3545; text-decoration:none; margin-left:5px; font-size:18px; font-weight:bold;" title="移出分组">×</a>
                </div>
            </div>'''
            
        # 组本身支持拖拽换序，点击标题折叠
        groups_html += f'''<div class="group-container" data-resize-id="group-{html.escape(g_name)}" draggable="true" ondragstart="dragStart(event, 'group', '{html.escape(g_name)}')" ondragover="allowDrop(event)" ondrop="dropGroup(event, '{html.escape(g_name)}')" style="margin-bottom:15px; border:1px solid #444; border-radius:6px; padding:10px; cursor: grab; resize: vertical; overflow-y: auto; min-height: 60px;">
            <div style="position: sticky; top: -10px; z-index: 10; background: #1a1a1a; display:flex; justify-content:space-between; align-items:center; cursor: pointer; padding: 10px; margin: -10px -10px 10px -10px; border-bottom: 1px solid transparent;" onclick="toggleGroup('{html.escape(g_name)}')">
                <b style="font-size:15px; color:#6cb6ff;">📂 {html.escape(g_name)} ({len(valid_items)})</b>
                <span id="arrow-{html.escape(g_name)}" style="color:#888;">▼</span>
            </div>
            <div id="content-{html.escape(g_name)}" style="margin-top:10px; transition: max-height 0.3s;">
                {btns}
            </div>
        </div>'''
        
    if not groups_html:
        groups_html = '<div style="color:#888; font-size:13px;">暂无分组数据。</div>'

    # “分配到组”只能从“一般库存”里挑，或者反向操作
    available_to_assign = [s for s in groups.get('一般库存', []) if s in all_md_stems]
    assign_options = "".join(
        f'<div class="assign-item" onclick="handleSelectItem(event, this)" style="display:flex; align-items:flex-start; padding:5px 8px; cursor:pointer; border-radius:4px; transition: background 0.1s; user-select:none;">' 
        f'<input type="checkbox" class="assign-cb" value="{html.escape(s)}" style="margin-right:8px; margin-top:3px; pointer-events:none;"> ' 
        f'<span style="font-size:13px; color:#eee; word-break:break-all; line-height:1.4;">{html.escape(s)}</span></div>' 
        for s in available_to_assign
    )
    group_names = "".join(f'<option value="{html.escape(g)}">{html.escape(g)}</option>' for g in groups)

    groups_block = f'''
    <div class="card" data-resize-id="card-group-manage">
        <h2>📂 书籍分组管理</h2>
        <div style="display:flex; gap:10px; margin-bottom:15px;">
            <input type="text" id="new-group-name" placeholder="新建分组名称 (如: 马哲)" style="flex:1; padding:8px; border-radius:4px; border:1px solid #555; background:#222; color:#fff;">
            <button class="btn btn-blue" onclick="createGroup()">新建分组</button>
        </div>
        <div style="display:flex; flex-direction:column; gap:8px; margin-bottom:15px;">
            <div style="display:flex; gap:10px; align-items:center;">
                <div style="flex:1; font-size:13px; color:#aaa;">请选择书籍（支持 Shift 连选、Ctrl 多选、Ctrl+A 全选）：</div>
                <button class="btn" style="background:#555; padding:4px 10px; font-size:12px; border-radius:4px;" onclick="selectAllBooks()">全选</button>
                <button class="btn" style="background:#555; padding:4px 10px; font-size:12px; border-radius:4px;" onclick="selectNoneBooks()">取消全选</button>
            </div>
            <div style="display:flex; gap:10px;">
                <div id="assign-book-list" data-resize-id="assign-book-list" tabindex="0" style="flex:2; height: 180px; min-height: 100px; resize: vertical; overflow-y: auto; background: #1a1a1a; border: 1px solid #555; border-radius: 4px; padding: 5px; display:flex; flex-direction:column; outline:none;">
                    {assign_options}
                </div>
                <div style="display:flex; flex-direction:column; gap:10px; flex:1;">
                    <select id="assign-group" style="padding:8px; border-radius:4px; border:1px solid #555; background:#222; color:#fff; width:100%;">
                        <option value="">-- 选择目标分组 --</option>
                        {group_names}
                    </select>
                    <button class="btn btn-green" style="padding:10px; width:100%;" onclick="assignBook()">分配到组</button>
                </div>
            </div>
        </div>
        <div class="inner-scroll" data-resize-id="inner-group-list" style="background:#1a1a1a; padding:15px; border-radius:6px; height: 350px; min-height: 150px; display: flex; flex-direction: column;">
            {groups_html}
        </div>
    </div>
    '''

    # 3. 书籍列表
    books = get_books()
    if books:
        book_links = "".join(f'<a href="/?book={urllib.parse.quote(b)}" class="book-item">📚 {html.escape(b)}</a>' for b in books)
    else:
        book_links = '<div style="color:#888;">暂无包含单页 JSON 的书籍。请先运行 OCR 或导入 Markdown。</div>'

    return INDEX_TEMPLATE.replace('__STATUS_BLOCK__', status_block).replace('__GROUPS_BLOCK__', groups_block).replace('__MD_OPTIONS__', md_options).replace('__BOOK_LINKS__', book_links)

def build_editor(book, page):
    recs = S.load_book(book)
    if not recs:
        return f'<h1>找不到 {book} 的 JSON 数据</h1>'

    if not page:
        page = 1
    page = int(page)

    rec = next((r for r in recs if r['physical_index'] == page), None)
    if not rec:
        return f'<h1>找不到物理页 {page}</h1>'

    text = rec.get('text', '')
    images = rec.get('images', [])
    if not images and text:
        # 忽略路径中的括号，直接锚定扩展名
        m = re.search(r'!\[[^\]]*\]\((.*?\.(?:jpg|jpeg|png|bmp|webp))\)', text, re.I)
        if m:
            images = [m.group(1)]
            
    if images:
        img_path = images[0].replace('\\', '/')
        if not img_path.startswith('assets/'):
            img_path = f'assets/{book}/{img_path}'
        safe_path = urllib.parse.quote(img_path, safe='/')
        img_html = f'<img src="/library/{safe_path}" alt="Page {page}">'
    else:
        img_html = '<div class="no-image">此页没有原始扫描图</div>'

    if is_paused():
        badge = '<span style="color:#d39e00;">🟡 任务已暂停</span>'
    elif is_running():
        badge = '<span style="color:#28a745;">🟢 任务运行中</span>'
    else:
        badge = '<span style="color:#dc3545;">🔴 任务未运行</span>'

    real_page = str(rec.get('real_page_num', '-'))
    total = len(recs)

    content = EDITOR_TEMPLATE.replace('__BOOK_TITLE__', html.escape(book))
    content = content.replace('__BOOK_JS__', json.dumps(book))
    content = content.replace('__PAGE__', str(page))
    content = content.replace('__TOTAL__', str(total))
    content = content.replace('__TEXT_JS__', json.dumps(text))
    content = content.replace('__REAL_PAGE__', real_page)
    content = content.replace('__IMAGE_CONTENT__', img_html)
    content = content.replace('__STATUS_BADGE__', badge)

    return content

class MyTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """多线程 TCP 服务器。

    原来只继承 TCPServer（单线程），浏览器刷新时会并发请求首页、favicon、
    /api/status 等多个 URL；任何一个慢请求都会把后面全部堵住，表现为页面
    白屏卡死。ThreadingMixIn 让每个请求独立线程处理。
    """
    allow_reuse_address = True
    daemon_threads = True

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        book = qs.get('book', [None])[0]
        page = qs.get('page', [None])[0]

        if parsed.path == '/api/toggle_monitor':
            flag = ROOT / 'scripts' / '.sys_monitor_disabled'
            if flag.exists():
                flag.unlink()
                resp = {'status': 'on'}
            else:
                flag.touch()
                resp = {'status': 'off'}
            body = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == '/api/open_folder':
            book = qs.get('book', [''])[0]
            if book:
                docx_path = ROOT / 'outputs' / 'docx' / f'{book}.docx'
                pdf_path = ROOT / 'outputs' / 'docx' / f'{book}.pdf'
                # 优先选中实际存在的文件，如果都没有就打开目录
                target = None
                if docx_path.exists():
                    target = docx_path
                elif pdf_path.exists():
                    target = pdf_path
                
                try:
                    if target:
                        subprocess.Popen(['explorer', '/select,', str(target)])
                    else:
                        os.startfile(str(ROOT / 'outputs' / 'docx'))
                    self.send_response(200)
                    self.end_headers()
                except Exception as e:
                    self.send_response(500)
                    self.end_headers()
                return
            self.send_response(400)
            self.end_headers()
            return

        if parsed.path == '/api/status':
            resp = get_dashboard_data()
            body = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == '/api/page':
            try:
                page = int(page)
                recs = S.load_book(book)
                rec = next((r for r in recs if r['physical_index'] == page), None)
                if rec:
                    text = rec.get('text', '')
                    images = rec.get('images', [])
                    if not images and text:
                        m = re.search(r'!\[[^\]]*\]\((.*?\.(?:jpg|jpeg|png|bmp|webp))\)', text, re.I)
                        if m:
                            images = [m.group(1)]
                            
                    if images:
                        img_path = images[0].replace('\\', '/')
                        if not img_path.startswith('assets/'):
                            img_path = f'assets/{book}/{img_path}'
                        safe_path = urllib.parse.quote(img_path, safe='/')
                        img_html = f'<img src="/library/{safe_path}" alt="Page {page}">'
                    else:
                        img_html = '<div class="no-image">此页没有原始扫描图</div>'
                    resp = {
                        'status': 'ok', 'page': page, 'text': text,
                        'real_page': rec.get('real_page_num', '-'), 'img_html': img_html
                    }
                else:
                    resp = {'status': 'error', 'msg': 'page not found'}
            except Exception as e:
                resp = {'status': 'error', 'msg': str(e)}
            
            body = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path.startswith('/library/'):
            # 解析 URL 编码（处理路径中的中文和特殊字符）
            decoded_path = urllib.parse.unquote(parsed.path)
            file_path = ROOT / decoded_path.lstrip('/')
            
            if file_path.exists() and file_path.is_file():
                self.send_response(200)
                suffix = file_path.suffix.lower()
                if suffix in ('.jpg', '.jpeg'):
                    self.send_header('Content-Type', 'image/jpeg')
                elif suffix == '.png':
                    self.send_header('Content-Type', 'image/png')
                elif suffix == '.bmp':
                    self.send_header('Content-Type', 'image/bmp')
                elif suffix == '.webp':
                    self.send_header('Content-Type', 'image/webp')
                elif suffix == '.gif':
                    self.send_header('Content-Type', 'image/gif')
                self.end_headers()
                self.wfile.write(file_path.read_bytes())
                return

        if book:
            content = build_editor(book, page)
        else:
            content = build_index()

        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(content.encode('utf-8'))

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get('Content-Length', 0))
        qs = urllib.parse.parse_qs(self.rfile.read(length).decode('utf-8'))

        if parsed.path == '/api/delete_book':
            book = qs.get('book', [''])[0]
            if book:
                try:
                    deleted = delete_book_fully(book)
                    resp = {'status': 'ok', 'count': len(deleted), 'msg': f'已删除 {len(deleted)} 项数据'}
                except Exception as e:
                    resp = {'status': 'error', 'msg': str(e)}
                body = json.dumps(resp).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        if parsed.path == '/api/rename_book':
            old_name = qs.get('old', [''])[0]
            new_name = qs.get('new', [''])[0]
            ok, msg = rename_book_fully(old_name, new_name)
            resp = {'status': 'ok' if ok else 'error', 'msg': msg}
            body = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == '/api/reorder':
            rtype = qs.get('type', [''])[0]
            src = qs.get('src', [''])[0]
            dst = qs.get('dst', [''])[0]
            target_group = qs.get('group', [''])[0]
            groups = load_groups()
            
            try:
                if rtype == 'group':
                    if src in groups and dst in groups:
                        keys = list(groups.keys())
                        src_idx = keys.index(src)
                        dst_idx = keys.index(dst)
                        keys.insert(dst_idx, keys.pop(src_idx))
                        groups = {k: groups[k] for k in keys}
                elif rtype == 'book':
                    if target_group in groups and src in groups[target_group] and dst in groups[target_group]:
                        items = groups[target_group]
                        src_idx = items.index(src)
                        dst_idx = items.index(dst)
                        items.insert(dst_idx, items.pop(src_idx))
                        
                save_groups(groups)
                resp = {'status': 'ok'}
            except Exception as e:
                resp = {'status': 'error', 'msg': str(e)}
                
            body = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == '/api/manage_group':
            action = qs.get('action', [''])[0]
            group = qs.get('group', [''])[0]
            book = qs.get('book', [''])[0]
            groups = load_groups()
            
            try:
                if action == 'create':
                    if group not in groups:
                        groups[group] = []
                elif action == 'assign':
                    if group in groups:
                        # 先从其他组移除
                        for g in groups:
                            if book in groups[g]:
                                groups[g].remove(book)
                        if book not in groups[group]:
                            groups[group].append(book)
                elif action == 'assign_multi':
                    books_list = qs.get('book', [])
                    if group in groups:
                        for b in books_list:
                            for g in groups:
                                if b in groups[g]:
                                    groups[g].remove(b)
                            if b not in groups[group]:
                                groups[group].append(b)
                elif action == 'remove':
                    if group in groups and book in groups[group]:
                        groups[group].remove(book)
                save_groups(groups)
                resp = {'status': 'ok'}
            except Exception as e:
                resp = {'status': 'error', 'msg': str(e)}
                
            body = json.dumps(resp).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == '/save':
            book = qs.get('book', [''])[0]
            page = int(qs.get('page', ['0'])[0])
            text = qs.get('text', [''])[0]

            rec = S.load(book, page)
            if rec:
                ok = apply_text(book, rec, text, prompt='人工校对直接修改', engine='human', rebuild=True)
                if ok:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'ok')
                    return
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b'fail')
            return

        if parsed.path == '/api/toggle_ocr':
            try:
                resp = toggle_ocr()
            except Exception as e:
                resp = {'action': 'error', 'msg': '%s: %s' % (type(e).__name__, e)}
            body = json.dumps(resp, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path in ('/api/start_tomd', '/api/start_ocr'):
            if parsed.path == '/api/start_ocr' and importlib.util.find_spec('rapidocr_openvino') is None:
                resp = {'status': 'error', 'msg': 'RapidOCR 未安装：python -m pip install -r requirements-ocr.txt'}
            elif is_running():
                resp = {'status': 'error', 'msg': '当前已有任务在运行，请先暂停或等待结束'}
            else:
                try:
                    if parsed.path == '/api/start_tomd':
                        pid = launch_process('to_md_parallel.py', ['-j', '5'])
                    else:
                        pid = launch_process('ocr_all_batch.py')
                    resp = {'status': 'ok', 'msg': '已启动任务', 'pid': pid}
                except Exception as e:
                    resp = {'status': 'error', 'msg': str(e)}
            body = json.dumps(resp, ensure_ascii=False).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == '/api/convert_md':
            if CONVERT_TASK.get('running'):
                self.send_response(429)
                self.end_headers()
                self.wfile.write(b'busy')
                return
                
            book = qs.get('book', [''])[0]
            fmt = qs.get('format', ['docx'])[0]
            
            threading.Thread(target=run_convert_task, args=(book, fmt), daemon=True).start()
            
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'started')
            return

def _port_free(port):
    """用与正式监听一致的方式实测端口是否可用。

    旧实现用 connect_ex('127.0.0.1', port) 探测：只监听 IPv6 的进程（例如
    绑在 :: 上的 Manager.exe）探不到，于是误判为空闲；随后尝试强杀受保护
    进程又会静默失败，最终仍返回被占端口，bind 时抛 WinError 10013。
    改为直接 bind("", port) 实测，与 TCPServer 的行为一致。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            return True
        except OSError:
            return False


def find_free_port(start_port, max_tries=20):
    """从 start_port 起找一个真正可 bind 的端口，被占则顺延。"""
    for port in range(start_port, start_port + max_tries):
        if _port_free(port):
            return port
    raise RuntimeError(
        f"端口 {start_port}-{start_port + max_tries - 1} 均被占用，请释放后重试"
    )

def open_browser(port):
    time.sleep(1.5)
    webbrowser.open(f"http://127.0.0.1:{port}")

def watch_source():
    """热更新：源码变动时重启。

    以前用 `sys.argv + ['--no-browser']` 重启，每重启一次多一个参数，
    最终出现 `--no-browser --no-browser --no-browser …` 且起出多个实例。
    现在用固定 argv + 环境变量标记。
    """
    src = Path(__file__).resolve()
    try:
        last_mtime = src.stat().st_mtime
    except OSError:
        return
    while True:
        time.sleep(1.0)
        try:
            mtime = src.stat().st_mtime
            if mtime > last_mtime + 0.001:
                last_mtime = mtime
                print("\n🔄 检测到代码更新，正在热重启...")
                time.sleep(0.5)
                env = dict(os.environ, CONSOLE_RESTART='1')
                subprocess.Popen(
                    [sys.executable, str(src), '--no-browser'],
                    cwd=str(ROOT), env=env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS)
                os._exit(0)          # 新进程已接管，本进程直接退出（execve 在 Windows 不可靠）
        except Exception:
            pass


def running_console():
    """已有控制台在跑就返回它的端口，避免重复起实例。"""
    import urllib.request
    for port in range(DEFAULT_PORT, DEFAULT_PORT + 11):
        try:
            # 放宽容错时间，防止机器满载时误判为“没在运行”而导致多开
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/status',
                                        timeout=1.5) as r:
                if b'"running"' in r.read(300):
                    return port
        except Exception:
            continue
    return None

if __name__ == '__main__':
    # 监听关闭信号，干净退出
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    threading.Thread(target=monitor_resources_loop, daemon=True).start()

    # 启动前全量清理旧实例（防止因权限或异常导致的多实例/多端口）
    try:
        for p in psutil.process_iter(['pid', 'cmdline']):
            cmd = ' '.join(p.info.get('cmdline') or [])
            if 'review_ui.py' in cmd and p.pid != os.getpid():
                p.kill()
    except Exception:
        pass

    restarting = os.environ.get('CONSOLE_RESTART') == '1'
    no_browser = restarting or '--no-browser' in sys.argv

    # 已有实例在跑就不再起第二个（热重启除外）
    if not restarting:
        alive = running_console()
        if alive:
            print(f"\nℹ️ 控制台已在运行：http://127.0.0.1:{alive}（正在为您打开页面）\n")
            webbrowser.open(f"http://127.0.0.1:{alive}")
            sys.exit(0)

    base = int(os.environ.get('CONSOLE_PORT') or DEFAULT_PORT)
    port = find_free_port(base)

    # 首次启动才自动开浏览器
    if not no_browser:
        threading.Thread(target=open_browser, args=(port,), daemon=True).start()
        
    # 热更新默认关闭：开发时加 --reload 才启用（避免误重启/多实例）
    if '--reload' in sys.argv:
        threading.Thread(target=watch_source, daemon=True).start()
    
    try:
        # 先 bind 再报成功，端口被占时不会误报「已启动」
        with MyTCPServer(("", port), Handler) as httpd:
            print(f"\n🚀 统一控制台已启动：http://127.0.0.1:{port}")
            print("（如需关闭，双击 StopConsole.bat）\n")
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
