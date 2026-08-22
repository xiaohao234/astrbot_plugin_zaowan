"""
早晚安打卡插件 (astrbot_plugin_zaowan)
=====================================

功能概述
--------
- 监听群消息（无需 @ 机器人），识别「早安 / 晚安」及类似用语；
- 记录每个用户的早安 / 晚安时间，自动计算：
  * 睡眠时长 = 上一次晚安 -> 本次早安
  * 清醒时长 = 今日早安 -> 本次晚安
- 早安有效时间：06:00:00 - 11:59:59（6 时到 12 时）
- 晚安有效时间：21:00:00 - 次日 05:59:59（21 时到第二天早上 6 时）
- 排名（今早第 n 个起床 / 今晚第 n 个睡觉）以每天 06:00 为界自动清零。
- 重复打卡：距上次成功晚安不足 6 小时（可配置）回复「N小时内你已经晚安过了哦~」，
  超过 6 小时且仍在晚安时段内则视为一次新的晚安；同一天重复早安静默不理会。

可靠性设计（详见 README.md「安全与健壮性审计」）
--------
1. 数据不丢失：每次状态变更后立即「临时文件 + fsync + os.replace」原子落盘，
   进程被 kill / 定期重启 AstrBot 均不会丢失或写坏数据；
   数据存于 AstrBot 规范目录 data/plugin_data/<插件名>/，更新插件不会覆盖数据。
2. 无内存泄漏：不创建任何后台线程 / 定时器（每日清零通过时间戳比较实现）；
   正则在初始化时仅编译一次；每用户历史记录封顶；无全局缓存无限增长。
3. 并发安全：所有「读取-修改-写入」均在同一把可重入锁内完成。
4. 输入防御：落盘字段全部做类型 / 长度清洗，损坏文件自动备份后重建，绝不崩溃。
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # Python 3.9+ 标准库（Debian 12 自带 Python 3.11）
    from zoneinfo import ZoneInfo
    _HAS_ZONEINFO = True
except ImportError:  # pragma: no cover
    _HAS_ZONEINFO = False

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, register

try:  # 兼容极老版本 AstrBot 无 StarTools 的情况
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover
    StarTools = None  # type: ignore


PLUGIN_NAME = "astrbot_plugin_zaowan"
DATA_FILE_NAME = "records.json"
DATA_VERSION = 1

# ---------- 防御性上限：任何异常输入都不允许让内存无界膨胀 ----------
MAX_SESSION_ID_LEN = 256   # 会话 ID（unified_msg_origin）最大长度
MAX_USER_ID_LEN = 64       # 用户 ID 最大长度
MAX_NAME_LEN = 64          # 昵称最大长度
MAX_SESSIONS = 100_000     # 会话数量上限
MAX_USERS_PER_SESSION = 100_000  # 单会话用户数上限
MAX_WORD_LEN = 24          # 单个触发词最大长度
MAX_WORDS = 200            # 触发词数量上限

DEFAULT_MORNING_WORDS = (
    "早安,早,早上好,早好,起床,起床了,起床啦,起床咯,醒了,我醒了,"
    "good morning,gm,morning"
)
DEFAULT_NIGHT_WORDS = (
    "晚安,睡觉,睡觉了,睡觉啦,睡觉咯,睡了,去睡了,去睡觉了,去睡觉啦,"
    "我睡了,我去睡了,困死了,睡了睡了,good night,goodnight,gn,night night"
)
# 作息查询触发词（整句匹配，只读不打卡）
DEFAULT_GROUP_QUERY_WORDS = "群友作息"
DEFAULT_SELF_QUERY_WORDS = "我的作息"

# 问候语前缀（可选）/ 语气词（可重复出现）：
#   「大家早安呀~」「各位晚安啦」「我醒了」等均可命中
_PERSON = r"(?:大家|各位|群友(?:们)?|小伙伴(?:们)?|兄弟(?:们)?|集美(?:们)?|宝子(?:们)?|友友(?:们)?|俺|我)?"
_PARTICLE = r"[呀哈啊阿啦呗咯吧呢哦噢哒嘿~～!！?？。．.…，,、\s]"


def _to_float(v: Any, default: float = 0.0) -> float:
    """安全的 float 转换，任何异常输入都回退为默认值。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):  # NaN / Inf 防御
        return default
    return f


@register(
    PLUGIN_NAME,
    "xiaohao234",
    "早晚安打卡：记录早晚安时间与睡眠/清醒时长，每日排名，无需@机器人",
    "1.0.0",
)
class ZaowanPlugin(Star):
    """早晚安打卡插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        cfg: dict[str, Any] = config if isinstance(config, dict) else {}

        self._tz = self._load_timezone(str(cfg.get("timezone", "") or "Asia/Shanghai"))
        self._morning_words = self._split_words(cfg.get("morning_words"), DEFAULT_MORNING_WORDS)
        self._night_words = self._split_words(cfg.get("night_words"), DEFAULT_NIGHT_WORDS)
        self._group_query_words = self._split_words(cfg.get("group_query_words"), DEFAULT_GROUP_QUERY_WORDS)
        self._self_query_words = self._split_words(cfg.get("self_query_words"), DEFAULT_SELF_QUERY_WORDS)
        # 正则仅在初始化时编译一次，之后所有消息复用，避免反复编译造成开销
        self._exact_morning = self._compile_exact(self._morning_words)
        self._exact_night = self._compile_exact(self._night_words)
        self._exact_group_query = self._compile_query(self._group_query_words)
        self._exact_self_query = self._compile_query(self._self_query_words)

        self._fuzzy = bool(cfg.get("fuzzy_match", False))
        self._enable_private = bool(cfg.get("enable_private", False))
        self._pair_window = self._cfg_int(cfg, "pair_window_hours", 24, 1, 168) * 3600
        # 重复晚安判定窗口：距上次成功晚安不足该时长 -> 回复“N小时内你已经晚安过了哦~”；
        # 超过该时长且仍在晚安时段内，视为一次新的晚安（半夜醒后再睡场景）
        self._dup_night_window = self._cfg_int(cfg, "dup_night_hours", 6, 0, 24) * 3600
        self._history_cap = self._cfg_int(cfg, "history_cap", 30, 0, 500)
        self._prune_days = self._cfg_int(cfg, "prune_days", 0, 0, 3650)

        # 单实例单锁：所有状态读写 / 文件落盘共用，杜绝并发竞争
        self._lock = threading.RLock()
        self._data_file = self._resolve_data_file()
        self._records = self._load_records()
        self._prune_if_needed()

        logger.info(
            "[%s] 加载完成: 时区=%s 早安词=%d个 晚安词=%d个 数据文件=%s",
            PLUGIN_NAME, self._tz, len(self._morning_words), len(self._night_words), self._data_file,
        )

    # ------------------------------------------------------------------
    # 配置解析辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _cfg_int(cfg: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
        """读取 int 配置并夹紧到 [lo, hi]，非法输入回退默认值（防止 WebUI 填坏导致崩溃）。"""
        try:
            v = int(cfg.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    @staticmethod
    def _load_timezone(name: str):
        """加载 IANA 时区；系统缺 tzdata 或名称非法时回退 UTC+8（QQ 场景默认）。"""
        name = (name or "Asia/Shanghai").strip()
        if _HAS_ZONEINFO:
            try:
                return ZoneInfo(name)
            except Exception:  # noqa: BLE001 框架边界防御：任何时区错误都必须回退而不是崩溃
                logger.warning("[%s] 时区 %s 不可用（缺 tzdata？），回退 UTC+8", PLUGIN_NAME, name)
        return timezone(timedelta(hours=8))

    @staticmethod
    def _split_words(raw: Any, default: str) -> list[str]:
        """解析触发词配置（支持逗号/中文逗号/换行分隔或列表），去重、限长、限量。"""
        if isinstance(raw, (list, tuple)):
            items = [str(x) for x in raw]
        else:
            items = re.split(r"[,，\n;；]+", str(raw or ""))
        words: list[str] = []
        seen = set()
        for w in (x.strip() for x in items):
            w = w[:MAX_WORD_LEN]
            key = w.lower()
            if w and key not in seen:
                seen.add(key)
                words.append(w)
            if len(words) >= MAX_WORDS:
                break
        if not words:
            words = [x.strip() for x in default.split(",") if x.strip()]
        # 长词优先，保证「早上好」优先于「早」被完整匹配
        words.sort(key=len, reverse=True)
        return words

    @staticmethod
    def _compile_exact(words: list[str]) -> re.Pattern:
        """把整组触发词编译成一个锚定正则：^称呼? 触发词 语气词* 称呼? 语气词* $"""
        alt = "|".join(re.escape(w) for w in words)
        return re.compile(
            rf"^{_PERSON}\s*(?:{alt})(?:{_PARTICLE})*(?:{_PERSON})?(?:{_PARTICLE})*$",
            re.IGNORECASE,
        )

    @staticmethod
    def _compile_query(words: list[str]) -> re.Pattern:
        """把查询触发词编译成锚定正则：^触发词 语气词* $（不含称呼前缀，避免歧义）"""
        alt = "|".join(re.escape(w) for w in words)
        return re.compile(rf"^(?:{alt})(?:{_PARTICLE})*$", re.IGNORECASE)

    # ------------------------------------------------------------------
    # 时间窗口（「天」以 06:00 为界）
    # ------------------------------------------------------------------
    def _now(self) -> datetime:
        return datetime.now(self._tz)

    @staticmethod
    def _day_start(now: datetime) -> datetime:
        """当前「打卡日」的 06:00。凌晨 0-6 点属于前一天，保证每晚排名在 6 点整自然清零。"""
        start = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now < start:
            start -= timedelta(days=1)
        return start

    @staticmethod
    def _week_start(now: datetime) -> datetime:
        """本周第一个打卡日（周一）的 06:00。周界与打卡日界一致：
        周日 21 点后的晚安属于本周，周一凌晨 0-6 点的晚安属于上一周。"""
        day = now.replace(hour=6, minute=0, second=0, microsecond=0)
        if now < day:
            day -= timedelta(days=1)
        return day - timedelta(days=day.weekday())

    @staticmethod
    def _is_morning_time(now: datetime) -> bool:
        """早安有效：6 时到 12 时（含 6:00:00，不含 12:00:00）。"""
        return 6 <= now.hour < 12

    @staticmethod
    def _is_night_time(now: datetime) -> bool:
        """晚安有效：21 时到第二天早上 6 时（含 21:00:00 与凌晨，不含 6:00:00）。"""
        return now.hour >= 21 or now.hour < 6

    @staticmethod
    def _fmt_dur(seconds: float) -> str:
        """时长格式化：7h10m19s（小时不补零，分秒补零）。"""
        s = max(0, round(seconds))
        h, rest = divmod(s, 3600)
        m, sec = divmod(rest, 60)
        return f"{h}h{m:02d}m{sec:02d}s"

    @staticmethod
    def _fmt_dur_cn(seconds: float) -> str:
        """时长中文格式化：0天17时14分18秒（作息查询用）。"""
        s = max(0, round(seconds))
        d, rest = divmod(s, 86400)
        h, rest = divmod(rest, 3600)
        m, sec = divmod(rest, 60)
        return f"{d}天{h}时{m}分{sec}秒"

    # ------------------------------------------------------------------
    # 消息匹配
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(text: str) -> str:
        """消息纯文本清洗：去表情占位、@ 文本、零宽字符，统一小写与空白。"""
        t = str(text or "")
        t = re.sub(r"\[.+?\]|\(.+?\)|（.+?）", "", t)     # [表情] /（动作）等占位
        t = re.sub(r"@[0-9A-Za-z_\-\u4e00-\u9fff]+", "", t)  # 残留的 @ 文本
        t = re.sub(r"[\u200b\u200c\u200d\ufeff\s]+", " ", t)  # 零宽字符/连续空白
        return t.strip().lower()

    def _match(self, text: str) -> str | None:
        """返回 'morning' / 'night' / 'group_query' / 'self_query' / None。"""
        t = self._normalize(text)
        # 长度上限：正常问候语远短于 64 字符；超长消息直接跳过，
        # 防止恶意长文本（如几千个语气词）触发正则回溯造成 CPU 尖峰
        if not t or t.startswith("/") or len(t) > 64:
            return None
        if self._exact_group_query.match(t):
            return "group_query"
        if self._exact_self_query.match(t):
            return "self_query"
        if self._exact_morning.match(t):
            return "morning"
        if self._exact_night.match(t):
            return "night"
        if self._fuzzy:
            # 模糊模式：消息中“包含”触发词即触发（默认关闭，避免“早饭”等误触）
            for w in self._night_words:
                if len(w) >= 2 and (w.isascii() and re.search(rf"\b{re.escape(w)}\b", t) or (not w.isascii() and w in t)):
                    return "night"
            for w in self._morning_words:
                if len(w) >= 2 and (w.isascii() and re.search(rf"\b{re.escape(w)}\b", t) or (not w.isascii() and w in t)):
                    return "morning"
        return None

    # ------------------------------------------------------------------
    # 数据持久化（原子写 + 损坏自愈）
    # ------------------------------------------------------------------
    def _resolve_data_file(self) -> Path:
        """按 AstrBot 规范取数据目录 data/plugin_data/<插件名>/records.json。"""
        if StarTools is not None:
            try:
                return Path(StarTools.get_data_dir(PLUGIN_NAME)) / DATA_FILE_NAME
            except Exception:  # noqa: BLE001 兼容各版本框架，任何失败都走本地回退路径
                logger.warning("[%s] StarTools.get_data_dir 不可用，回退相对路径", PLUGIN_NAME)
        d = Path("data") / "plugin_data" / PLUGIN_NAME
        d.mkdir(parents=True, exist_ok=True)
        return d / DATA_FILE_NAME

    def _load_records(self) -> dict[str, Any]:
        """启动时加载记录：损坏则备份重建，字段逐项清洗，保证内存结构永远合法。"""
        f = self._data_file
        if not f.exists():
            return {"version": DATA_VERSION, "sessions": {}}
        try:
            with open(f, "r", encoding="utf-8") as fp:
                raw = json.load(fp)
            if not isinstance(raw, dict) or not isinstance(raw.get("sessions"), dict):
                raise TypeError("records 顶层结构不合法")
        except (json.JSONDecodeError, TypeError, ValueError, OSError, UnicodeDecodeError) as e:
            # 关键：损坏文件先改名备份（绝不直接覆盖，防数据丢失），再从空状态开始
            try:
                stamp = self._now().strftime("%Y%m%d%H%M%S")
                bak = f.parent / f"{f.name}.corrupt-{stamp}"
                os.replace(f, bak)
                logger.error("[%s] 记录文件损坏(%s)，已备份到 %s", PLUGIN_NAME, e, bak)
            except OSError:
                logger.error("[%s] 记录文件损坏(%s)且备份失败，将重建", PLUGIN_NAME, e)
            return {"version": DATA_VERSION, "sessions": {}}
        return self._sanitize(raw)

    def _sanitize(self, raw: dict[str, Any]) -> dict[str, Any]:
        """逐字段类型/长度清洗，任何被篡改或异常的历史数据都不会进入内存。"""
        out: dict[str, Any] = {"version": DATA_VERSION, "sessions": {}}
        sessions = raw.get("sessions") or {}
        for sid, sv in list(sessions.items())[:MAX_SESSIONS]:
            if not isinstance(sid, str) or not isinstance(sv, dict):
                continue
            users_in = sv.get("users")
            if not isinstance(users_in, dict):
                users_in = {}
            users_out: dict[str, Any] = {}
            for uid, uv in list(users_in.items())[:MAX_USERS_PER_SESSION]:
                if not isinstance(uid, str) or not isinstance(uv, dict):
                    continue
                hist_in = uv.get("history")
                hist_out = []
                if isinstance(hist_in, list) and self._history_cap > 0:
                    # 注意 Python 切片 [ -0: ] 会取全部，cap=0 必须显式置空
                    for h in hist_in[-(self._history_cap * 2):]:
                        if isinstance(h, dict):
                            hist_out.append({
                                "t": _to_float(h.get("t")),
                                "k": "n" if h.get("k") == "n" else "m",
                                "r": int(_to_float(h.get("r"), 0)),
                                "d": None if h.get("d") is None else _to_float(h.get("d")),
                            })
                # 周计数字段；旧版本数据（无 week_start 键）升级时尽力从保留的历史回填本周计数
                week_ws = _to_float(uv.get("week_start"))
                week_m = int(_to_float(uv.get("week_morning_total")))
                week_n = int(_to_float(uv.get("week_night_total")))
                if "week_start" not in uv and hist_out:
                    ws_now = self._week_start(self._now()).timestamp()
                    # 回填的计数以本周为界，week_start 必须同步指向本周，
                    # 否则查询/下次打卡会误判为上周数据而归零
                    week_ws = ws_now
                    for h in hist_out:
                        if h["t"] >= ws_now:
                            if h["k"] == "m":
                                week_m += 1
                            else:
                                week_n += 1
                users_out[uid[:MAX_USER_ID_LEN]] = {
                    "name": str(uv.get("name") or "")[:MAX_NAME_LEN],
                    "last_goodnight": _to_float(uv.get("last_goodnight")),
                    "last_goodmorning": _to_float(uv.get("last_goodmorning")),
                    "last_paired_morning": _to_float(uv.get("last_paired_morning")),
                    "morning_total": int(_to_float(uv.get("morning_total"))),
                    "night_total": int(_to_float(uv.get("night_total"))),
                    "paired_morning_total": int(_to_float(uv.get("paired_morning_total"))),
                    "paired_night_total": int(_to_float(uv.get("paired_night_total"))),
                    "sleep_total": _to_float(uv.get("sleep_total")),
                    "awake_total": _to_float(uv.get("awake_total")),
                    "history": hist_out,
                    "week_start": week_ws,
                    "week_morning_total": week_m,
                    "week_night_total": week_n,
                }
            out["sessions"][sid[:MAX_SESSION_ID_LEN]] = {"users": users_out}
        return out

    def _save_locked(self) -> None:
        """原子落盘（调用方必须已持锁）：写临时文件 -> fsync -> os.replace。"""
        tmp = self._data_file.parent / (self._data_file.name + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self._records, fp, ensure_ascii=False, separators=(",", ":"))
                fp.flush()
                os.fsync(fp.fileno())
            os.replace(tmp, self._data_file)  # 同目录 rename，POSIX 原子语义
        except OSError as e:
            # 写盘失败：内存状态仍正确，下次成功写入时会一并落盘，不丢数据
            logger.error("[%s] 保存记录失败(将在下次打卡时重试): %s", PLUGIN_NAME, e)
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    def _prune_if_needed(self) -> None:
        """可选清理长期不活跃用户（默认 0=永不清理，确保数据不丢）。仅在启动时执行一次。"""
        if self._prune_days <= 0:
            return
        cutoff = datetime.now(self._tz).timestamp() - self._prune_days * 86400
        with self._lock:
            changed = False
            for sid in list(self._records["sessions"].keys()):
                users = self._records["sessions"][sid]["users"]
                for uid in list(users.keys()):
                    u = users[uid]
                    last = max(
                        _to_float(u.get("last_goodnight")),
                        _to_float(u.get("last_goodmorning")),
                    )
                    if last < cutoff:
                        del users[uid]
                        changed = True
                if not users:
                    del self._records["sessions"][sid]
            if changed:
                self._save_locked()

    @staticmethod
    def _new_user(name: str) -> dict[str, Any]:
        return {
            "name": name[:MAX_NAME_LEN], "last_goodnight": 0.0, "last_goodmorning": 0.0,
            "last_paired_morning": 0.0, "morning_total": 0, "night_total": 0,
            "paired_morning_total": 0, "paired_night_total": 0,
            "sleep_total": 0.0, "awake_total": 0.0, "history": [],
            "week_start": 0.0, "week_morning_total": 0, "week_night_total": 0,
        }

    @staticmethod
    def _count_since(users: dict[str, Any], field: str, start_ts: float, now_ts: float) -> int:
        """统计本打卡日内某字段落在 [start, now] 的用户数（即当前排名，含自身）。"""
        n = 0
        for v in users.values():
            t = _to_float(v.get(field))
            if start_ts <= t <= now_ts:
                n += 1
        return n

    def _append_history(self, u: dict[str, Any], ts: float, kind: str,
                        rank: int | None, dur: float | None) -> None:
        """追加打卡历史并封顶截断，内存占用有上界。"""
        h = u.setdefault("history", [])
        h.append({"t": ts, "k": kind, "r": rank or 0, "d": dur})
        cap = self._history_cap
        if cap > 0 and len(h) > cap:
            del h[: len(h) - cap]
        elif cap == 0:
            h.clear()

    # ------------------------------------------------------------------
    # 核心打卡逻辑（纯同步 + 全程持锁，可被单测完整覆盖）
    # ------------------------------------------------------------------
    def _process(self, session_id: str, user_id: str, name: str,
                 now: datetime, kind: str) -> str | None:
        """处理一次打卡，返回回复文本（None 表示不回复）。"""
        with self._lock:
            sessions = self._records["sessions"]
            sess = sessions.get(session_id)
            if sess is None:
                sess = sessions[session_id[:MAX_SESSION_ID_LEN]] = {"users": {}}
            users: dict[str, Any] = sess["users"]
            u = users.get(user_id)
            if u is None:
                u = users[user_id[:MAX_USER_ID_LEN]] = self._new_user(name)
            if u.get("name") != name:
                u["name"] = name[:MAX_NAME_LEN]  # 昵称有变化时顺带更新

            now_ts = now.timestamp()
            start_ts = self._day_start(now).timestamp()
            # 周计数维护：跨周后第一次打卡时重置（时间戳比较实现，无后台任务）
            ws_ts = self._week_start(now).timestamp()
            if _to_float(u.get("week_start")) != ws_ts:
                u["week_start"] = ws_ts
                u["week_morning_total"] = 0
                u["week_night_total"] = 0

            if kind == "morning":
                if not self._is_morning_time(now):
                    return "现在不能早安哦，可以早安的时间为6时到12时~"
                if _to_float(u.get("last_goodmorning")) >= start_ts:
                    return None  # 重复早安：静默不理会（需求规定不回复）
                gn = _to_float(u.get("last_goodnight"))
                # 仅当最近一次晚安在配对窗口内（默认 24h，即“昨晚”）才计算睡眠时长
                paired = 0 < gn <= now_ts and (now_ts - gn) <= self._pair_window
                u["last_goodmorning"] = now_ts
                u["morning_total"] = int(_to_float(u.get("morning_total"))) + 1
                u["week_morning_total"] = int(_to_float(u.get("week_morning_total"))) + 1
                if paired:
                    dur = now_ts - gn
                    u["last_paired_morning"] = now_ts
                    u["paired_morning_total"] = int(_to_float(u.get("paired_morning_total"))) + 1
                    u["sleep_total"] = _to_float(u.get("sleep_total")) + dur
                    # 先写入自身再计数：排名含自身，且只统计“成功起床”（有睡眠时长）的用户
                    rank = self._count_since(users, "last_paired_morning", start_ts, now_ts)
                    self._append_history(u, now_ts, "m", rank, dur)
                    self._save_locked()
                    return (
                        f"早安成功！你的睡眠时长为{self._fmt_dur(dur)}，\n"
                        f"你是今早第{rank}个起床的群友！"
                    )
                self._append_history(u, now_ts, "m", None, None)
                self._save_locked()
                return "早安～"

            # ---- 晚安 ----
            if not self._is_night_time(now):
                return "现在不能晚安哦，可以晚安的时间为21时到第二天早上6时~"
            last_gn = _to_float(u.get("last_goodnight"))
            # 重复晚安：同一打卡日内、距上次成功晚安不足窗口时长（默认 6 小时）时提示，不记录不落盘。
            # “同一天”条件保证即使把窗口配置得很大，也绝不会把第二天晚上的新晚安误判为重复
            # （合法晚安时段内同晚两次晚安最大间隔 < 9h、跨晚最小间隔 > 15h）。
            if last_gn >= start_ts and (now_ts - last_gn) < self._dup_night_window:
                return f"{self._dup_night_window // 3600}小时内你已经晚安过了哦~"
            gm = _to_float(u.get("last_goodmorning"))
            # 今日（本打卡日 6 点后）有早安记录才计算清醒时长
            paired = start_ts <= gm <= now_ts
            u["last_goodnight"] = now_ts
            u["night_total"] = int(_to_float(u.get("night_total"))) + 1
            u["week_night_total"] = int(_to_float(u.get("week_night_total"))) + 1
            rank = self._count_since(users, "last_goodnight", start_ts, now_ts)
            if paired:
                dur = now_ts - gm
                u["paired_night_total"] = int(_to_float(u.get("paired_night_total"))) + 1
                u["awake_total"] = _to_float(u.get("awake_total")) + dur
                self._append_history(u, now_ts, "n", rank, dur)
                self._save_locked()
                return (
                    f"晚安成功！你今天的清醒时长为{self._fmt_dur(dur)}，\n"
                    f"你是今晚第{rank}个睡觉的群友！"
                )
            self._append_history(u, now_ts, "n", rank, None)
            self._save_locked()
            return f"晚安成功！你是今晚第{rank}个睡觉的群友！"

    # ------------------------------------------------------------------
    # 作息查询（只读操作，不建档不落盘）
    # ------------------------------------------------------------------
    def _query_group(self, session_id: str, now: datetime) -> str:
        """群作息：今日（本打卡日）早晚安人数。"""
        with self._lock:
            users: dict[str, Any] = self._records["sessions"].get(session_id, {}).get("users", {})
            start_ts = self._day_start(now).timestamp()
            now_ts = now.timestamp()
            m = self._count_since(users, "last_goodmorning", start_ts, now_ts)
            n = self._count_since(users, "last_goodnight", start_ts, now_ts)
            return f"今天已经有{m}位群友早安了，{n}位群友晚安了~"

    def _query_self(self, session_id: str, user_id: str, now: datetime) -> str:
        """个人作息：最近早晚安时间、本周/累计次数、累计睡眠时长。"""
        with self._lock:
            users: dict[str, Any] = self._records["sessions"].get(session_id, {}).get("users", {})
            u = users.get(user_id)
            if u is None:
                return (
                    "你的作息数据如下：\n"
                    "最近一次早安时间为暂无记录\n"
                    "最近一次晚安时间为暂无记录\n"
                    "本周早安了0次\n"
                    "本周晚安了0次\n"
                    "一共早安了0次\n"
                    "一共晚安了0次\n"
                    "一共睡眠了0天0时0分0秒"
                )
            ws_ts = self._week_start(now).timestamp()
            # 跨周后尚未打卡时，存储的周计数属于上一周，展示值动态归零
            if _to_float(u.get("week_start")) == ws_ts:
                week_m = int(_to_float(u.get("week_morning_total")))
                week_n = int(_to_float(u.get("week_night_total")))
            else:
                week_m = week_n = 0
            gm = _to_float(u.get("last_goodmorning"))
            gn = _to_float(u.get("last_goodnight"))
            return "\n".join([
                "你的作息数据如下：",
                "最近一次早安时间为" + (self._ts_str(gm) if gm > 0 else "暂无记录"),
                "最近一次晚安时间为" + (self._ts_str(gn) if gn > 0 else "暂无记录"),
                f"本周早安了{week_m}次",
                f"本周晚安了{week_n}次",
                f"一共早安了{int(_to_float(u.get('morning_total')))}次",
                f"一共晚安了{int(_to_float(u.get('night_total')))}次",
                "一共睡眠了" + self._fmt_dur_cn(_to_float(u.get("sleep_total"))),
            ])

    # ------------------------------------------------------------------
    # 事件处理器
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """早晚安监听：识别群消息中的早安/晚安用语（无需@机器人）"""
        try:
            umo = getattr(event, "unified_msg_origin", "")
            if not umo:
                return
            # 防自触发：跳过机器人自身发出的消息
            try:
                self_id = str(getattr(event.message_obj, "self_id", "") or "")
                sender = str(event.get_sender_id() or "")
                if not sender or (self_id and sender == self_id):
                    return
            except Exception:  # noqa: BLE001 事件对象结构异常时宁可漏处理也不崩溃
                return
            # 默认仅群聊生效（可配置开启私聊）
            if not self._enable_private:
                try:
                    if not getattr(event.message_obj, "group_id", ""):
                        return
                except Exception:  # noqa: BLE001
                    return

            kind = self._match(getattr(event, "message_str", "") or "")
            if kind is None:
                return  # 非早晚安消息：不做任何事，开销极小

            sender_id = str(event.get_sender_id() or "unknown")
            sender_name = str(event.get_sender_name() or sender_id)
            if kind == "group_query":
                # 群作息查询：纯文本回复，不 @ 用户
                yield event.plain_result(self._query_group(umo, self._now()))
                return
            if kind == "self_query":
                # 个人作息查询：@ 用户 + 数据
                text = self._query_self(umo, sender_id, self._now())
                yield event.chain_result([At(qq=sender_id), Plain("\u200b " + text)])
                return
            reply = self._process(umo, sender_id, sender_name, self._now(), kind)
            if reply:
                # \u200b：aiocqhttp 平台会 strip 消息段首尾空白，零宽字符可保住 @ 后的空格
                yield event.chain_result([At(qq=sender_id), Plain("\u200b " + reply)])
        except Exception as e:  # noqa: BLE001 顶层兜底：任何异常只记录日志，绝不让插件崩掉
            logger.error("[%s] 处理消息异常: %s", PLUGIN_NAME, e)

    @filter.command("sleep_stat", alias={"早晚安统计"})
    async def sleep_stat(self, event: AstrMessageEvent):
        """查询我在本群的早晚安打卡统计（只读操作）"""
        try:
            umo = getattr(event, "unified_msg_origin", "")
            uid = str(event.get_sender_id() or "")
            if not umo or not uid:
                return
            with self._lock:
                u = self._records["sessions"].get(umo, {}).get("users", {}).get(uid)
                if u is None:
                    yield event.plain_result("你还没有打过卡哦，发「早安」或「晚安」试试～")
                    return
                lines = [
                    "【早晚安打卡统计】",
                    (f"早安次数: {int(_to_float(u.get('morning_total')))}"
                     f"（成功配对 {int(_to_float(u.get('paired_morning_total')))}）"),
                    (f"晚安次数: {int(_to_float(u.get('night_total')))}"
                     f"（成功配对 {int(_to_float(u.get('paired_night_total')))}）"),
                ]
                gn = _to_float(u.get("last_goodnight"))
                gm = _to_float(u.get("last_goodmorning"))
                if gm > 0:
                    lines.append("最近早安: " + self._ts_str(gm))
                if gn > 0:
                    lines.append("最近晚安: " + self._ts_str(gn))
                pc = int(_to_float(u.get("paired_morning_total")))
                nc = int(_to_float(u.get("paired_night_total")))
                if pc > 0:
                    lines.append("平均睡眠: " + self._fmt_dur(_to_float(u.get("sleep_total")) / pc))
                if nc > 0:
                    lines.append("平均清醒: " + self._fmt_dur(_to_float(u.get("awake_total")) / nc))
            yield event.plain_result("\n".join(lines))
        except Exception as e:  # noqa: BLE001 查询为只读操作，兜底保证不崩溃
            logger.error("[%s] 查询统计异常: %s", PLUGIN_NAME, e)
            yield event.plain_result("查询失败，请查看日志。")

    def _ts_str(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, self._tz).strftime("%Y-%m-%d %H:%M:%S")

    async def terminate(self):
        """插件卸载/停用时调用：最终落盘一次，双保险。"""
        with self._lock:
            self._save_locked()
        logger.info("[%s] 已卸载，数据已落盘", PLUGIN_NAME)
