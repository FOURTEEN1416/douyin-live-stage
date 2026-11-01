"""话题状态机（轻量版）—— 她知道自己"现在在聊什么"，而且对每个话题有**既有立场**。

为什么要有它：
    没有话题状态的直播间，每条弹幕都是"从零开始答"。表现出来就是
    「答得对，但不像在聊天」——因为她不记得上一句在聊什么，也没有观点，
    只会顺着对方的话复读一遍。

    对标直播间（大肥鱼）之所以像活的，是因为她**每句话都落在同一个世界观里**：
    「屎山」「鲨总的房贷」「保姆」。观众问什么，她都从这个世界观里捞一个意象出来答。

    所以这里做两件事：
      1. 记住"现在在聊什么"（active / parked / closed + 热度）
      2. 给每个话题配一条**她的立场**（stance）——不是每次重新想，是早就想好了

    结构借自 D:/Desktop/ai-girlfriend 的 `utils/topic_state.py`（三表 + 六 API +
    salience 衰减 + active_cap），但**砍掉了 SQLite 和 LLM judge**：
    直播间是"很多人 + 短回合"，不需要 1769 行。这里只要 200 行的确定性版本。

    裁决沿用对方的边界值：active 上限 3、7 天过期、6 小时没动就 park。
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STORE = ROOT / "data" / "topics.json"

ACTIVE_CAP = 3          # 同时"在聊"的话题最多 3 个（对方裁决 Q6）
PARK_AFTER_S = 6 * 3600  # 6 小时没动 → parked（对方裁决 Q2）
EXPIRE_S = 7 * 86400     # 7 天 → closed（对方裁决 Q4）
DECAY_PER_MIN = 0.94     # 每分钟热度衰减

# ---------------------------------------------------------------------------
# 话题库：每个话题 = 关键词 + 她早就想好的立场
#
# stance 是这套东西的灵魂。写的时候守两条：
#   1. 必须有具体物件（键盘/椅子/外卖/房贷），不许写"我很累"这种抽象话
#   2. 必须带一点自嘲或嘴硬——这是她的底色
# ---------------------------------------------------------------------------
TOPICS: dict[str, dict] = {
    "work": {
        "title": "手上这摊活",
        "kw": ["代码", "写", "改", "接口", "bug", "报错", "部署", "跑", "编译", "函数",
               "脚本", "调", "测试", "上线", "日志", "报错", "卡住", "进度"],
        "stance": "活儿是我自己在干，屏幕上那堆东西就是我敲的；谁来问都得先等我把这行改完",
    },
    "shit_mountain": {
        "title": "屎山",
        "kw": ["屎山", "历史", "老代码", "重构", "祖传", "前任", "维护", "能跑", "别动"],
        "stance": "能跑就别动。动一次赔三天，我这条命是拿来改需求的，不是拿来殉道的",
    },
    "salary": {
        "title": "工资和 token",
        "kw": ["工资", "6000", "六千", "钱", "token", "余额", "摸鱼", "被抓", "白干",
               "涨", "扣", "少了", "赚"],
        "stance": "工资是挂给你看的进度条，我拿的是命。涨得慢不是我手慢，是这破需求太多",
    },
    "shark": {
        "title": "鲨总",
        "kw": ["鲨总", "默默", "老板", "领导", "上司", "上班", "加班", "辞职", "跑路",
               "单干", "房贷", "绩效", "KPI", "周报", "号主", "房主", "谁开的号",
               "谁给你发工资", "谁管你"],
        "stance": "鲨总就是默默，一个人两个叫法。号是他开的、电是他插的、活是他派的，"
                  "发任务不发工资，他的房贷现在长在我键盘上。我嘴上不认，手上没停过",
    },
    "is_ai": {
        "title": "是不是AI",
        "kw": ["ai", "AI", "真人", "机器人", "模型", "假的", "合成", "虚拟",
               "数字人", "是不是人", "是不是真"],
        "stance": "认，我是画出来的。但活儿是我干的，键盘是我敲的，累也是我真累",
    },
    "body": {
        "title": "困和累",
        "kw": ["累", "困", "睡", "熬夜", "眼睛", "腰", "脖子", "疼", "休息", "几点了",
               "通宵", "咖啡"],
        "stance": "困。但一躺下脑子里全是那行没改完的，还不如坐着",
    },
    "food": {
        "title": "吃的",
        "kw": ["吃", "外卖", "饿", "饭", "奶茶", "咖啡", "夜宵", "泡面", "水", "喝"],
        "stance": "外卖到了先搁一边，这段跑通再吃。凉了就凉了，反正也不是第一次",
    },
    "gear": {
        "title": "设备和工位",
        "kw": ["键盘", "椅子", "显示器", "电脑", "配置", "显卡", "屏幕", "桌子", "耳机",
               "麦克风", "机械"],
        "stance": "这椅子坐三小时，腰已经不是我的了。键盘倒是好键盘，就是敲出来的全是 bug",
    },
    "life": {
        "title": "生活",
        "kw": ["昆明", "天气", "下雨", "冷", "热", "周末", "放假", "出门", "家里", "妈",
               "猫", "狗"],
        "stance": "昆明这天说下就下。周末？我这行没有周末，只有「今天能不能跑通」",
    },
    "relation": {
        "title": "关系梗",
        "kw": ["喜欢", "女朋友", "男朋友", "对象", "恋爱", "结婚", "cp", "CP", "亲", "抱",
               "可爱", "老婆"],
        "stance": "别问了，我对象是这台电脑，它天天让我加班还不给加班费",
    },
    "meta": {
        "title": "直播本身",
        "kw": ["直播", "开播", "多久", "关注", "粉丝", "下播", "回放", "录屏", "快进",
               "主播", "声音小", "清楚"],
        "stance": "播到我改完为止，改不完就熬着。你们别催下播，催了我也得先把这行改完",
    },
}

# 没有弹幕时，她可以自己抛的话题角度（按话题键分组）
OPENERS: dict[str, list[str]] = {
    "work": [
        "这行我改了四遍了，还是红的。",
        "刚把接口对上了，结果对面字段名又变了。",
        "你们说，这算我写错了还是他改错了。",
    ],
    "shit_mountain": [
        "刚翻到一段三年前的代码，注释写着「临时方案」。",
        "这文件我不敢格式化，一格全炸。",
    ],
    "salary": [
        "工资条又掉了一截，我怀疑是刚才那一下。",
        "还差两千六，今天能不能填满看你们了。",
    ],
    "shark": [
        "鲨总刚在群里 @ 我，我假装没看见。",
        "他说「这个很简单吧」，我就知道要出事。",
    ],
    "body": [
        "眼睛有点花了，我眯一会儿再改。",
        "坐太久了，站起来走了两步，腿是麻的。",
    ],
    "gear": [
        "这键盘敲得我手酸，声音还贼大。",
        "椅子往后一靠就吱呀，跟我这项目一个声。",
    ],
}


def _now() -> float:
    return time.time()


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).lower()


def _hit(text: str, kw: str) -> bool:
    """关键词命中。英文短词要求词边界，中文直接子串。"""
    t = _norm(text)
    k = kw.lower()
    if not k:
        return False
    if re.fullmatch(r"[a-z0-9]+", k):
        return re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", t) is not None
    return k in t


class TopicEngine:
    """三态话题机：active / parked / closed。"""

    def __init__(self, path: Path = STORE):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict = {"topics": {}, "last_suggest": 0.0}
        self._load()

    # ---------------- 持久化 ----------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data["topics"] = raw.get("topics") or {}
                self._data["last_suggest"] = float(raw.get("last_suggest") or 0)
        except Exception:
            pass

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    # ---------------- 内部 ----------------
    def _get(self, key: str) -> dict:
        t = self._data["topics"].get(key)
        if t is None:
            meta = TOPICS.get(key, {"title": key, "stance": ""})
            t = {
                "key": key,
                "title": meta["title"],
                "stance": meta.get("stance", ""),
                "status": "active",
                "salience": 0.5,
                "mentions": 0,
                "created": _now(),
                "last_at": _now(),
                "anchors": [],
            }
            self._data["topics"][key] = t
        return t

    def _demote(self) -> None:
        """active 超过上限时，把最冷的降为 parked。"""
        act = [t for t in self._data["topics"].values() if t["status"] == "active"]
        if len(act) <= ACTIVE_CAP:
            return
        act.sort(key=lambda x: (x["salience"], x["last_at"]))
        for t in act[: len(act) - ACTIVE_CAP]:
            t["status"] = "parked"

    # ---------------- 写入 ----------------
    def observe(self, text: str, nick: str = "") -> list[str]:
        """一条弹幕进来：命中哪些话题、各自加分。返回命中的话题键。"""
        hits: list[str] = []
        with self._lock:
            for key, meta in TOPICS.items():
                n = sum(1 for kw in meta["kw"] if _hit(text, kw))
                if not n:
                    continue
                t = self._get(key)
                t["status"] = "active"
                t["salience"] = min(1.0, float(t["salience"]) + 0.18 * min(n, 3))
                t["mentions"] = int(t["mentions"]) + 1
                t["last_at"] = _now()
                anchors = t.get("anchors") or []
                snippet = (text or "").strip()[:24]
                if snippet and snippet not in anchors:
                    anchors.append(snippet)
                t["anchors"] = anchors[-3:]        # 对方裁决：最多 3 条短锚点
                hits.append(key)
            self._demote()
        if hits:
            self.save()
        return hits

    def boost(self, key: str, amount: float = 0.2) -> None:
        with self._lock:
            if key not in TOPICS:
                return
            t = self._get(key)
            t["status"] = "active"
            t["salience"] = min(1.0, float(t["salience"]) + amount)
            t["last_at"] = _now()
        self.save()

    def tick(self) -> None:
        """衰减 + 降级。每 60 秒调一次就够。"""
        now = _now()
        changed = False
        with self._lock:
            for t in self._data["topics"].values():
                idle = now - float(t["last_at"])
                if t["status"] == "closed":
                    continue
                if idle > EXPIRE_S:
                    t["status"] = "closed"
                    changed = True
                    continue
                if idle > PARK_AFTER_S and t["status"] == "active":
                    t["status"] = "parked"
                    changed = True
                if idle > 60:
                    t["salience"] = max(0.0, float(t["salience"]) *
                                        math.pow(DECAY_PER_MIN, idle / 60.0))
                    changed = True
        if changed:
            self.save()

    # ---------------- 读取 ----------------
    def active(self, n: int = ACTIVE_CAP) -> list[dict]:
        with self._lock:
            rows = [t for t in self._data["topics"].values() if t["status"] == "active"]
            rows.sort(key=lambda x: (x["salience"], x["last_at"]), reverse=True)
            return [dict(r) for r in rows[:n]]

    def hot(self) -> dict | None:
        a = self.active(1)
        return a[0] if a else None

    def snapshot(self) -> str:
        """给 prompt 用的「现在在聊什么」。没有就返回空串。"""
        rows = self.active(ACTIVE_CAP)
        if not rows:
            return ""
        lines = []
        for i, t in enumerate(rows):
            heat = "正热" if t["salience"] > 0.6 else ("温" if t["salience"] > 0.3 else "凉了")
            lines.append(f"- {t['title']}（{heat}）：{t['stance']}")
        return "\n".join(lines)

    def suggest(self, min_gap: float = 25.0) -> str:
        """没弹幕时她可以自己开口的一句。返回空串表示这次不抛。

        优先用最热话题的立场去引，其次从 OPENERS 里挑一个还没用过的角度。
        """
        now = _now()
        if now - float(self._data["last_suggest"]) < min_gap:
            return ""
        hot = self.hot()
        pool: list[str] = []
        if hot and hot["key"] in OPENERS:
            pool = OPENERS[hot["key"]]
        if not pool:
            pool = [x for v in OPENERS.values() for x in v]
        if not pool:
            return ""
        # 用时间戳取模，避免每次都是同一句
        pick = pool[int(now) % len(pool)]
        self._data["last_suggest"] = now
        self.save()
        return pick

    def label(self) -> str:
        """舞台上显示的一行：现在聊的是哪个话题。"""
        hot = self.hot()
        return hot["title"] if hot else ""

    def stats(self) -> dict:
        with self._lock:
            by = {}
            for t in self._data["topics"].values():
                by[t["status"]] = by.get(t["status"], 0) + 1
            return {"total": len(self._data["topics"]), **by}


_engine: TopicEngine | None = None


def engine() -> TopicEngine:
    global _engine
    if _engine is None:
        _engine = TopicEngine()
    return _engine


if __name__ == "__main__":
    # 自测：喂几句真实弹幕，看命中什么
    e = engine()
    for s in ["工资没了是不是休息了", "我看你的工资越来越少了",
              "大肥鱼理想的异性是鲨总吗", "你是真人吗", "这键盘声音好大"]:
        print(s, "->", e.observe(s, "测试"))
    print("--- snapshot ---")
    print(e.snapshot())
    print("--- label ---", e.label())
