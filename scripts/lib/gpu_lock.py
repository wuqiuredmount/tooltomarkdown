# -*- coding: utf-8 -*-
"""iGPU 跨进程互斥锁。

背景：Intel Iris Xe 是**一块**核显，RapidOCR 走 OpenVINO GPU、GLM 走 Vulkan，
两个进程同时推理会触发原生崩溃（无 Python 堆栈，整批任务丢失）。

两层保护：
  1. **显式锁** `library/.locks/gpu.lock`（O_EXCL 创建，含 pid/tool/时间戳）；
  2. **外部活动探测**：即使对方没拿锁（例如正在跑的全量批处理是旧代码，不会加锁），
     通过「缓存目录 mtime 是否新鲜」判断 GPU 是否被占用，避免抢跑。
"""
import os
import json
import time
import ctypes
from pathlib import Path
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'library'
LOCK_DIR = LIB / '.locks'
LOCK_FILE = LOCK_DIR / 'gpu.lock'

TTL = 1800               # 锁超过 30 分钟视为陈旧
ACTIVE_WINDOW = 90       # 缓存目录 90s 内有更新 → 视为有活跃推理（覆盖换书时的引擎重编译间隙）
STILL_ACTIVE = 259

# 会产生 GPU 负载的工具
GPU_TOOLS = {'rapidocr', 'glm'}

ACTIVITY_DIRS = [
    LIB / 'ocr_text',                  # RapidOCR 逐页写缓存
    ROOT / 'tmp' / '_vlm_out',     # Paddle
    ROOT / 'tmp' / '_got_out',     # GOT（CPU，但保留口径一致）
    ROOT / 'tmp' / '_glm_out',     # GLM
]


def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == 'nt':
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_lock():
    try:
        return json.loads(LOCK_FILE.read_text(encoding='utf-8'))
    except Exception:
        return None


def external_activity(window: int = ACTIVE_WINDOW):
    """探测未被锁覆盖的 GPU 活动（兼容旧脚本）。

    返回 (busy: bool, reason: str)。只看目录 mtime，开销在毫秒级。
    """
    now = time.time()
    newest, where = 0.0, ''
    for d in ACTIVITY_DIRS:
        if not d.exists():
            continue
        try:
            ts = d.stat().st_mtime
        except OSError:
            continue
        # 目录本身 mtime 可能因删除不变，再看一层子目录
        try:
            for sub in d.iterdir():
                if sub.is_dir():
                    ts = max(ts, sub.stat().st_mtime)
        except OSError:
            pass
        if ts > newest:
            newest, where = ts, d.name
    if newest and now - newest < window:
        return True, f'{where} 在 {int(now - newest)}s 内有更新'
    return False, ''


def status():
    """给看板/诊断用：返回当前锁与外部活动状态。"""
    info = read_lock()
    busy_ext, why = external_activity()
    if info and _pid_alive(info.get('pid', 0)):
        return {'locked': True, 'owner': info, 'external': busy_ext, 'reason': why}
    return {'locked': False, 'owner': None, 'external': busy_ext, 'reason': why}


@contextmanager
def gpu_lock(tool: str, wait: bool = True, timeout: int = 1800, poll: float = 3.0):
    """获取 GPU 独占权。

    wait=True 时轮询等待；等待期间若检测到外部活动（旧脚本在跑），也会继续等。
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + timeout
    acquired = False

    while True:
        # 1) 尝试抢锁文件
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({'pid': os.getpid(), 'tool': tool,
                                     'ts': time.time(),
                                     'cmd': ' '.join(os.sys.argv)[:300]}).encode('utf-8'))
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            info = read_lock() or {}
            holder_alive = _pid_alive(int(info.get('pid', 0)))
            stale = (not holder_alive) or (time.time() - float(info.get('ts', 0)) > TTL)
            if stale:
                try:
                    LOCK_FILE.replace(LOCK_FILE.with_suffix('.stale'))
                except OSError:
                    pass
                continue
            if not wait:
                raise TimeoutError(f'GPU 被占用：pid={info.get("pid")} tool={info.get("tool")}')
        except OSError as e:
            raise RuntimeError(f'无法创建锁文件 {LOCK_FILE}: {e}')

        # 2) 有活锁 → 等
        if time.time() > deadline:
            info = read_lock() or {}
            raise TimeoutError(f'等待 GPU 超时（{timeout}s），当前占用者 '
                               f'pid={info.get("pid")} tool={info.get("tool")}')
        time.sleep(poll)

    try:
        # 3) 抢到锁后，再确认没有旧脚本在裸跑（它不会加锁）
        busy, why = external_activity()
        if busy:
            if not wait:
                # 立即拒绝：释放刚拿到的锁，避免占着锁却不用
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    pass
                acquired = False
                raise TimeoutError(f'GPU 正被外部进程占用：{why}')
            while busy and time.time() < deadline:
                time.sleep(poll)
                busy, why = external_activity()
            if busy:                      # 等满超时仍被占用 → 必须失败，不能放行
                try:
                    LOCK_FILE.unlink()
                except OSError:
                    pass
                acquired = False
                raise TimeoutError(f'等待 GPU 超时（{timeout}s）：{why}')
        yield
    finally:
        if acquired:
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass


def release_stale():
    """清理陈旧锁（pid 不存在或超时）。"""
    info = read_lock()
    if not info:
        return False
    if _pid_alive(int(info.get('pid', 0))) and time.time() - float(info.get('ts', 0)) <= TTL:
        return False
    try:
        LOCK_FILE.replace(LOCK_FILE.with_suffix('.stale'))
        return True
    except OSError:
        return False


if __name__ == '__main__':
    import pprint
    pprint.pprint(status())
