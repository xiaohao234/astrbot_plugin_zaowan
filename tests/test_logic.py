"""
早晚安打卡插件自测套件（无需安装 AstrBot，使用桩模块模拟框架接口）

运行方式：
    cd astrbot_plugin_zaowan
    python3 tests/test_logic.py

覆盖范围：需求样例逐条复现、时间窗口边界、每日 6 点清零、重复打卡、
配对窗口、误触防护、群隔离、数据持久化 / 损坏自愈、内存上限、性能。
退出码 0 表示全部通过。
"""

import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

PLUGIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_DIR))

TZ = timezone(timedelta(hours=8))


def T(*args):
    """构造测试时间（UTC+8）。"""
    return datetime(*args, tzinfo=TZ)


# ----------------------------------------------------------------------
# 桩：模拟 astrbot 框架接口
# ----------------------------------------------------------------------
def install_stubs(base_dir: Path):
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    event_mod = ModuleType("astrbot.api.event")
    filter_mod = ModuleType("astrbot.api.event.filter")
    star_mod = ModuleType("astrbot.api.star")
    mc_mod = ModuleType("astrbot.api.message_components")

    class AstrBotConfig(dict):
        pass

    class Context:
        pass

    class Star:
        def __init__(self, context=None):
            self.context = context

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            print("[stub-warn]", *a)

        def error(self, *a, **k):
            print("[stub-err]", *a)

        def debug(self, *a, **k):
            pass

    class At:
        def __init__(self, qq=None):
            self.qq = qq

    class Plain:
        def __init__(self, text=""):
            self.text = text

    class EventMessageType:
        ALL = "all"
        PRIVATE_MESSAGE = "private"
        GROUP_MESSAGE = "group"

    filter_mod.command = lambda *a, **k: (lambda fn: fn)
    filter_mod.event_message_type = lambda *a, **k: (lambda fn: fn)
    filter_mod.EventMessageType = EventMessageType

    class StarTools:
        _base = base_dir

        @staticmethod
        def get_data_dir(name):
            d = base_dir / name
            d.mkdir(parents=True, exist_ok=True)
            return d

    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools
    star_mod.register = lambda *a, **k: (lambda cls: cls)

    event_mod.filter = filter_mod

    class AstrMessageEvent:  # 仅用于类型注解
        pass

    event_mod.AstrMessageEvent = AstrMessageEvent

    api.AstrBotConfig = AstrBotConfig
    api.logger = _Logger()
    api.message_components = mc_mod
    mc_mod.At = At
    mc_mod.Plain = Plain

    astrbot.api = api
    for name, mod in [
        ("astrbot", astrbot),
        ("astrbot.api", api),
        ("astrbot.api.event", event_mod),
        ("astrbot.api.event.filter", filter_mod),
        ("astrbot.api.star", star_mod),
        ("astrbot.api.message_components", mc_mod),
    ]:
        sys.modules[name] = mod


# ----------------------------------------------------------------------
# 模拟事件对象
# ----------------------------------------------------------------------
class FakeMessageObj:
    def __init__(self, group_id="g1", self_id="bot"):
        self.group_id = group_id
        self.self_id = self_id


class FakeEvent:
    def __init__(self, text, uid="10001", name="用户A", group_id="g1"):
        self.message_str = text
        self.unified_msg_origin = (
            f"test:GroupMessage:{group_id}" if group_id else f"test:FriendMessage:{uid}"
        )
        self.message_obj = FakeMessageObj(group_id)
        self._uid = uid
        self._name = name
        self.results = []
        self.llm_blocked = False

    def should_call_llm(self, call_llm):
        # AstrBot 语义：call_llm=True 表示禁止默认 LLM 请求本条消息
        self.llm_blocked = call_llm

    def get_sender_id(self):
        return self._uid

    def get_sender_name(self):
        return self._name

    def plain_result(self, text):
        self.results.append(("plain", text))
        return ("plain", text)

    def chain_result(self, chain):
        self.results.append(("chain", chain))
        return ("chain", chain)


def text_of(ev: FakeEvent) -> str:
    """提取回复文本（chain 中 Plain 的文字 / plain 文本）。"""
    assert ev.results, "应当产生回复，但没有"
    kind, payload = ev.results[0]
    if kind == "chain":
        return "".join(getattr(c, "text", "") for c in payload)
    return payload


def drive(plugin, ev):
    """驱动异步生成器 handler，收集回复。"""

    async def run():
        out = []
        async for r in plugin.on_message(ev):
            out.append(r)
        return out

    return asyncio.run(run())


# ----------------------------------------------------------------------
# 测试用例
# ----------------------------------------------------------------------
def make_plugin(tmp: Path, config=None, now=None):
    from main import ZaowanPlugin

    p = ZaowanPlugin(None, dict(config or {}))
    if now is not None:
        p._now = lambda: now
    return p


def test_sample_scenario(tmp):
    """逐条复现需求给出的 6 条样例输出。"""
    p = make_plugin(tmp)

    # 8-19 22:00 某人晚安 -> 今晚第1个
    ev = FakeEvent("晚安", uid="20001", name="某人")
    p._now = lambda: T(2026, 8, 19, 22, 0, 0)
    drive(p, ev)
    assert text_of(ev) == "\u200b 晚安成功！你是今晚第1个睡觉的群友！", text_of(ev)
    assert ev.llm_blocked, "打卡命中应禁止默认 LLM（should_call_llm(True)）"

    # 8-19 23:40:35 不是很懂晚安 -> 今晚第2个
    ev = FakeEvent("晚安", uid="20003", name="不是很懂")
    p._now = lambda: T(2026, 8, 19, 23, 40, 35)
    drive(p, ev)
    assert "你是今晚第2个睡觉的群友" in text_of(ev), text_of(ev)

    # 8-20 06:50:54 不是很懂早安 -> 睡眠 7h10m19s，今早第1个
    ev = FakeEvent("早安", uid="20003", name="不是很懂")
    p._now = lambda: T(2026, 8, 20, 6, 50, 54)
    drive(p, ev)
    assert text_of(ev) == (
        "\u200b 早安成功！你的睡眠时长为7h10m19s，\n你是今早第1个起床的群友！"
    ), text_of(ev)

    # 8-20 08:00 雾雨徊早安（从没打过晚安）-> 只回 早安～
    ev = FakeEvent("早安", uid="20002", name="雾雨徊")
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    drive(p, ev)
    assert text_of(ev) == "\u200b 早安，雾雨徊～", text_of(ev)

    # 8-20 22:00 某人今晚再次晚安（新的一晚，排名已清零）-> 第1个
    ev = FakeEvent("晚安", uid="20001", name="某人")
    p._now = lambda: T(2026, 8, 20, 22, 0, 0)
    drive(p, ev)
    assert "你是今晚第1个睡觉的群友" in text_of(ev), text_of(ev)

    # 8-20 22:31:10 不是很懂晚安 -> 清醒 15h40m16s，今晚第2个
    ev = FakeEvent("晚安", uid="20003", name="不是很懂")
    p._now = lambda: T(2026, 8, 20, 22, 31, 10)
    drive(p, ev)
    assert text_of(ev) == (
        "\u200b 晚安成功！你今天的清醒时长为15h40m16s，\n你是今晚第2个睡觉的群友！"
    ), text_of(ev)

    # 8-20 15:00 早安/晚安 都不在时间窗口内
    ev = FakeEvent("早安", uid="20002", name="雾雨徊")
    p._now = lambda: T(2026, 8, 20, 15, 0, 0)
    drive(p, ev)
    assert text_of(ev) == "\u200b 雾雨徊，现在不能早安哦，可以早安的时间为6时到12时~", text_of(ev)

    ev = FakeEvent("晚安", uid="20001", name="某人")
    drive(p, ev)
    assert text_of(ev) == "\u200b 某人，现在不能晚安哦，可以晚安的时间为21时到第二天早上6时~", text_of(ev)


def test_dup_and_reset(tmp):
    """重复打卡提示；凌晨晚安计入前一晚；次日排名自动清零。"""
    p = make_plugin(tmp)
    # 第一晚：A 22:00 -> 1, B 23:00 -> 2
    p._now = lambda: T(2026, 8, 19, 22, 0, 0)
    ev = FakeEvent("晚安", uid="A")
    drive(p, ev)
    assert "第1个" in text_of(ev)
    p._now = lambda: T(2026, 8, 19, 23, 0, 0)
    ev = FakeEvent("晚安", uid="B")
    drive(p, ev)
    assert "第2个" in text_of(ev)
    # B 同晚重复晚安（距上次 30 分钟 < 6 小时）
    p._now = lambda: T(2026, 8, 19, 23, 30, 0)
    ev = FakeEvent("晚安", uid="B")
    drive(p, ev)
    assert "6小时内你已经晚安过了哦~" in text_of(ev), text_of(ev)
    # 次日 00:30 C 晚安，仍属前一晚 -> 第3个
    p._now = lambda: T(2026, 8, 20, 0, 30, 0)
    ev = FakeEvent("晚安", uid="C")
    drive(p, ev)
    assert "第3个" in text_of(ev), text_of(ev)
    # 次日 A 早安（有昨晚晚安，配对成功）
    p._now = lambda: T(2026, 8, 20, 7, 0, 0)
    ev = FakeEvent("早安", uid="A")
    drive(p, ev)
    assert "早安成功" in text_of(ev) and "第1个起床" in text_of(ev), text_of(ev)
    # A 同日重复早安 -> 静默不理会（不回复）
    ev = FakeEvent("早安", uid="A")
    drive(p, ev)
    assert not ev.results, f"重复早安应不回复，但回复了 {ev.results}"
    # 第二天晚上（21日晚）A 再晚安 -> 排名已清零，第1个
    p._now = lambda: T(2026, 8, 21, 21, 30, 0)
    ev = FakeEvent("晚安", uid="A")
    drive(p, ev)
    assert "第1个" in text_of(ev), text_of(ev)


def test_dup_night_window(tmp):
    """重复晚安 6 小时窗口：窗口内提示、恰好 6 小时整视为新晚安。"""
    p = make_plugin(tmp)
    # A 21:00 -> 第1
    p._now = lambda: T(2026, 8, 19, 21, 0, 0)
    ev = FakeEvent("晚安", uid="A")
    drive(p, ev)
    assert "第1个" in text_of(ev), text_of(ev)
    # B 22:00 -> 第2
    p._now = lambda: T(2026, 8, 19, 22, 0, 0)
    ev = FakeEvent("晚安", uid="B")
    drive(p, ev)
    assert "第2个" in text_of(ev), text_of(ev)
    # A 23:00（距上次 2h < 6h）-> 重复提示，不记录
    p._now = lambda: T(2026, 8, 19, 23, 0, 0)
    ev = FakeEvent("晚安", uid="A")
    drive(p, ev)
    assert "6小时内你已经晚安过了哦~" in text_of(ev), text_of(ev)
    # B 03:00（距上次 5h < 6h）-> 重复提示
    p._now = lambda: T(2026, 8, 20, 3, 0, 0)
    ev = FakeEvent("晚安", uid="B")
    drive(p, ev)
    assert "6小时内你已经晚安过了哦~" in text_of(ev), text_of(ev)
    # A 恰好 03:00:00（距 21:00 整 6 小时，不含）-> 新的晚安（半夜醒后再睡）
    # 当晚已睡过 A、B 两人，A 的再睡排名为第 2
    p._now = lambda: T(2026, 8, 20, 3, 0, 0)
    ev = FakeEvent("晚安", uid="A")
    drive(p, ev)
    assert "第2个" in text_of(ev) and "晚安成功" in text_of(ev), text_of(ev)
    # A 03:30（距 03:00 仅 30 分钟）-> 再次进入重复提示
    p._now = lambda: T(2026, 8, 20, 3, 30, 0)
    ev = FakeEvent("晚安", uid="A")
    drive(p, ev)
    assert "6小时内你已经晚安过了哦~" in text_of(ev), text_of(ev)
    # 次日早安时与最近一次晚安（03:00）配对计算睡眠时长
    p._now = lambda: T(2026, 8, 20, 11, 0, 0)
    ev = FakeEvent("早安", uid="A")
    drive(p, ev)
    assert "睡眠时长为8h" in text_of(ev), text_of(ev)


def test_dup_night_window_large_config(tmp):
    """把窗口配置成 24 小时：跨晚的新晚安也不会被误判为重复。"""
    p = make_plugin(tmp, {"dup_night_hours": 24})
    # 8/19 22:00 A 晚安 -> 第1
    p._now = lambda: T(2026, 8, 19, 22, 0, 0)
    drive(p, FakeEvent("晚安", uid="A"))
    # 1.5 小时后重复 -> 提示
    p._now = lambda: T(2026, 8, 19, 23, 30, 0)
    ev = FakeEvent("晚安", uid="A")
    drive(p, ev)
    assert "24小时内你已经晚安过了哦~" in text_of(ev), text_of(ev)
    # 次日 21:30（间隔 23.5h < 24h，但已跨过 6 点清零界）-> 新的晚安，第1
    p._now = lambda: T(2026, 8, 20, 21, 30, 0)
    ev = FakeEvent("晚安", uid="A")
    drive(p, ev)
    assert "晚安成功" in text_of(ev) and "第1个" in text_of(ev), text_of(ev)


def test_boundaries(tmp):
    """时间窗口边界：6/12/21/6 点。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 20, 5, 59, 59)
    ev = FakeEvent("晚安", uid="X")
    drive(p, ev)
    assert "晚安成功" in text_of(ev)

    p._now = lambda: T(2026, 8, 20, 6, 0, 0)
    ev = FakeEvent("晚安", uid="Y")
    drive(p, ev)
    assert "现在不能晚安哦" in text_of(ev), text_of(ev)

    ev = FakeEvent("早安", uid="Y")
    drive(p, ev)
    assert text_of(ev).endswith("早安，用户A～"), text_of(ev)

    p._now = lambda: T(2026, 8, 20, 11, 59, 59)
    ev = FakeEvent("早安", uid="Z")
    drive(p, ev)
    assert "早安" in text_of(ev)

    p._now = lambda: T(2026, 8, 20, 12, 0, 0)
    ev = FakeEvent("早安", uid="W")
    drive(p, ev)
    assert "现在不能早安哦" in text_of(ev), text_of(ev)

    p._now = lambda: T(2026, 8, 20, 21, 0, 0)
    ev = FakeEvent("晚安", uid="W")
    drive(p, ev)
    assert "晚安成功" in text_of(ev)


def test_pair_window(tmp):
    """昨晚的晚安才配对睡眠时长；隔天太久只回 早安～。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 19, 23, 0, 0)
    drive(p, FakeEvent("晚安", uid="P"))
    # 两天后的早安：间隔 33h > 24h 窗口
    p._now = lambda: T(2026, 8, 21, 8, 0, 0)
    ev = FakeEvent("早安", uid="P")
    drive(p, ev)
    assert text_of(ev).endswith("早安，用户A～"), text_of(ev)


def test_no_false_positive(tmp):
    """误触防护：闲聊/指令/空消息不触发。"""
    p = make_plugin(tmp)
    # 均非问候语：含关键字但不是打卡语义
    cases = ["早饭后吃什么呢", "晚安故事会开始了", "/help", "", "在吗", "我今晚要早睡"]
    for i, c in enumerate(cases):
        ev = FakeEvent(c, uid=f"u{i}")
        drive(p, ev)
        assert not ev.results, f"“{c}” 不应触发，但回复了 {ev.results}"
    # “早上好呀大家”称呼在句尾，属于真实问候语，应触发
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    ev = FakeEvent("早上好呀大家", uid="ok1")
    drive(p, ev)
    assert ev.results, "“早上好呀大家”应触发早安"


def test_variants(tmp):
    """变体问候语触发。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    # 同一用户重复早安会被静默忽略，因此每个变体使用不同用户
    for i, c in enumerate(["大家早安呀~", "早!", "早呀", "GM", "Good Morning!!", "起床啦~", "我醒了"]):
        ev = FakeEvent(c, uid=f"VM{i}")
        drive(p, ev)
        assert ev.results, f"“{c}” 应触发早安"
        ev.results.clear()
    p._now = lambda: T(2026, 8, 20, 23, 0, 0)
    # 同样使用不同用户，避免 6 小时重复晚安窗口拦截
    for j, c in enumerate(["各位晚安啦", "困死了", "我去睡了~", "goodnight", "睡觉觉"]):  # 睡觉觉不在默认词表
        ev = FakeEvent(c, uid=f"VN{j}")
        drive(p, ev)
        if c != "睡觉觉":
            assert ev.results, f"“{c}” 应触发晚安"
        ev.results.clear()


def test_group_isolation(tmp):
    """不同群排名相互独立。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 19, 22, 0, 0)
    ev = FakeEvent("晚安", uid="A", group_id="g1")
    drive(p, ev)
    assert "第1个" in text_of(ev)
    ev = FakeEvent("晚安", uid="B", group_id="g2")
    drive(p, ev)
    assert "第1个" in text_of(ev), "另一个群的第一个晚安应为第1个"


def test_private_disabled(tmp):
    """默认私聊不生效。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    ev = FakeEvent("早安", uid="A", group_id="")
    drive(p, ev)
    assert not ev.results


def test_self_message_ignored(tmp):
    """机器人自身消息不触发（防自回复死循环）。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    ev = FakeEvent("早安", uid="bot")
    ev.message_obj.self_id = "bot"
    drive(p, ev)
    assert not ev.results


def test_persistence(tmp):
    """数据落盘：重启（新建实例）后状态完整恢复。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 19, 22, 0, 0)
    drive(p, FakeEvent("晚安", uid="A"))
    p._now = lambda: T(2026, 8, 20, 7, 0, 0)
    drive(p, FakeEvent("早安", uid="A"))

    # 模拟重启：同一数据目录新建实例
    p2 = make_plugin(tmp)
    p2._now = lambda: T(2026, 8, 20, 7, 5, 0)
    ev = FakeEvent("早安", uid="A")
    drive(p2, ev)
    # 重复早安静默不理会：若重启丢数据，这里会回复「早安～」，故无回复即证明状态已恢复
    assert not ev.results, f"重启后重复早安应静默不理会，但回复了 {ev.results}"
    # 再用统计指令确认打卡记录完整恢复
    stats_ev = FakeEvent("/sleep_stat", uid="A")

    async def _stat():
        async for _ in p2.sleep_stat(stats_ev):
            pass

    asyncio.run(_stat())
    assert "早安次数: 1" in text_of(stats_ev), text_of(stats_ev)
    # 落盘文件是合法 JSON
    f = list(tmp.rglob("records.json"))
    assert f, "records.json 应存在"
    data = json.loads(f[0].read_text(encoding="utf-8"))
    assert data["sessions"], data


def test_corrupt_recovery(tmp):
    """文件损坏：自动备份并重建，不崩溃。"""
    f = tmp / "astrbot_plugin_zaowan" / "records.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("{{{not a json!!!", encoding="utf-8")
    p = make_plugin(tmp)  # 不应抛异常
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    ev = FakeEvent("早安", uid="A")
    drive(p, ev)
    assert ev.results
    backups = list(tmp.rglob("records.json.corrupt-*"))
    assert backups, "损坏文件应被备份"


def test_history_cap(tmp):
    """历史记录封顶，内存/文件体积有上界。"""
    p = make_plugin(tmp, {"history_cap": 3})
    for day in range(10, 16):
        p._now = lambda d=day: T(2026, 8, d, 22, 0, 0)
        drive(p, FakeEvent("晚安", uid="A"))
        p._now = lambda d=day: T(2026, 8, d + 1, 7, 0, 0)
        drive(p, FakeEvent("早安", uid="A"))
    u = p._records["sessions"]["test:GroupMessage:g1"]["users"]["A"]
    assert len(u["history"]) <= 3, len(u["history"])


def test_fuzzy_mode(tmp):
    """模糊匹配开关行为。"""
    p = make_plugin(tmp, {"fuzzy_match": False})
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    ev = FakeEvent("我看完这个视频就早安", uid="A")
    drive(p, ev)
    assert not ev.results, "默认整句匹配，包含词不应触发"

    p2 = make_plugin(tmp, {"fuzzy_match": True})
    p2._now = lambda: T(2026, 8, 20, 8, 0, 0)
    ev = FakeEvent("我看完这个视频就早安", uid="A")
    drive(p2, ev)
    assert ev.results, "模糊模式下包含触发词应触发"


def test_regex_dos_guard(tmp):
    """超长消息（含纯语气词刷屏）不触发且耗时可控。"""
    import time as _t

    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    t0 = _t.time()
    for payload in ["～" * 5000, "早" + "～" * 5000, "a" * 10000, "早安" * 3000]:
        ev = FakeEvent(payload, uid="A")
        drive(p, ev)
        assert not ev.results, "超长消息不应触发"
    cost = _t.time() - t0
    assert cost < 2.0, f"超长消息处理过慢: {cost:.2f}s"


def test_history_cap_zero_sanitize(tmp):
    """history_cap=0 时，旧文件中的历史在加载阶段即被丢弃。"""
    f = tmp / "astrbot_plugin_zaowan" / "records.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "version": 1,
        "sessions": {"s1": {"users": {"u1": {
            "name": "X", "last_goodnight": 100.0, "last_goodmorning": 0.0,
            "last_paired_morning": 0.0, "morning_total": 1, "night_total": 1,
            "paired_morning_total": 0, "paired_night_total": 0,
            "sleep_total": 0.0, "awake_total": 0.0,
            "history": [{"t": 100.0, "k": "n", "r": 1, "d": None}] * 10,
        }}}}
    }), encoding="utf-8")
    p = make_plugin(tmp, {"history_cap": 0})
    u = p._records["sessions"]["s1"]["users"]["u1"]
    assert u["history"] == [], f"cap=0 不应加载历史，实际 {len(u['history'])} 条"
    # 但打卡统计字段正常恢复
    assert u["night_total"] == 1


def test_query_group(tmp):
    """群友作息：今日早晚安人数统计。"""
    p = make_plugin(tmp)
    # 空群查询 -> 0/0
    p._now = lambda: T(2026, 8, 20, 12, 0, 0)
    ev = FakeEvent("群友作息", uid="X")
    drive(p, ev)
    assert text_of(ev) == "今天已经有0位群友早安了，0位群友晚安了~", text_of(ev)
    # A、B 早安，B、C、D 晚安
    p._now = lambda: T(2026, 8, 20, 7, 0, 0)
    drive(p, FakeEvent("早安", uid="A"))
    drive(p, FakeEvent("早安", uid="B"))
    p._now = lambda: T(2026, 8, 20, 22, 0, 0)
    drive(p, FakeEvent("晚安", uid="B"))
    drive(p, FakeEvent("晚安", uid="C"))
    drive(p, FakeEvent("晚安", uid="D"))
    p._now = lambda: T(2026, 8, 20, 23, 0, 0)
    ev = FakeEvent("群友作息", uid="X")
    drive(p, ev)
    assert text_of(ev) == "今天已经有2位群友早安了，3位群友晚安了~", text_of(ev)
    # 次日中午查询：排名已清零（B 昨晚的晚安属于昨天打卡日）
    p._now = lambda: T(2026, 8, 21, 12, 0, 0)
    ev = FakeEvent("群友作息", uid="X")
    drive(p, ev)
    assert text_of(ev) == "今天已经有0位群友早安了，0位群友晚安了~", text_of(ev)


def test_query_self(tmp):
    """我的作息：完整输出格式（含样例风格的时间与中文时长）。"""
    p = make_plugin(tmp)
    # 两晚打卡：8/13(周四) 23:01:38 晚安 -> 8/14 08:23:36 早安（9h21m58s）
    #           8/14 23:30:00 晚安 -> 8/15(周六) 07:02:40 早安（7h32m40s）
    p._now = lambda: T(2026, 8, 13, 23, 1, 38)
    drive(p, FakeEvent("晚安", uid="20003", name="不是很懂"))
    p._now = lambda: T(2026, 8, 14, 8, 23, 36)
    drive(p, FakeEvent("早安", uid="20003", name="不是很懂"))
    p._now = lambda: T(2026, 8, 14, 23, 30, 0)
    drive(p, FakeEvent("晚安", uid="20003", name="不是很懂"))
    p._now = lambda: T(2026, 8, 15, 7, 2, 40)
    drive(p, FakeEvent("早安", uid="20003", name="不是很懂"))

    p._now = lambda: T(2026, 8, 15, 12, 0, 0)
    ev = FakeEvent("我的作息", uid="20003", name="不是很懂")
    drive(p, ev)
    got = text_of(ev)
    assert got == (
        "\u200b 你的作息数据如下：\n"
        "最近一次早安时间为2026-08-15 07:02:40\n"
        "最近一次晚安时间为2026-08-14 23:30:00\n"
        "本周早安了2次\n"
        "本周晚安了2次\n"
        "一共早安了2次\n"
        "一共晚安了2次\n"
        "一共睡眠了0天16时54分38秒"
    ), got


def test_query_self_week_reset(tmp):
    """跨周后周计数自动重置（周一 6 点为周界）。"""
    p = make_plugin(tmp)
    # 第 1 周（8/10 周一 - 8/16 周日）打卡 2 早安 2 晚安
    for day in (11, 12):
        p._now = lambda d=day: T(2026, 8, d, 22, 0, 0)
        drive(p, FakeEvent("晚安", uid="A"))
        p._now = lambda d=day: T(2026, 8, d + 1, 7, 0, 0)
        drive(p, FakeEvent("早安", uid="A"))
    p._now = lambda: T(2026, 8, 12, 12, 0, 0)
    ev = FakeEvent("我的作息", uid="A")
    drive(p, ev)
    assert "本周早安了2次" in text_of(ev) and "本周晚安了2次" in text_of(ev), text_of(ev)
    # 下周二（8/18）查询且未打卡 -> 本周归零
    p._now = lambda: T(2026, 8, 18, 12, 0, 0)
    ev = FakeEvent("我的作息", uid="A")
    drive(p, ev)
    got = text_of(ev)
    assert "本周早安了0次" in got and "本周晚安了0次" in got, got
    assert "一共早安了2次" in got, "累计不应被清零"
    # 下周二晚再打卡 1 次 -> 本周 1
    p._now = lambda: T(2026, 8, 18, 22, 0, 0)
    drive(p, FakeEvent("晚安", uid="A"))
    p._now = lambda: T(2026, 8, 18, 23, 0, 0)
    ev = FakeEvent("我的作息", uid="A")
    drive(p, ev)
    got = text_of(ev)
    assert "本周晚安了1次" in got and "一共晚安了3次" in got, got


def test_query_self_no_record(tmp):
    """未打卡用户查询：全 0 且不建档。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 20, 12, 0, 0)
    ev = FakeEvent("我的作息", uid="nobody")
    drive(p, ev)
    got = text_of(ev)
    assert "暂无记录" in got and "一共早安了0次" in got and "0天0时0分0秒" in got, got
    # 查询不应创建用户档案
    sessions = p._records["sessions"]
    assert "test:GroupMessage:g1" not in sessions or "nobody" not in sessions["test:GroupMessage:g1"]["users"]


def test_query_words_not_checkin(tmp):
    """查询词不会被当作早晚安打卡。"""
    p = make_plugin(tmp)
    # 早安时段发“群友作息”不应触发打卡回复
    p._now = lambda: T(2026, 8, 20, 8, 0, 0)
    ev = FakeEvent("群友作息", uid="A")
    drive(p, ev)
    assert "早安" not in text_of(ev).split("~")[0] or "位群友" in text_of(ev)
    # 我的作息也不触发打卡
    ev = FakeEvent("我的作息", uid="A")
    drive(p, ev)
    assert "作息数据" in text_of(ev)
    # 用户确实没有打卡档案（查询不建档，会话可能整个不存在）
    sessions = p._records["sessions"]
    assert "test:GroupMessage:g1" not in sessions or "A" not in sessions["test:GroupMessage:g1"]["users"]


def test_query_upgrade_backfill(tmp):
    """旧版数据（无周计数字段）升级时从历史回填本周计数。"""
    f = tmp / "astrbot_plugin_zaowan" / "records.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    # 历史时间戳按「真实当前时间」所在周动态生成：加载阶段的回填比较的是
    # 真实时钟的周界，写死日期会让测试随时间流逝而失效（过期教训）
    base = datetime.now(TZ)
    week_mon_6 = (base - timedelta(days=base.weekday())).replace(
        hour=6, minute=0, second=0, microsecond=0
    )
    t_night = week_mon_6 + timedelta(days=2, hours=17)    # 周三 23:00
    t_morning = week_mon_6 + timedelta(days=3, hours=1)   # 周四 07:00（睡 8 小时）
    f.write_text(json.dumps({
        "version": 1,
        "sessions": {"s1": {"users": {"u1": {
            "name": "旧用户", "last_goodnight": t_night.timestamp(),
            "last_goodmorning": t_morning.timestamp(),
            "last_paired_morning": t_morning.timestamp(),
            "morning_total": 1, "night_total": 1,
            "paired_morning_total": 1, "paired_night_total": 0,
            "sleep_total": 28800.0, "awake_total": 0.0,
            "history": [
                {"t": t_night.timestamp(), "k": "n", "r": 1, "d": None},
                {"t": t_morning.timestamp(), "k": "m", "r": 1, "d": 28800.0},
            ],
        }}}}}
    ), encoding="utf-8")
    p = make_plugin(tmp)
    p._now = lambda: datetime.now(TZ)
    u = p._records["sessions"]["s1"]["users"]["u1"]
    # 两条历史都在本周（周一 6 点之后）-> 回填 1 早安 1 晚安
    assert u["week_morning_total"] == 1, u
    assert u["week_night_total"] == 1, u
    ev = FakeEvent("我的作息", uid="u1")
    ev.unified_msg_origin = "s1"
    ev.message_obj = FakeMessageObj("s1")
    drive(p, ev)
    got = text_of(ev)
    assert "本周早安了1次" in got and "本周晚安了1次" in got, got
    assert "一共睡眠了0天8时0分0秒" in got, got


def test_stats_command(tmp):
    """sleep_stat 查询命令可用。"""
    p = make_plugin(tmp)
    p._now = lambda: T(2026, 8, 19, 23, 0, 0)
    drive(p, FakeEvent("晚安", uid="A"))
    p._now = lambda: T(2026, 8, 20, 7, 0, 0)
    drive(p, FakeEvent("早安", uid="A"))

    ev = FakeEvent("/sleep_stat", uid="A")

    async def run():
        async for _ in p.sleep_stat(ev):
            pass

    asyncio.run(run())
    assert ev.results and "统计" in text_of(ev), "应有统计输出"
    # 未打卡用户
    ev2 = FakeEvent("/sleep_stat", uid="nobody")

    async def run2():
        async for _ in p.sleep_stat(ev2):
            pass

    asyncio.run(run2())
    assert ev2.results and "还没有打过卡" in text_of(ev2)


def test_perf(tmp):
    """性能冒烟：300 用户两轮打卡（含排名统计与落盘）。"""
    p = make_plugin(tmp, {"history_cap": 0})
    umo = "test:GroupMessage:g1"
    t0 = time.time()
    for i in range(300):
        p._process(umo, f"u{i}", f"U{i}", T(2026, 8, 19, 23, 0, i % 60), "night")
    for i in range(300):
        p._process(umo, f"u{i}", f"U{i}", T(2026, 8, 20, 7, 0, 0), "morning")
    cost = time.time() - t0
    assert cost < 10, f"300 用户处理耗时 {cost:.2f}s，过慢"
    users = p._records["sessions"][umo]["users"]
    assert len(users) == 300
    first = users["u0"]
    assert "7h" in json.dumps(first) or first["sleep_total"] > 0


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def main():
    cases = [
        test_sample_scenario,
        test_dup_and_reset,
        test_dup_night_window,
        test_dup_night_window_large_config,
        test_boundaries,
        test_pair_window,
        test_no_false_positive,
        test_variants,
        test_group_isolation,
        test_private_disabled,
        test_self_message_ignored,
        test_persistence,
        test_corrupt_recovery,
        test_history_cap,
        test_fuzzy_mode,
        test_regex_dos_guard,
        test_history_cap_zero_sanitize,
        test_query_group,
        test_query_self,
        test_query_self_week_reset,
        test_query_self_no_record,
        test_query_words_not_checkin,
        test_query_upgrade_backfill,
        test_stats_command,
        test_perf,
    ]
    passed = 0
    failed = 0
    for case in cases:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            install_stubs(tmp)
            # 清除可能残留的 main 模块缓存，保证数据目录指向本次 tmp
            sys.modules.pop("main", None)
            try:
                case(tmp)
                print(f"  [PASS] {case.__name__}")
                passed += 1
            except AssertionError as e:
                print(f"  [FAIL] {case.__name__}: {e}")
                failed += 1
            except Exception as e:  # noqa: BLE001
                print(f"  [ERROR] {case.__name__}: {type(e).__name__}: {e}")
                failed += 1
    print(f"\n结果: {passed} 通过, {failed} 失败")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
