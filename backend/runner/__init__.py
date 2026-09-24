"""自测运行器（Run Code / 自定义输入试运行）。

与正式评测的关系：
  - 复用同一套沙箱后端（engine.sandbox），因此四种语言的编译/运行
    行为、资源限制与判题完全一致；
  - 同步执行一次用户自定义输入，**不写提交分片、不进评测队列、
    不更新排行榜、不做防作弊检测**，因此不会产生任何正式提交记录，
    也不影响正式提交与判题流程；
  - 使用独立的临时目录与独立的并发信号量，自测再多也不会拖垮判题。

对外只暴露 run_once()，返回标准化字典：
  status       OK / TLE / MLE / OLE / RE / CE / SE / BUSY
  message      人类可读的中文提示（任何情况都非空兜底）
  stdout/stderr 截断后的程序输出
  truncated    输出是否被截断（stdout/stderr/输出超限）
  time_ms/memory_kb 实测耗时与内存
"""
import os
import shutil
import threading

from backend import config
from backend.sandbox import get_sandbox, ST_OK, ST_TLE, ST_MLE, ST_OLE, ST_RE, ST_CE, ST_SE
from backend.storage import read_json
from backend.utils import gen_id, truncate

# ---- 自测专用限制（防止滥用，与正式判题相互独立）----
MAX_INPUT_CHARS = 64 * 1024        # 自定义输入最多 64KB 文本
MAX_CODE_CHARS = 100_000           # 与正式提交一致
MAX_CONCURRENT_RUNS = 4            # 自测最大并发（独立于判题信号量）
MAX_TIME_MS = 10_000               # 自测运行时限上限 10s
MAX_MEMORY_KB = 512 * 1024         # 自测内存上限 512MB
DEFAULT_TIME_MS = 2_000            # 未选题 / 题目缺省时的时限
DEFAULT_MEMORY_KB = 128 * 1024     # 未选题 / 题目缺省时的内存
# 回传给前端展示的输出上限（字符，按码点截断；沙箱层另有 4MB 字节硬上限）
DISPLAY_LIMIT_CHARS = 16 * 1024

# 状态 -> （是否为错误类，人类可读提示模板由 message 字段补充）
_STATUS_TEXT = {
    ST_OK: "运行完成",
    ST_TLE: "运行超时：程序没有在时间限制内结束，已被强制终止",
    ST_MLE: "内存超限：程序使用的内存超过限制，已被强制终止",
    ST_OLE: "输出超限：程序产生的输出过多（超过输出大小上限），仅展示前面的部分",
    ST_RE: "运行错误：程序异常退出（崩溃 / 非零退出码 / 段错误等）",
    ST_CE: "编译失败：请根据编译器输出检查语法错误",
    ST_SE: "运行环境错误：沙箱执行失败，请稍后重试或联系管理员",
}


class CodeRunner:
    """自测运行器单例（与 JudgeEngine 共享沙箱后端）。"""

    def __init__(self, sandbox=None):
        # 优先复用评测引擎已初始化的沙箱，保证自测与判题环境一致；
        # 在引擎未启动的独立场景下退化为自建沙箱。
        self.sandbox = sandbox
        self._sema = threading.Semaphore(MAX_CONCURRENT_RUNS)

    def _get_sandbox(self):
        if self.sandbox is None:
            try:
                from backend.judge import engine
                self.sandbox = engine.sandbox
            except Exception:
                self.sandbox = get_sandbox()
        return self.sandbox

    # ---- 资源限制：选中题目时沿用题目的时限/内存（自测更上限封顶）----
    @staticmethod
    def _limits_for(problem_id):
        time_ms = DEFAULT_TIME_MS
        memory_kb = DEFAULT_MEMORY_KB
        if problem_id:
            problem = read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))
            if problem:
                try:
                    time_ms = int(problem.get("time_limit_ms") or DEFAULT_TIME_MS)
                except (TypeError, ValueError):
                    time_ms = DEFAULT_TIME_MS
                try:
                    memory_kb = int(problem.get("memory_limit_kb") or DEFAULT_MEMORY_KB)
                except (TypeError, ValueError):
                    memory_kb = DEFAULT_MEMORY_KB
        return min(max(time_ms, 100), MAX_TIME_MS), min(max(memory_kb, 1024), MAX_MEMORY_KB)

    def run_once(self, code, language, stdin_text, problem_id=None):
        """编译并以 stdin_text 作为标准输入运行一次。同步返回结果字典。

        全流程不抛异常给上层：任何失败都转换为带中文提示的标准化结果，
        保证前端永远不会拿到一片空白。
        """
        sandbox = self._get_sandbox()
        time_limit_ms, memory_limit_kb = self._limits_for(problem_id)
        workdir = os.path.join(config.RUNS_DIR, "run_" + gen_id("r"))
        acquired = self._sema.acquire(timeout=20)
        if not acquired:
            return self._result("BUSY", "当前自测的人较多，运行资源繁忙，请稍后再试")
        try:
            os.makedirs(workdir, exist_ok=True)
            compile_timeout = config.DEFAULT_SETTINGS["judge"]["compile_timeout_ms"]
            compile_result = sandbox.compile(code, language, workdir, compile_timeout)
            if compile_result["status"] == ST_CE:
                return self._result(
                    ST_CE,
                    "编译失败：请根据编译器输出检查语法错误",
                    stderr=compile_result.get("message", ""),
                    time_limit_ms=time_limit_ms, memory_limit_kb=memory_limit_kb,
                )
            if compile_result["status"] != ST_OK:
                return self._result(
                    ST_SE,
                    "编译阶段发生环境错误：" + (compile_result.get("message") or "未知错误"),
                    time_limit_ms=time_limit_ms, memory_limit_kb=memory_limit_kb,
                )

            res = sandbox.run(
                language, workdir,
                (stdin_text or "").encode("utf-8"),
                time_limit_ms, memory_limit_kb,
            )
            return self._from_run(res, time_limit_ms, memory_limit_kb)
        except Exception as e:  # 兜底：任何意外都要给出人能看懂的提示
            return self._result(ST_SE, f"运行服务异常：{e}",
                                time_limit_ms=time_limit_ms, memory_limit_kb=memory_limit_kb)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
            self._sema.release()

    def _from_run(self, res, time_limit_ms, memory_limit_kb):
        """把沙箱原始结果转换为自测响应（截断输出、补全中文提示）。"""
        status = res.get("status", ST_SE)
        stdout = res.get("stdout", "") or ""
        stderr = res.get("stderr", "") or ""

        truncated = False
        if len(stdout) > DISPLAY_LIMIT_CHARS:
            stdout = truncate(stdout, DISPLAY_LIMIT_CHARS)
            truncated = True
        if len(stderr) > DISPLAY_LIMIT_CHARS:
            stderr = truncate(stderr, DISPLAY_LIMIT_CHARS)
            truncated = True
        # 沙箱层因输出超过硬上限判定 OLE
        if status == ST_OLE:
            truncated = True

        detail = res.get("message", "") or ""
        message = self._human_message(status, detail, stderr, time_limit_ms, memory_limit_kb)
        return {
            "status": status,
            "message": message,
            "detail": truncate(detail, 1000),
            "stdout": stdout,
            "stderr": stderr,
            "truncated": truncated,
            "output_truncated": truncated,
            "time_ms": int(res.get("time_ms", 0) or 0),
            "memory_kb": int(res.get("memory_kb", 0) or 0),
            "exit_code": int(res.get("exit_code", -1) or -1),
            "time_limit_ms": time_limit_ms,
            "memory_limit_kb": memory_limit_kb,
            "output_limit_chars": DISPLAY_LIMIT_CHARS,
        }

    @staticmethod
    def _human_message(status, detail, stderr_text, time_limit_ms, memory_limit_kb):
        if status == ST_TLE:
            base = f"运行超时：程序没有在 {time_limit_ms} ms 内结束，已被强制终止"
        elif status == ST_MLE:
            base = f"内存超限：程序使用内存超过 {round(memory_limit_kb / 1024)} MB，已被强制终止"
        elif status == ST_RE:
            base = "运行错误：程序异常退出"
            if detail:
                base += f"（{detail}）"
            elif stderr_text.strip():
                base += "，可查看下方错误输出了解原因"
            return base
        else:
            base = _STATUS_TEXT.get(status, "运行结束")
        if status == ST_OK:
            return base
        # 错误类状态若沙箱附带了细节（如信号、退出码），附在后面便于排查
        if detail and status in (ST_TLE, ST_MLE, ST_SE):
            return f"{base}（{detail}）"
        return base

    @staticmethod
    def _result(status, message, stderr="", time_limit_ms=DEFAULT_TIME_MS,
                memory_limit_kb=DEFAULT_MEMORY_KB):
        return {
            "status": status,
            "message": message,
            "detail": "",
            "stdout": "",
            "stderr": truncate(stderr, DISPLAY_LIMIT_CHARS),
            "truncated": len(stderr) > DISPLAY_LIMIT_CHARS,
            "output_truncated": len(stderr) > DISPLAY_LIMIT_CHARS,
            "time_ms": 0,
            "memory_kb": 0,
            "exit_code": -1,
            "time_limit_ms": time_limit_ms,
            "memory_limit_kb": memory_limit_kb,
            "output_limit_chars": DISPLAY_LIMIT_CHARS,
        }

# 全局单例（沙箱在首次使用时绑定到 engine.sandbox）
runner = CodeRunner()
