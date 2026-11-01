"""台词质检 —— 把"客服话"挡在门外。

为什么要有它：
    模型不是不会写段子，是**会被上下文带偏**。把最近 8 条"XX 来了，进来坐"
    喂回去，它下一句必然还是"来了，进来坐"——负样本污染。

    光在 prompt 里写"别这样"没用（实测约束力约 50%）。所以这里做一道
    **确定性闸门**：不合格就打回，带着理由重写一次；还不行就用兜底句。

    评分不是玄学，四件事各占一档：
      接得住（复述对方关键词） · 说人话（有具体物件） · 有反转（结尾翻一次）
      · 不重复（不抄上一句、不开门童腔）

    对标依据：D:/Desktop/大肥鱼直播/analysis/bench_dafeiyu/ 里 6 轮问答，
    每条都满足"复述 + 意象 + 反转"三件套。
"""

from __future__ import annotations

import hashlib
import re

# ---------------------------------------------------------------------------
# 禁句式：门童腔。命中开头就拒。
# ---------------------------------------------------------------------------
BAN_OPENERS = (
    "来了", "进来坐", "找个地儿", "找地方坐", "欢迎", "大家好", "哈喽", "hello",
    "你好呀", "又见面", "好久不见", "谢谢关注", "感谢关注", "谢谢你的", "很高兴",
)

# 开头 6 个字以内出现这些，算"没接住话，先招呼"
BAN_ANYWHERE = ("作为一个", "希望对你有帮助", "总之", "首先", "其次", "综上",
                "我是AI", "我是 AI", "语言模型", "很高兴为您", "请问还有什么")

# ---------------------------------------------------------------------------
# 具体意象：她嘴里应该出现"东西"，不是"感受"
# ---------------------------------------------------------------------------
IMAGERY = (
    "键盘", "椅子", "屏幕", "显示器", "外卖", "咖啡", "泡面", "杯子", "水", "桌子",
    "房贷", "工资", "工位", "机房", "服务器", "日志", "报错", "红", "行", "文件", "代码", "字段", "键盘",
    "屎山", "注释", "需求", "接口", "bug", "Bug", "BUG", "版本", "分支", "终端",
    "鲨总", "老板", "群", "消息", "电话", "枕头", "腰", "脖子", "眼睛", "手",
    "猫", "雨", "窗", "楼下", "电梯", "夜", "三点", "外卖", "发票", "报销",
)

# 反转标记：结尾半句翻一次，笑点在这里
REVERSE = ("但", "反正", "算了", "不然", "别问", "倒是", "倒", "结果", "偏偏", "就",
           "才", "还", "只", "也", "其实", "不过", "谁知道", "问题是", "问题在于",
           "可是", "偏偏", "偏")

# 抽象空话：出现越多越像客服
EMPTY = ("努力", "加油", "支持", "理解", "感受", "心情", "状态", "体验", "陪伴",
         "温暖", "治愈", "情绪价值", "正能量")

_QUOTES = "\"'“”‘’「」『』"
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF\uFE0F]")


def sanitize(text: str) -> str:
    """去掉引号、emoji、说话人前缀、括号动作。"""
    t = (text or "").strip()
    t = re.sub(r"^\s*(大肥鱼|我|AI)\s*[:：]\s*", "", t)
    t = re.sub(r"^\s*[-—·]\s*", "", t)
    t = _EMOJI.sub("", t)
    t = t.strip(_QUOTES + " \n\t")
    t = re.sub(r"\s*\n+\s*", " ", t)
    return t.strip()


def _bigrams(s: str) -> set[str]:
    s = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def echo_ratio(reply: str, src: str) -> float:
    """回复里有多少"对方说过的东西"。0~1。"""
    a, b = _bigrams(reply), _bigrams(src)
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(b))


def check(text: str, src: str = "", kind: str = "chat",
          recent: list[str] | None = None) -> tuple[bool, int, list[str]]:
    """返回 (是否通过, 分数 0~100, 打回理由列表)。

    recent：她最近说过的几句，用来抓"换个人名同一句话"的复读。
    """
    t = sanitize(text)
    reasons: list[str] = []
    score = 100

    if not t:
        return False, 0, ["空"]

    # 1) 长度
    lo, hi = (8, 24) if kind == "chat" else (6, 22)
    if len(t) < lo:
        score -= 25
        reasons.append(f"太短（{len(t)}字，要 {lo}-{hi}）")
    elif len(t) > hi:
        score -= 20
        reasons.append(f"太长（{len(t)}字，要 {lo}-{hi}）")

    # 2) 门童腔
    head = t[:8]
    for bad in BAN_OPENERS:
        if head.startswith(bad):
            score -= 60
            reasons.append(f"开头是门童腔「{bad}」")
            break
    for bad in BAN_ANYWHERE:
        if bad in t:
            score -= 40
            reasons.append(f"有 AI 味「{bad}」")
            break

    # 3) 复述：软检查。
    #    只做硬判会误杀——「你在干啥呀」答「改接口呢」是接住了，但没有共同字。
    #    所以只扣 10 分，真正起作用的是 prompt 和重试理由。
    if kind == "chat" and src and len(_bigrams(src)) >= 3:
        if echo_ratio(t, src) < 0.12:
            score -= 10
            reasons.append("没复用对方话里的词（能复述一个更好）")

    # 4) 具体物件：硬一点。没有物件的回答就是"客服"。
    if not _has_object(t):
        score -= 25
        reasons.append("没有具体物件（全是感受词）")

    # 5) 反转：软检查。结尾拐个弯就有笑点。
    tail = t[-12:]
    if not any(w in tail for w in REVERSE) and "？" not in t and "?" not in t:
        score -= 10
        reasons.append("结尾可以再翻一下")

    # 6) 空话
    if sum(1 for w in EMPTY if w in t) >= 2:
        score -= 30
        reasons.append("空话太多")

    # 7) 跟最近说过的话撞车
    if recent:
        for old in recent[:6]:
            if old and _similar(t, old) > 0.6:
                score -= 50
                reasons.append("跟上一条说的几乎一样")
                break

    return score >= 60, max(0, score), reasons


def _has_object(t: str) -> bool:
    """有没有一个能摸到的东西（或一个量词：三小时/第四遍/两杯）。"""
    if any(w in t for w in IMAGERY):
        return True
    return re.search(r"(\d+|[一两二三四五六七八九十])\s*(小时|分钟|秒|遍|行|次|段|杯|把|点|版|块)",
                     t) is not None


def _similar(a: str, b: str) -> float:
    """两句的相似度（按字符二元组交集 / 并集）。"""
    x, y = _bigrams(a), _bigrams(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def repair_hint(reasons: list[str]) -> str:
    """把打回理由翻译成一句给模型看的重写指令。"""
    if not reasons:
        return ""
    return ("上一句不合格：" + "；".join(reasons) +
            "。重写一句：先复用对方话里的一个词，再给一个具体物件，最后半句翻一下。")


# ---------------------------------------------------------------------------
# 招呼语池：member / social 事件不交给模型，直接从池子里挑
#
# 为什么要池子：模型写招呼 90% 会写成"XX来了，进来坐"。池子里每条都是
# 手写过的、带具体动作或物件的一句，且不重复。
# ---------------------------------------------------------------------------
GREET_NEW = [
    "别客气，随便找块地儿站，我这儿椅子只有一把。",
    "新面孔。小心点，屏幕上全是红的，别被吓着。",
    "来得巧，我正卡在一行日志上，你陪我想想。",
    "先别说话，听一下这风扇声，它在替我喊累。",
    "站那儿就行，地上有线，别绊着。",
    "刚想找个人说句话，你就进来了。",
    "来得正好，帮我看看这报错像不像人话。",
    "别看我屏幕，看我也行，反正都挺乱的。",
    "椅子给你，我站着改，反正也坐麻了。",
    "你来得不是时候——我刚好在删一段不该删的。",
]

GREET_BACK = [
    "又是你。上次那问题我还没想明白。",
    "你回来得挺快，我这行还没改完。",
    "老位置，别踩线。今天比昨天还乱。",
    "你上次问的那个，我后来试了，不行。",
    "来得挺准，我正好想喝口水。",
    "又是这个点。你也不睡？",
    "你那条弹幕我记着呢，一会儿答。",
    "回来啦。我刚把键盘敲出火星子。",
]

GIFT_LINES = [
    "这钱我收下，回头给你记在工位墙上。",
    "谢了。等这行跑通，我给你念一遍日志当回礼。",
    "别乱花，我这儿的进度条涨得比工资快。",
    "收到。这杯咖啡算你请的。",
    "你这手笔，够我多熬半小时。",
]

SOCIAL_LINES = [
    "点关注了？行，那以后我卡住就赖你。",
    "记住了。下次开播我第一个喊你。",
    "关注收到。别指望我天天准时，我这活儿不由我。",
]


def _pick(pool: list[str], seed: str) -> str:
    """按种子挑一条：同一个昵称每次不同，但同一秒内稳定。"""
    h = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:8], 16)
    return pool[h % len(pool)]


def greeting(nick: str, returning: bool = False, salt: str = "") -> str:
    pool = GREET_BACK if returning else GREET_NEW
    return _pick(pool, f"{nick}|{salt}|{'b' if returning else 'n'}")


def gift_line(nick: str, salt: str = "") -> str:
    return _pick(GIFT_LINES, f"{nick}|{salt}|g")


def social_line(nick: str, salt: str = "") -> str:
    return _pick(SOCIAL_LINES, f"{nick}|{salt}|s")


# ---------------------------------------------------------------------------
# 自测
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cases = [
        ("哟，序七，刚从哪儿溜达过来。", "你在干啥呀", "chat"),
        ("来了，进来坐。", "你在干啥呀", "chat"),
        ("工资没了？那是我刚把一段跑通的代码删了，赔的。", "工资没了是不是休息了", "chat"),
        ("这山的 bug 只认我这双手。", "怎么不找外包来干", "chat"),
        ("我很理解你的感受，加油哦。", "我好累", "chat"),
        ("键盘敲得手酸，反正也没人替我。", "你手酸不酸", "chat"),
        ("别客气，随便找块地儿站，我这儿椅子只有一把。", "", "member"),
    ]
    for txt, src, kind in cases:
        ok, sc, rs = check(txt, src, kind)
        print(f"{'PASS' if ok else 'FAIL'} {sc:3d}  {txt}")
        if rs:
            print("      ", "；".join(rs))
    print("\n招呼池抽样：")
    for n in ("麟迟", "麟迟", "十四", "GUte1"):
        print("  ", greeting(n, returning=False, salt="t1"))
