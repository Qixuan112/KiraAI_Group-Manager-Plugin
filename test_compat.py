#!/usr/bin/env python3
"""行为测试：从 main.py 抽取真实函数源码执行，验证 NapCat / SnowLuma 两种协议端兼容性。
（ast 抽取真源码而非复制逻辑 —— 源码改了探针自动跟着走）
背景：SnowLuma 的 get_group_system_msg 返回 data 为数组（NapCat 为 dict+join_requests），
旧代码 data.get("join_requests") 直接抛 'list' object has no attribute 'get'。
"""
import ast
import asyncio
import sys

SRC = open("main.py", encoding="utf-8").read()
tree = ast.parse(SRC)


def extract_func(name):
    """按名字抽取函数源码（不含装饰器）。找不到就退出 —— 探针显式报错，不静默。"""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(SRC, node)
    print(f"!! 探针抽不到函数 {name} —— 源码可能被重构，探针需要跟上")
    sys.exit(2)


class FakeClient:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    async def send_action(self, action, params):
        self.calls.append(action)
        return self.resp


class FakeAdapter:
    def __init__(self, resp):
        self._client = FakeClient(resp)

    def get_client(self):
        return self._client


class FakePlatform:
    platform = "QQ"
    name = "qq1"


class FakeEvent:
    session = type("S", (), {"session_id": "12345"})()
    adapter = FakePlatform()


class Dummy:
    enable_essence = True
    enable_join_request = True

    # 真实函数注入（真源码签名引用 KiraMessageBatchEvent，仅注解 —— def 时求值，需先备好桩）
    ns = {"KiraMessageBatchEvent": object}
    for fname in ("_fetch_pending_requests", "check_join_requests",
                  "list_essence", "_request_key"):
        code = extract_func(fname)
        exec(compile(ast.Module(body=[ast.parse(code).body[0]], type_ignores=[]),
                     "<extracted>", "exec"), ns)
    _fetch_pending_requests = ns["_fetch_pending_requests"]
    check_join_requests = ns["check_join_requests"]
    list_essence = ns["list_essence"]
    _request_key = staticmethod(ns["_request_key"])

    def __init__(self):
        self._seen_flags = {}
        self.saved = 0
        self.logged = []
        # ctx 桩：check_join_requests 走 ctx.adapter_mgr.get_adapter(event.adapter.name)
        adapter = FakeAdapter({"status": "ok", "retcode": 0, "data": [
            {"request_id": 2001, "group_id": 123, "requester_uin": 999,
             "requester_nick": "小红", "message": "拉我", "checked": False,
             "flag": "slreq:1:2001:123:1:0"},
        ]})
        self.ctx = type("Ctx", (), {"adapter_mgr": type(
            "M", (), {"get_adapter": staticmethod(lambda name: adapter)})()})()

    def _save_seen_flags(self):
        self.saved += 1

    def _is_admin(self, event):
        return True

    def _log_operation(self, op, operator, target, detail):
        self.logged.append((op, operator, target, detail))

    def _format_time(self, ts):
        return str(ts)

    async def _call_group_action(self, event, action, params, op_name, target="", need_group=True):
        if action == "get_essence_msg_list":
            return self.essence_data, None, "op"
        return None, f"未知action {action}", "op"


PASS, FAIL = 0, []


def check(name, cond):
    global PASS
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL.append(name)
        print(f"  ✗ {name}")


d = Dummy()

print("== A. get_group_system_msg 兼容性 ==")

# A1: NapCat dict 格式
napcat = {"status": "ok", "retcode": 0, "data": {"invited_requests": [], "InvitedRequest": [],
          "join_requests": [
              {"request_id": 1001, "group_id": 123, "requester_uin": 888,
               "requester_nick": "小明", "message": "求带", "actor": 0},
              {"request_id": 1002, "group_id": 123, "requester_uin": 889,
               "requester_nick": "已处理", "message": "", "actor": 555},
          ]}}
pending, err = asyncio.run(d._fetch_pending_requests(FakeAdapter(napcat)))
check("A1 NapCat dict 格式不报错", err is None and pending is not None)
check("A1 actor=0 的申请保留", len(pending) == 1 and pending[0]["request_id"] == 1001)

# A2: SnowLuma list 格式（本次线上报错的场景）
snow = {"status": "ok", "retcode": 0, "data": [
    {"request_id": 2001, "group_id": 123, "requester_uin": 999, "requester_nick": "小红",
     "message": "拉我", "checked": False, "flag": "slreq:1:2001:123:1:0"},
    {"request_id": 2000, "group_id": 123, "requester_uin": 778, "requester_nick": "已处理",
     "message": "", "checked": True, "flag": "slreq:1:2000:123:1:0"},
]}
pending, err = asyncio.run(d._fetch_pending_requests(FakeAdapter(snow)))
check("A2 SnowLuma list 格式不报错（'list' object has no attribute get 已修）",
      err is None and pending is not None)
check("A2 checked=True 的申请被过滤", len(pending) == 1 and pending[0]["request_id"] == 2001)

# A3: SnowLuma 空数组
pending, err = asyncio.run(d._fetch_pending_requests(FakeAdapter({"status": "ok", "retcode": 0, "data": []})))
check("A3 空数组 → 0 条待处理", err is None and pending == [])

# A4: data 为 null（两端都可能出现）
pending, err = asyncio.run(d._fetch_pending_requests(FakeAdapter({"status": "ok", "retcode": 0, "data": None})))
check("A4 data=null → 0 条且不崩", err is None and pending == [])

# A5: 失败响应
pending, err = asyncio.run(d._fetch_pending_requests(FakeAdapter({"status": "failed", "message": "boom"})))
check("A5 失败响应正常返回 err", err == "boom" and pending is None)

print("== B. check_join_requests 工具（flag 展示）==")
ev = FakeEvent()
out = asyncio.run(d.check_join_requests(ev))
check("B1 SnowLuma 下查询不崩且展示规范 flag", "slreq:1:2001:123:1:0" in out and "2001" in out)
check("B2 展示用 flag 不再只是 request_id", "request_flag=slreq:" in out)

print("== C. list_essence 兼容性 ==")
d2 = Dummy()
# C1: NapCat/SnowLuma 都是段数组 content
d2.essence_data = [{"sender_nick": "甲", "sender_id": 1, "sender_time": 100,
                    "content": [{"type": "text", "data": {"text": "精华正文"}},
                                {"type": "image", "data": {"url": "http://x"}}]}]
out = asyncio.run(d2.list_essence(FakeEvent(), 10))
check("C1 段数组 content 提取纯文本", "精华正文" in out and "http" not in out)
# C2: 防御性 —— 字符串 content（万一旧版/其他端）也能显示
d2.essence_data = [{"sender_nick": "乙", "sender_id": 2, "sender_time": 100, "content": "老式字符串"}]
out = asyncio.run(d2.list_essence(FakeEvent(), 10))
check("C2 字符串 content 仍正常", "老式字符串" in out)
# C3: content 缺失不崩
d2.essence_data = [{"sender_nick": "丙", "sender_id": 3, "sender_time": 100}]
out = asyncio.run(d2.list_essence(FakeEvent(), 10))
check("C3 content 缺失不崩", "丙" in out)

print(f"\n{'='*46}\n通过 {PASS} 条" + (f"，失败 {len(FAIL)} 条: {FAIL}" if FAIL else "，全部绿 ✓"))
sys.exit(1 if FAIL else 0)
