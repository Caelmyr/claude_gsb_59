"""自定义运行（自测）API。

仅供用户自测：不判对错、不计入成绩、不产生正式提交记录，
与正式提交/判题流程互不影响。
"""
from flask import Blueprint, request

from backend import config
from backend.api import ok, err, require_auth
from backend.judge.testrun import custom_run, MAX_INPUT_LEN, MAX_CODE_LEN

testrun_bp = Blueprint("testrun", __name__)


@testrun_bp.post("/run")
@require_auth
def run_custom():
    data = request.get_json(silent=True) or {}
    code = data.get("code") or ""
    language = data.get("language", "python")
    stdin_text = data.get("input") or ""

    if language not in config.LANGUAGES:
        return err("不支持的语言", 400)
    if not code.strip():
        return err("代码不能为空", 400)
    if len(code) > MAX_CODE_LEN:
        return err("代码过长", 400)
    if len(stdin_text) > MAX_INPUT_LEN:
        return err("输入数据过大（上限 256KB）", 400)

    return ok(custom_run(code, language, stdin_text))
