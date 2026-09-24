"""自定义运行（自测）：用户在编辑器中自带输入试运行代码。

与正式判题流程完全隔离：
  - 不创建提交记录、不写分片、不更新排行榜、不做防作弊检测；
  - 沙箱接口与判题一致（compile/run，四种语言通用），Docker 模式
    直接复用引擎实例，原生模式使用放宽进程数限制的专用实例；
  - 使用独立的临时运行目录与独立的并发信号量，不占用判题并发额度。

所有异常路径（编译失败、超时、崩溃、系统错误等）都会兜底为
带人类可读 message 的结果字典，保证前端永远有内容可展示。
"""
import os
import shutil
import threading

from backend import config
from backend.judge import engine
from backend.sandbox import ST_OK, ST_CE, NativeSandbox
from backend.utils import gen_id, truncate

# 自测运行限制（与具体题目无关，取足够宽松的固定值，保证各语言都能跑）
TIME_LIMIT_MS = 5000          # 运行时限
MEMORY_LIMIT_KB = 256 * 1024  # 内存上限
MAX_INPUT_LEN = 256 * 1024    # 自定义输入最大长度（字符）
MAX_CODE_LEN = 100000         # 代码长度上限（与正式提交一致）
DISPLAY_LIMIT = 20000         # 返回给前端展示的输出上限（字符）

# 自测并发槽：独立于评测引擎的信号量，避免自测请求挤占判题资源
_slots = threading.Semaphore(2)


class _SelfTestNativeSandbox(NativeSandbox):
    """自测专用的原生沙箱：不施加 RLIMIT_NPROC 进程数限制。

    评测机常以共享 UID 的容器部署，而 RLIMIT_NPROC 按 UID 统计（包含
    宿主机上同 UID 的进程），过小的进程数上限会让 gcc/g++ 等编译器
    完全无法 fork。自测运行有独立的超时看门狗与 CPU/内存限制兜底，
    因此去掉该限制；其余资源限制与正式沙箱保持一致。
    仅用于自测，正式判题仍使用引擎原有的沙箱实例。
    """

    name = "native-selftest"

    def _setup_limits(self):
        import resource
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (256 * 1024 * 1024, 256 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        except (ValueError, OSError):
            pass
        try:
            # 降权到 nobody（与正式沙箱一致）
            if os.getuid() == 0:
                import pwd
                try:
                    nobody = pwd.getpwnam("nobody")
                    os.setgid(nobody.pw_gid)
                    os.setuid(nobody.pw_uid)
                except (KeyError, OSError):
                    pass
        except Exception:
            pass


_selftest_native = _SelfTestNativeSandbox()


def _sandbox():
    """返回自测用沙箱：Docker 模式直接复用引擎实例；
    原生模式换用放宽进程数限制的自测专用实例。"""
    sandbox = engine.sandbox
    if isinstance(sandbox, NativeSandbox):
        return _selftest_native
    return sandbox

_STATUS_MESSAGE = {
    "OK": "运行完成",
    "CE": "编译失败，请根据下方编译信息修改代码",
    "TLE": f"运行超时：程序在 {TIME_LIMIT_MS}ms 内未能结束",
    "MLE": "超出内存限制",
    "OLE": "输出内容超过大小限制",
    "RE": "运行时错误：程序崩溃或异常退出",
    "SE": "系统内部错误，请稍后重试",
}


def _cut(text):
    """截断用于展示的文本，返回 (文本, 是否被截断)。"""
    text = text or ""
    if len(text) > DISPLAY_LIMIT:
        return text[:DISPLAY_LIMIT], True
    return text, False


def _result(status, **kw):
    base = {
        "status": status,
        "message": _STATUS_MESSAGE.get(status, "未知状态"),
        "stdout": "",
        "stderr": "",
        "stdout_truncated": False,
        "stderr_truncated": False,
        "compile_message": "",
        "time_ms": 0,
        "memory_kb": 0,
        "time_limit_ms": TIME_LIMIT_MS,
        "memory_limit_kb": MEMORY_LIMIT_KB,
    }
    base.update(kw)
    return base


def custom_run(code, language, stdin_text):
    """同步编译并运行用户代码，返回结果字典（绝不抛异常）。"""
    if not _slots.acquire(timeout=20):
        return _result("SE", message="当前自测请求较多，请稍后重试")
    workdir = os.path.join(config.RUNS_DIR, gen_id("tr"))
    try:
        os.makedirs(workdir, exist_ok=True)
        sandbox = _sandbox()

        # 1) 编译（解释型语言仅落盘源码）
        compile_timeout = config.DEFAULT_SETTINGS["judge"]["compile_timeout_ms"]
        try:
            comp = sandbox.compile(code, language, workdir, compile_timeout)
        except Exception as e:
            return _result("SE", message=f"编译阶段出现异常: {e}")
        if comp["status"] == ST_CE:
            return _result("CE", compile_message=truncate(comp.get("message", ""), 4000))
        if comp["status"] != ST_OK:
            return _result("SE", message=f"编译阶段异常: {comp.get('message', '')}")

        # 2) 运行（自带输入，固定宽松限制）
        try:
            res = sandbox.run(
                language, workdir,
                (stdin_text or "").encode("utf-8", "replace"),
                TIME_LIMIT_MS, MEMORY_LIMIT_KB,
            )
        except Exception as e:
            return _result("SE", message=f"运行阶段出现异常: {e}")

        stdout, out_cut = _cut(res.get("stdout", ""))
        stderr, err_cut = _cut(res.get("stderr", ""))
        status = res.get("status", "SE")
        detail = (res.get("message") or "").strip()
        # 原生沙箱对非零退出码仍回报 OK（Docker 后端会报 RE），
        # 在自测层统一为 RE，保证崩溃/异常退出有明确提示
        if status == "OK" and res.get("exit_code", 0) != 0:
            status = "RE"
            detail = f"程序以非零退出码 {res.get('exit_code')} 结束"
        if status not in _STATUS_MESSAGE:
            status = "SE"
        message = _STATUS_MESSAGE[status]
        if detail and status != "OK":
            message = f"{message}（{detail}）"
        return _result(
            status,
            message=message,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=out_cut,
            stderr_truncated=err_cut,
            time_ms=res.get("time_ms", 0),
            memory_kb=res.get("memory_kb", 0),
        )
    except Exception as e:  # 兜底：任何意外都给出可读提示，绝不空白
        return _result("SE", message=f"自测运行失败: {e}")
    finally:
        _slots.release()
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass  # 清理失败不影响响应，临时目录由系统兜底清理
