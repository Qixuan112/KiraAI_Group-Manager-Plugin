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


def extract_assignment(name):
    """按名字抽取顶层/类层赋值语句源码（如 _SEGMENT_PLACEHOLDERS 字典）。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.get_source_segment(SRC, node)
    print(f"!! 探针抽不到赋值 {name} —— 源码可能被重构，探针需要跟上")
    sys.exit(2)


class Dummy:
    enable_essence = True
    enable_join_request = True
    enable_notice = True

    # 真实函数注入（真源码签名引用 KiraMessageBatchEvent，仅注解 —— def 时求值，需先备好桩）
    ns = {"KiraMessageBatchEvent": object}
    import asyncio as _asyncio_mod
    import json as _json_mod
    ns["asyncio"] = _asyncio_mod
    ns["json"] = _json_mod
    ns["logger"] = type("L", (), {"info": staticmethod(lambda *a, **k: None),
                                  "warning": staticmethod(lambda *a, **k: None)})()
    ns["_UNKNOWN_ACTION_RE"] = __import__("re").compile(
        r"unknown|不支持|not.?support|invalid action|未实现|不存在|no such", __import__("re").I)
    for fname in ("_fetch_pending_requests", "check_join_requests",
                  "list_essence", "_request_key", "_parse_essence_item",
                  "_segments_to_text", "_web_msg_content_to_text",
                  "_fetch_msg_text", "delete_notice", "_load_seen_flags"):
        code = extract_func(fname)
        exec(compile(ast.Module(body=[ast.parse(code).body[0]], type_ignores=[]),
                     "<extracted>", "exec"), ns)
    exec(extract_assignment("_SEGMENT_PLACEHOLDERS"), ns)
    _fetch_pending_requests = ns["_fetch_pending_requests"]
    check_join_requests = ns["check_join_requests"]
    list_essence = ns["list_essence"]
    _request_key = staticmethod(ns["_request_key"])
    _parse_essence_item = ns["_parse_essence_item"]
    _web_msg_content_to_text = staticmethod(ns["_web_msg_content_to_text"])
    _fetch_msg_text = staticmethod(ns["_fetch_msg_text"])
    delete_notice = ns["delete_notice"]
    _load_seen_flags = ns["_load_seen_flags"]
    _SEGMENT_PLACEHOLDERS = ns["_SEGMENT_PLACEHOLDERS"]

    def __init__(self):
        self._seen_flags = {}
        self.saved = 0
        self.logged = []
        self.action_calls = []
        self.essence_data = []
        # get_msg 反查用的 client（None = 不可用，list_essence 应跳过反查）
        self.msg_client = None
        # 删公告行为表：action → None(成功) 或 错误消息；缺省按「未知 action」处理
        self.notice_behavior = {}
        # ctx 桩：check_join_requests 走 ctx.adapter_mgr.get_adapter(event.adapter.name)
        adapter = FakeAdapter({"status": "ok", "retcode": 0, "data": [
            {"request_id": 2001, "group_id": 123, "requester_uin": 999,
             "requester_nick": "小红", "message": "拉我", "checked": False,
             "flag": "slreq:1:2001:123:1:0"},
        ]})
        self.ctx = type("Ctx", (), {"adapter_mgr": type(
            "M", (), {"get_adapter": staticmethod(lambda name: adapter)})()})()

    def _get_qq_client(self, event):
        return self.msg_client

    def _save_seen_flags(self):
        self.saved += 1

    def _is_admin(self, event):
        return True

    def _log_operation(self, op, operator, target, detail):
        self.logged.append((op, operator, target, detail))

    def _format_time(self, ts):
        return str(ts)

    async def _call_group_action(self, event, action, params, op_name, target="", need_group=True):
        self.action_calls.append(action)
        if action == "get_essence_msg_list":
            return self.essence_data, None, "op"
        if action in ("_del_group_notice", "_delete_group_notice"):
            # 缺省模拟 LLOneBot：不认识 _del_group_notice
            err = self.notice_behavior.get(action, f"Unknown action '{action}'")
            if err:
                return None, f"❌ {op_name}失败: {err}", "op"
            return {}, None, "op"
        return None, f"未知action {action}", "op"


# _fetch_msg_text 内部直接引用 GroupManagerPlugin._segments_to_text ——
# 用 shim 类提供同名 classmethod，保证抽取的真源码能解析到真实实现
class _Shim:
    _SEGMENT_PLACEHOLDERS = Dummy._SEGMENT_PLACEHOLDERS
    _segments_to_text = classmethod(Dummy.ns["_segments_to_text"])


Dummy.ns["GroupManagerPlugin"] = _Shim
Dummy._segments_to_text = _Shim._segments_to_text

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

print("== D. list_essence 四形态正文兼容（v1.2.4 核心修复）==")

# D1: 旧版 SnowLuma 透传 QQ web 原始格式 —— msg_content / sender_uin /
#     add_digest_*，无 message_id（2026-10-08 用户线上命中的真实场景）
D1_ITEM = {"sender_nick": "爱理奈", "sender_uin": "769690776", "sender_time": 1753934607,
           "add_digest_uin": "111", "add_digest_nick": "C7", "add_digest_time": 1753934700,
           "msg_content": [{"msg_type": 1, "text": "网线保住了，下次还测233"}]}
d2.essence_data = [D1_ITEM]
out = asyncio.run(d2.list_essence(FakeEvent(), 10))
check("D1 旧 SnowLuma 透传格式显示正文", "网线保住了，下次还测233" in out)
check("D1 无 message_id 时明确标注", "id=不可用" in out)
check("D1 显示设置人 add_digest_nick", "C7设置" in out)

# D2: 旧 SnowLuma 混合格式：text + image + face
d2.essence_data = [{"sender_nick": "乙", "sender_time": 100, "msg_content": [
    {"msg_type": 1, "text": "看这个"}, {"msg_type": 3, "image_url": "http://x"},
    {"msg_type": 2, "face_index": 14}]}]
out = asyncio.run(d2.list_essence(FakeEvent(), 10))
check("D2 混合内容占位符渲染", "看这个[图片][表情]" in out)

# D3: LLOneBot 形态 —— 只有元数据，无 content/msg_content，有 message_id
#     → get_msg 反查出正文
D3_ITEM = {"sender_id": 769690776, "sender_nick": "爱理奈", "sender_time": 1753934607,
           "operator_id": 111, "operator_nick": "C7", "operator_time": 1753934700,
           "message_id": -12345}
d3 = Dummy()
d3.essence_data = [D3_ITEM]
d3.msg_client = FakeClient({"status": "ok", "data": {
    "time": 1753934607,
    "message": [{"type": "text", "data": {"text": "反查到的正文"}},
                {"type": "image", "data": {"url": "http://y"}}]}})
out = asyncio.run(d3.list_essence(FakeEvent(), 10))
check("D3 LLOneBot 无 content → get_msg 反查正文", "反查到的正文[图片]" in out)
check("D3 输出携带 message_id 供 unset 使用", "id=-12345" in out)
check("D3 get_msg 被调用一次", d3.msg_client.calls == ["get_msg"])

# D4: NapCat 形态 —— content 段数组 + 无 sender_time → 回退 operator_time
d4 = Dummy()
d4.essence_data = [{"sender_nick": "甲", "operator_nick": "C7", "operator_time": 1753934700,
                    "message_id": 777, "content": [{"type": "text", "data": {"text": "hi"}}]}]
out = asyncio.run(d4.list_essence(FakeEvent(), 10))
check("D4 无 sender_time 回退 operator_time", "[1753934700]" in out and "hi" in out)

# D5: 全部获取渠道失败 → 诚实占位，不崩不留白
d5 = Dummy()
d5.essence_data = [{"sender_nick": "丙", "sender_time": 100}]
out = asyncio.run(d5.list_essence(FakeEvent(), 10))
check("D5 无内容显示 [内容不可用]", "[内容不可用]" in out)

# D6: get_msg 反查失败（有 id 但接口报错）→ 占位，不崩
d6 = Dummy()
d6.essence_data = [{"sender_nick": "丁", "sender_time": 100, "message_id": 999}]
d6.msg_client = FakeClient({"status": "failed", "message": "消息不存在"})
out = asyncio.run(d6.list_essence(FakeEvent(), 10))
check("D6 反查失败显示 [内容不可用]", "[内容不可用]" in out)

# D7: 反向验证 —— 旧逻辑（v1.2.3）对 D1/D3 必然空正文（证明 bug 真实存在、探针有效）
def _old_parse(it):
    """v1.2.3 旧解析逻辑的逐字复刻（验证用）"""
    content = it.get("content")
    if isinstance(content, list):
        parts = []
        for seg in content:
            if isinstance(seg, dict) and seg.get("type") == "text":
                parts.append(str((seg.get("data") or {}).get("text") or ""))
        content = "".join(parts)
    elif not isinstance(content, str):
        content = ""
    return content.strip()

check("D7 反向验证：旧逻辑对旧 SnowLuma 格式必然空正文", _old_parse(D1_ITEM) == "")
check("D7 反向验证：旧逻辑对 LLOneBot 格式必然空正文", _old_parse(D3_ITEM) == "")

# D8: data 非数组（协议端异常返回 dict）→ 报错不崩（旧代码 dict 切片直接 KeyError）
d8 = Dummy()
d8.essence_data = {"msg_list": []}
out = asyncio.run(d8.list_essence(FakeEvent(), 10))
check("D8 data 非数组诚实报错不崩", out.startswith("❌") and "格式异常" in out)

print("== E. delete_notice 三端双名兜底 ==")

# E1: NapCat/SnowLuma —— 首选名直接成功，不触发第二次调用
de = Dummy()
de.notice_behavior = {"_del_group_notice": None}
out = asyncio.run(de.delete_notice(FakeEvent(), "nid1"))
check("E1 首选名成功", out.startswith("✅") and de.action_calls == ["_del_group_notice"])

# E2: LLOneBot —— 首选名 Unknown action → 自动换名成功
de2 = Dummy()
de2.notice_behavior = {"_delete_group_notice": None}
out = asyncio.run(de2.delete_notice(FakeEvent(), "nid2"))
check("E2 未知 action 自动换名重试成功", out.startswith("✅")
      and de2.action_calls == ["_del_group_notice", "_delete_group_notice"])

# E3: 真实失败（权限不足）—— 不触发第二次调用，原样报错
de3 = Dummy()
de3.notice_behavior = {"_del_group_notice": "机器人不是群管理员"}
out = asyncio.run(de3.delete_notice(FakeEvent(), "nid3"))
check("E3 权限失败不盲目重试", out.startswith("❌") and de3.action_calls == ["_del_group_notice"])

# E4: 错误措辞大小写/变体（"NOT SUPPORTED"）也能识别换名
de4 = Dummy()
de4.notice_behavior = {"_del_group_notice": "Action NOT SUPPORTED",
                       "_delete_group_notice": None}
out = asyncio.run(de4.delete_notice(FakeEvent(), "nid4"))
check("E4 错误措辞变体识别", out.startswith("✅") and len(de4.action_calls) == 2)

print("== F. _load_seen_flags 类型回归（set() bug）==")

import tempfile
df = Dummy()
bad = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
bad.write("{损坏的json")
bad.close()
df._seen_flags_path = lambda: __import__("pathlib").Path(bad.name)
df._load_seen_flags()
check("F1 JSON 损坏兜底为 dict", isinstance(df._seen_flags, dict))
try:
    df._seen_flags["k"] = None
    check("F2 兜底后可正常赋值（旧 set() 会抛 TypeError）", True)
except TypeError:
    check("F2 兜底后可正常赋值（旧 set() 会抛 TypeError）", False)
check("F3 源码不再出现 set() 兜底", "self._seen_flags = set()" not in SRC)

print(f"\n{'='*46}\n通过 {PASS} 条" + (f"，失败 {len(FAIL)} 条: {FAIL}" if FAIL else "，全部绿 ✓"))
sys.exit(1 if FAIL else 0)
