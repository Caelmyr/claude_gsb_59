#!/usr/bin/env python3
"""自定义运行（自测）功能的端到端测试 + 正式判题流程回归测试。"""
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 启动前清理上一轮测试可能残留的运行数据（引擎启动时会重建索引）
for _d in ("data/submissions", "data/scores"):
    if os.path.exists(_d):
        shutil.rmtree(_d)

from backend.app import app

client = app.test_client()
FAILURES = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  | {extra}" if extra and not cond else ""))
    if not cond:
        FAILURES.append(name)


def api(path, method="GET", body=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    resp = getattr(client, method.lower())("/api" + path, data=json.dumps(body or {}), headers=headers)
    return resp.status_code, resp.get_json()


# ---- 登录 ----
st, r = api("/auth/login", "POST", {"username": "alice", "password": "123456"})
token = r["data"]["token"]
print("== 登录 alice 成功 ==")

# ---- 1. Python 正常运行 ----
st, r = api("/run", "POST", {
    "language": "python",
    "code": "a, b = map(int, input().split())\nprint(a + b)",
    "input": "3 4\n",
}, token)
d = r["data"]
check("python 正常运行 status=OK", r["code"] == 0 and d["status"] == "OK", json.dumps(d, ensure_ascii=False)[:300])
check("python 输出正确", d["stdout"].strip() == "7", repr(d["stdout"]))
check("python 有耗时统计", d["time_ms"] >= 0 and d["message"] == "运行完成")

# ---- 2. C 正常运行（带输入） ----
st, r = api("/run", "POST", {
    "language": "c",
    "code": '#include <stdio.h>\nint main(){long a,b;scanf("%ld %ld",&a,&b);printf("%ld\\n",a+b);return 0;}',
    "input": "10 20",
}, token)
d = r["data"]
check("c 正常运行 status=OK", d["status"] == "OK", json.dumps(d, ensure_ascii=False)[:300])
check("c 输出正确", d["stdout"].strip() == "30", repr(d["stdout"]))

# ---- 3. C++ 正常运行 ----
st, r = api("/run", "POST", {
    "language": "cpp",
    "code": '#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b<<std::endl;return 0;}',
    "input": "100 200",
}, token)
d = r["data"]
check("cpp 正常运行 status=OK", d["status"] == "OK", json.dumps(d, ensure_ascii=False)[:300])
check("cpp 输出正确", d["stdout"].strip() == "300", repr(d["stdout"]))

# ---- 4. 编译失败（C++ 语法错误） ----
st, r = api("/run", "POST", {"language": "cpp", "code": "int main( { 这不是合法代码", "input": ""}, token)
d = r["data"]
check("编译失败 status=CE", d["status"] == "CE", json.dumps(d, ensure_ascii=False)[:200])
check("编译失败有人类可读提示", "编译" in d["message"])
check("编译信息非空", bool(d["compile_message"].strip()))

# ---- 5. 运行超时（死循环） ----
t0 = time.time()
st, r = api("/run", "POST", {"language": "python", "code": "while True: pass", "input": ""}, token)
d = r["data"]
check("死循环 status=TLE", d["status"] == "TLE", json.dumps(d, ensure_ascii=False)[:200])
check("TLE 有可读提示", "超时" in d["message"])
print(f"     (TLE 判定耗时 {time.time()-t0:.1f}s)")

# ---- 6. 运行时错误（Python 异常） ----
st, r = api("/run", "POST", {"language": "python", "code": "print(1/0)", "input": ""}, token)
d = r["data"]
check("Python 异常 status=RE", d["status"] == "RE", json.dumps(d, ensure_ascii=False)[:200])
check("RE 有可读提示", "运行时错误" in d["message"])
check("RE 展示 stderr", "ZeroDivisionError" in d["stderr"])

# ---- 7. 运行时崩溃（C 段错误） ----
st, r = api("/run", "POST", {"language": "c", "code": "int main(){int*p=0;*p=42;return 0;}", "input": ""}, token)
d = r["data"]
check("段错误 status=RE", d["status"] == "RE", json.dumps(d, ensure_ascii=False)[:200])
check("段错误有可读提示", bool(d["message"]))

# ---- 8. 超长输出截断 ----
st, r = api("/run", "POST", {"language": "python", "code": "print('x' * 1000000)", "input": ""}, token)
d = r["data"]
check("长输出 status=OK", d["status"] == "OK", d["status"])
check("长输出被截断", d["stdout_truncated"] is True and len(d["stdout"]) <= 20000,
      f"len={len(d['stdout'])} truncated={d['stdout_truncated']}")

# ---- 9. 输出超限（OLE，>4MB） ----
st, r = api("/run", "POST", {"language": "python", "code": "print('y' * 5 * 1024 * 1024)", "input": ""}, token)
d = r["data"]
check("巨量输出 status=OLE", d["status"] == "OLE", json.dumps(d, ensure_ascii=False)[:200])
check("OLE 有可读提示", "输出" in d["message"])

# ---- 10. 无输出程序（不应空白） ----
st, r = api("/run", "POST", {"language": "python", "code": "x = 1", "input": ""}, token)
d = r["data"]
check("无输出程序 status=OK 且 stdout 为空", d["status"] == "OK" and d["stdout"] == "")

# ---- 11. Java（环境无 javac，应给出可读的编译失败提示而非空白） ----
st, r = api("/run", "POST", {"language": "java", "code": "public class Main { public static void main(String[] a){} }", "input": ""}, token)
d = r["data"]
check("java 无编译器时 status=CE 且提示可读", d["status"] == "CE" and bool(d["compile_message"] or d["message"]),
      json.dumps(d, ensure_ascii=False)[:200])

# ---- 12. 参数校验 ----
st, r = api("/run", "POST", {"language": "python", "code": "   ", "input": ""}, token)
check("空代码被拒绝", r["code"] != 0 and "代码" in r.get("message", ""))
st, r = api("/run", "POST", {"language": "rust", "code": "fn main(){}", "input": ""}, token)
check("不支持的语言被拒绝", r["code"] != 0)
st, r = api("/run", "POST", {"language": "python", "code": "print(1)", "input": "x" * (300 * 1024)}, token)
check("超大输入被拒绝", r["code"] != 0)
st, r = api("/run", "POST", {"language": "python", "code": "print(1)", "input": ""})
check("未登录被拒绝 401", st == 401)

# ---- 13. 自测不产生提交记录 ----
st, r = api("/submissions?mine=1", "GET", token=token)
total_before = r["data"]["total"]
st, r = api("/run", "POST", {"language": "python", "code": "print('selftest')", "input": ""}, token)
check("自测运行成功", r["data"]["status"] == "OK")
st, r = api("/submissions?mine=1", "GET", token=token)
check("自测不产生提交记录", r["data"]["total"] == total_before,
      f"before={total_before} after={r['data']['total']}")
check("自测不创建提交分片目录", not os.path.exists("data/submissions") or
      not any(True for _ in os.scandir("data/submissions")))

# ---- 14. 正式提交判题流程不受影响 ----
st, r = api("/submissions", "POST", {
    "language": "python",
    "code": "a, b = map(int, input().split())\nprint(a + b)",
    "problem_id": "p1001",
}, token)
check("正式提交创建成功", r["code"] == 0, json.dumps(r, ensure_ascii=False)[:200])
sub_id = r["data"]["id"]
final = None
for _ in range(30):
    st, r = api("/submissions/" + sub_id, "GET", token=token)
    if r["data"]["status"] not in ("PENDING", "JUDGING"):
        final = r["data"]
        break
    time.sleep(1)
check("正式提交判题完成", final is not None)
if final:
    check("正式提交判为 AC", final["status"] == "AC", final["status"])
    check("正式提交得分 100", final["score"] == 100, str(final["score"]))

# ---- 15. 自测与判题并发时互不影响（自测期间正式提交仍能判） ----
st, r = api("/submissions", "POST", {
    "language": "python", "code": "a,b=map(int,input().split())\nprint(a+b)", "problem_id": "p1001",
}, token)
sub2 = r["data"]["id"]
st, r = api("/run", "POST", {"language": "python", "code": "print(1+1)", "input": ""}, token)
check("判题进行中自测仍可用", r["data"]["status"] == "OK" and r["data"]["stdout"].strip() == "2")
final2 = None
for _ in range(30):
    st, r = api("/submissions/" + sub2, "GET", token=token)
    if "data" not in r:
        print("     轮询异常响应:", st, r)
        break
    if r["data"]["status"] not in ("PENDING", "JUDGING"):
        final2 = r["data"]
        break
    time.sleep(1)
check("并发后正式提交仍正常判题", final2 and final2["status"] == "AC",
      final2 and final2["status"])

# ---- 清理测试产生的运行数据 ----
for d in ("data/submissions", "data/scores"):
    if os.path.exists(d):
        shutil.rmtree(d)
for f in os.listdir("data/runs") if os.path.exists("data/runs") else []:
    p = os.path.join("data/runs", f)
    shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)

print()
if FAILURES:
    print(f"❌ {len(FAILURES)} 项失败: {FAILURES}")
    sys.exit(1)
print("✅ 全部测试通过")
