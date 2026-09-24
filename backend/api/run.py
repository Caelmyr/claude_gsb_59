"""自定义运行（自测）API。

POST /api/run  使用用户提供的一组标准输入在沙箱中编译并运行一次代码，
仅用于编辑器内自测：不判对错、不计分、不写提交记录、不触发排行榜与
防作弊。正式提交仍走 /api/submissions，两套流程完全独立。
"""
import os

from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth
from backend.runner import runner, MAX_INPUT_CHARS, MAX_CODE_CHARS
from backend.storage import read_json
from backend.utils import sanitize_id

run_bp = Blueprint("run", __name__)


@run_bp.post("/run")
@require_auth
def run_code():
    data = request.get_json(silent=True) or {}
    code = data.get("code") or ""
    language = data.get("language", "python")
    stdin_text = data.get("stdin")
    problem_id = data.get("problem_id") or None

    if language not in config.LANGUAGES:
        return err("不支持的语言", 400)
    if not code.strip():
        return err("代码不能为空", 400)
    if len(code) > MAX_CODE_CHARS:
        return err("代码过长（上限 100000 字符）", 400)
    if stdin_text is None:
        stdin_text = ""
    if not isinstance(stdin_text, str):
        return err("输入内容格式不正确", 400)
    if len(stdin_text) > MAX_INPUT_CHARS:
        return err(f"自定义输入过长（上限 {MAX_INPUT_CHARS // 1024}KB）", 400)

    # 题目可选：选中时沿用题目时限/内存；传了但不存在则直接提示，避免静默
    if problem_id:
        problem_id = sanitize_id(problem_id)
        problem = read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))
        if not problem:
            return err("所选题目不存在，无法按题目时限运行（可取消选择题目后重试）", 404)

    result = runner.run_once(code, language, stdin_text, problem_id)
    result["language"] = language
    result["language_name"] = config.LANGUAGES[language]["name"]
    return ok(result)
