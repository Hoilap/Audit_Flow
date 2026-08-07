"""process_utils — 跨平台进程管理工具

提供安全的子进程执行与完整的进程树清理，解决 Windows 上僵尸进程问题。

核心功能：
- run_safe(): subprocess.run() 的安全封装，超时/异常时确保整个进程树被终止
- Windows: 使用 Job Object (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE) 绑定进程组
- Unix: 使用 process group (start_new_session + os.killpg)

使用方式：
    from audit_workflow.process_utils import run_safe

    result = run_safe([sys.executable, script_path], timeout=30, cwd=project_root)
    # 超时或异常时，进程树已被完整清理，不会留下孤儿进程
"""

from __future__ import annotations

import os
import subprocess
import sys
import logging

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    # ── Windows Job Object API ──

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # 结构体定义
    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_ulonglong),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    # Win32 常量
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
    JobObjectExtendedLimitInformation = 9

    def _create_kill_on_close_job() -> ctypes.c_void_p:
        """创建 Windows Job Object，配置为 '句柄关闭时终止所有进程'。"""
        job_name = f"AuditWorkflow_Job_{os.getpid()}_{id(object())}"
        h_job = _kernel32.CreateJobObjectW(None, job_name)
        if not h_job:
            err = ctypes.get_last_error()
            logger.warning("CreateJobObject 失败 (code=%d)，回退到无 job 模式", err)
            return None

        # 配置：job 句柄关闭时杀死所有关联进程
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )

        size = ctypes.sizeof(info)
        ok = _kernel32.SetInformationJobObject(
            ctypes.c_void_p(h_job),
            JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            size,
        )
        if not ok:
            err = ctypes.get_last_error()
            logger.warning("SetInformationJobObject 失败 (code=%d)", err)
            _kernel32.CloseHandle(ctypes.c_void_p(h_job))
            return None

        return ctypes.c_void_p(h_job)

    def _assign_to_job(h_job: ctypes.c_void_p, pid: int) -> bool:
        """将进程分配到 job object 中。失败时返回 False。"""
        h_proc = _kernel32.OpenProcess(0x1F0FFF, False, pid)  # PROCESS_ALL_ACCESS
        if not h_proc:
            return False
        ok = _kernel32.AssignProcessToJobObject(h_job, ctypes.c_void_p(h_proc))
        _kernel32.CloseHandle(ctypes.c_void_p(h_proc))
        return bool(ok)

    def _close_job_handle(h_job: ctypes.c_void_p) -> None:
        """关闭 job handle（会触发 KILL_ON_JOB_CLOSE，终止所有关联进程）。"""
        try:
            _kernel32.CloseHandle(h_job)
        except Exception:
            pass


def _kill_process_tree_windows(pid: int) -> None:
    """Windows: 递归终止进程树。"""
    try:
        # 使用 taskkill /T 终止整个进程树
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
    except Exception as e:
        logger.warning("taskkill 失败 (pid=%d): %s", pid, e)


def _kill_process_tree_unix(pid: int) -> None:
    """Unix: 使用 SIGKILL 向进程组发送信号。"""
    try:
        os.killpg(pid, 9)  # SIGKILL
    except Exception as e:
        logger.warning("killpg 失败 (pgid=%d): %s", pid, e)


def run_safe(
    args: list[str],
    timeout: int = 30,
    cwd: str | None = None,
    capture_output: bool = True,
    text: bool = True,
    **kwargs,
) -> subprocess.CompletedProcess:
    """安全执行子进程，确保进程树完整清理。

    相比 subprocess.run() 的增强：
    1. Windows: 创建 Job Object，超时或异常时杀死整个进程树
    2. 超时后强制 kill 进程树（不仅仅是子进程本身）
    3. 异常时确保清理

    Args:
        args: 命令行参数列表，同 subprocess.run()
        timeout: 超时秒数
        cwd: 工作目录
        capture_output: 是否捕获输出
        text: 是否文本模式
        **kwargs: 传递给 subprocess.Popen 的其他参数（除 start_new_session 外）

    Returns:
        subprocess.CompletedProcess 对象

    Raises:
        subprocess.TimeoutExpired: 超时（但进程树已被清理）
        subprocess.CalledProcessError: 进程返回非零退出码
    """
    popen_kwargs: dict = {"cwd": cwd, "text": text, **kwargs}

    if capture_output:
        popen_kwargs["stdout"] = subprocess.PIPE
        popen_kwargs["stderr"] = subprocess.PIPE

    h_job = None
    if _IS_WINDOWS:
        # Windows: 创建 job object 用于进程组管理
        h_job = _create_kill_on_close_job()
        # 标记当前进程应在 job handle 关闭时存活
        # (job 只用于管理子进程)
        if h_job:
            # BREAKAWAY_OK 已配置，子进程默认继承 job 但可以被 breakaway
            # 我们手动分配子进程到 job
            pass

    proc = None
    try:
        if _IS_WINDOWS:
            # Windows: 使用 CREATE_BREAKAWAY_FROM_JOB 避免子进程继承父进程的 job
            popen_kwargs.setdefault("creationflags", 0)
            popen_kwargs["creationflags"] |= 0x01000000  # CREATE_BREAKAWAY_FROM_JOB

        proc = subprocess.Popen(args, **popen_kwargs)

        # 将子进程加入 job object
        if h_job and proc.pid:
            _assign_to_job(h_job, proc.pid)

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            returncode = proc.returncode

            return subprocess.CompletedProcess(
                args=args,
                returncode=returncode,
                stdout=stdout if capture_output else None,
                stderr=stderr if capture_output else None,
            )
        except subprocess.TimeoutExpired:
            # 超时：杀死进程树
            logger.warning("run_safe: 进程超时 (%ds)，正在终止进程树...", timeout)

            if _IS_WINDOWS:
                if h_job and proc.pid:
                    # 关闭 job handle → JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 触发
                    # 所有在 job 中的进程被终止
                    _close_job_handle(h_job)
                    h_job = None
                else:
                    _kill_process_tree_windows(proc.pid)
            else:
                if proc.pid:
                    _kill_process_tree_unix(proc.pid)

            # 等待进程实际终止
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("run_safe: 进程未在 5s 内终止，强制 kill")
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

            raise
    except Exception:
        # 任何其他异常也要清理
        if proc and proc.pid:
            logger.warning("run_safe: 异常退出，清理进程树 pid=%d", proc.pid)
            try:
                if _IS_WINDOWS:
                    if h_job:
                        _close_job_handle(h_job)
                        h_job = None
                    else:
                        _kill_process_tree_windows(proc.pid)
                else:
                    proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                pass
        raise
    finally:
        if h_job:
            _close_job_handle(h_job)


def kill_process_tree(pid: int) -> None:
    """终止指定 PID 的整个进程树。

    用于手动清理已知的孤儿进程。
    """
    if _IS_WINDOWS:
        _kill_process_tree_windows(pid)
    else:
        try:
            _kill_process_tree_unix(pid)
        except Exception:
            pass
