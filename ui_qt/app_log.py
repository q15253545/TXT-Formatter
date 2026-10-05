"""執行記錄：程式卡死、出錯時事後有跡可查。

記錄檔放在 %LOCALAPPDATA%\\TXTFormatter\\logs：
  app.log     一般記錄。啟動環境、每個操作的開始／結束與耗時、未攔截的
              例外（完整 traceback）、Qt 自己的警告。超過 2MB 自動換檔，
              保留 5 份舊檔。
  faults.log  卡死與閃退。主執行緒超過 FREEZE_SECONDS 秒沒有回應，就把
              所有執行緒當下的呼叫堆疊寫進來（仍卡著的話每隔同樣秒數再寫
              一次）；Python 或 Qt 直接閃退時的堆疊也寫在這裡。

卡死偵測用 faulthandler.dump_traceback_later，不是自己開 Python 執行緒
輪詢：它的計時器是 C 層級的執行緒，不需要 GIL。主執行緒卡在正則回溯這類
不釋放 GIL 的 C 函式裡時，Python 寫的監看執行緒根本輪不到執行，C 層級的
計時器仍然照樣觸發。主執行緒每秒重設一次計時器；只要沒卡住，它永遠不會
觸發。

記錄只寫在使用者自己的電腦上，不會傳到任何地方。
"""

import contextlib
import faulthandler
import functools
import inspect
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
import traceback

from PySide6.QtCore import QTimer, QtMsgType, qInstallMessageHandler, qVersion

from core.persistence import APP_DATA_DIR

LOG_DIR = APP_DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "app.log"
FAULT_FILE = LOG_DIR / "faults.log"
FREEZE_SECONDS = 10
# 內部的重算（重畫目錄、套標題格式…）平常不記，超過這個秒數才記，避免
# 記錄檔被灌爆；卡在裡面時靠 faults.log 的堆疊就看得出是哪一段。
SLOW_SECONDS = 0.5

log = logging.getLogger("txtfmt")

_fault_stream = None
_heartbeat_timer = None
_last_beat = 0.0
_error_dialog_open = False
_on_unhandled_error = None


def setup():
    """程式一啟動就呼叫（建立 QApplication 之前也可以）。"""
    global _fault_stream
    if log.handlers:
        return
    log.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _rotate_fault_file()
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        log.addHandler(file_handler)
        _fault_stream = open(FAULT_FILE, "a", encoding="utf-8")
        # 堆疊本身沒有時間，先寫一行標記這次啟動，事後才對得上 app.log。
        _fault_stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 啟動 =====\n")
        _fault_stream.flush()
        faulthandler.enable(file=_fault_stream, all_threads=True)
    except OSError:
        log.addHandler(logging.NullHandler())
    # 從命令列執行時同時印在終端機上。
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        console.setLevel(logging.WARNING)
        log.addHandler(console)

    sys.excepthook = _excepthook
    threading.excepthook = lambda args: _excepthook(args.exc_type, args.exc_value, args.exc_traceback)
    qInstallMessageHandler(_qt_message_handler)
    log.info("===== 啟動 ===== Python %s／Qt %s／%s %s", platform.python_version(), qVersion(),
             platform.system(), platform.version())


def _rotate_fault_file():
    """faults.log 是用附加的方式寫，長期卡死會一直長大；開檔前先輪替一次。"""
    try:
        if FAULT_FILE.exists() and FAULT_FILE.stat().st_size > 2 * 1024 * 1024:
            backup = FAULT_FILE.with_suffix(".log.1")
            backup.unlink(missing_ok=True)
            FAULT_FILE.rename(backup)
    except OSError:
        pass


def start_freeze_watchdog():
    """QApplication 建好之後呼叫：主執行緒每秒回報一次「還活著」。"""
    global _heartbeat_timer, _last_beat
    if _fault_stream is None or _heartbeat_timer is not None:
        return
    _last_beat = time.monotonic()
    _heartbeat_timer = QTimer()
    _heartbeat_timer.setInterval(1000)
    _heartbeat_timer.timeout.connect(_heartbeat)
    _heartbeat_timer.start()
    _arm_fault_timer()


def _arm_fault_timer():
    faulthandler.cancel_dump_traceback_later()
    faulthandler.dump_traceback_later(FREEZE_SECONDS, repeat=True, file=_fault_stream)


def _heartbeat():
    global _last_beat
    now = time.monotonic()
    stalled = now - _last_beat
    _last_beat = now
    if stalled >= FREEZE_SECONDS:
        # faults.log 的堆疊本身沒有時間，這裡在 app.log 補一筆對得起來的時間點。
        _fault_stream.write(f"--- 以上堆疊：{time.strftime('%Y-%m-%d %H:%M:%S')} 恢復回應，"
                            f"停頓約 {stalled:.0f} 秒 ---\n")
        _fault_stream.flush()
        log.warning("介面停止回應約 %.0f 秒，當時的呼叫堆疊記在 %s", stalled, FAULT_FILE)
    _arm_fault_timer()


@contextlib.contextmanager
def native_dialog():
    """開原生檔案對話框的期間：Windows 的原生對話框自己跑事件迴圈、不派送 QTimer，
    心跳會停，使用者慢慢選檔就被當成當機寫進 faults.log。先停掉，關掉後重新計時。"""
    global _last_beat
    active = _heartbeat_timer is not None
    if active:
        faulthandler.cancel_dump_traceback_later()
    try:
        yield
    finally:
        if active:
            _last_beat = time.monotonic()
            _arm_fault_timer()


def set_error_callback(callback):
    """未攔截的例外發生時，除了寫記錄之外要怎麼告訴使用者（由主視窗提供）。"""
    global _on_unhandled_error
    _on_unhandled_error = callback


def _excepthook(exc_type, exc_value, exc_traceback):
    global _error_dialog_open
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    log.error("未攔截的例外：\n%s", text.rstrip())
    if _on_unhandled_error is not None and not _error_dialog_open:
        _error_dialog_open = True
        try:
            _on_unhandled_error(f"{exc_type.__name__}: {exc_value}")
        except Exception:   # 顯示錯誤本身失敗也不能再丟例外，否則會無限遞迴
            log.exception("顯示錯誤訊息失敗")
        finally:
            _error_dialog_open = False


_QT_LEVELS = {
    QtMsgType.QtDebugMsg: logging.DEBUG,
    QtMsgType.QtInfoMsg: logging.INFO,
    QtMsgType.QtWarningMsg: logging.WARNING,
    QtMsgType.QtCriticalMsg: logging.ERROR,
    QtMsgType.QtFatalMsg: logging.CRITICAL,
}


def _qt_message_handler(mode, context, message):
    log.log(_QT_LEVELS.get(mode, logging.WARNING), "Qt：%s", message)


def _is_existing_path(value: str) -> bool:
    """檔案路徑照記（查開檔、存檔問題要用）。不能只看有沒有「/」：「山路/過河」這種短本文、
    搜尋字串也有，會被當成路徑寫進記錄檔。所以要是絕對路徑、而且真的存在。"""
    return (len(value) < 260 and "\n" not in value and os.path.isabs(value)
            and os.path.exists(value))


def _describe_args(args) -> str:
    """只記下參數的「形狀」，不記內容：不把整份小說寫進記錄檔。"""
    parts = []
    for value in args:
        if isinstance(value, str):
            parts.append(value if _is_existing_path(value) else f"str({len(value)})")
        elif isinstance(value, (int, float, bool)) or value is None:
            parts.append(repr(value))
        elif isinstance(value, (list, tuple, set, dict)):
            parts.append(f"{type(value).__name__}({len(value)})")
    return ", ".join(parts)


def _positional_limit(func):
    """func 最多吃幾個位置參數（有 *args 就是不限）。

    包裝函式的簽名是 *args：接到 Qt signal 時，PySide 會把 signal 的參數
    （例如 clicked 的 checked）全部塞進來，原本只收 self 的方法就會多一個
    參數而出錯。所以呼叫原函式前先把多出來的位置參數丟掉。"""
    limit = 0
    for parameter in inspect.signature(func).parameters.values():
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            return None
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD):
            limit += 1
    return limit


def action(func):
    """使用者操作：記下開始、結束與耗時；中途丟出例外也會記下來。
    卡死時最後一筆「開始」沒有對應的「完成」，就知道是卡在哪個操作。"""
    name = func.__name__
    limit = _positional_limit(func)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if limit is not None:
            args = args[:limit]
        detail = _describe_args(args[1:])
        log.info("開始 %s(%s)", name, detail)
        started = time.perf_counter()
        try:
            result = func(*args, **kwargs)
        except Exception as error:
            # 完整 traceback 由 _excepthook 記，這裡只記是哪個操作失敗。
            log.error("失敗 %s（%.2f 秒）：%s", name, time.perf_counter() - started, error)
            raise
        log.info("完成 %s（%.2f 秒）", name, time.perf_counter() - started)
        return result
    return wrapper


def timed(func):
    """內部的重算：超過 SLOW_SECONDS 才記，平常不佔記錄檔。"""
    name = func.__name__
    limit = _positional_limit(func)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if limit is not None:
            args = args[:limit]
        started = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - started
        if elapsed >= SLOW_SECONDS:
            log.info("偏慢 %s：%.2f 秒", name, elapsed)
        return result
    return wrapper
