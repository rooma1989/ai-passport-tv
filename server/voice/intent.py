"""语音指令 → 决定。纯函数，不碰网络、不碰套接字，单测覆盖（tests/test_voice_intent.py）。

规则优先：先认固定说法（音量、亮度、暂停、配网……），再认「X 第 N 集」，最后把剩下的话当成
频道名做精确、别名、编号、分组、模糊匹配。设计见
docs/superpowers/specs/2026-10-12-voice-control-design.md 第 3 节。

结果分三类，由 session.py 发给设备或交给中转：
- 设备自己做的：`do` 里是 AV_SWITCH 认识的键（channel、volume、brightness、audio_only）
  外加新的 page、setup；
- 几个候选让用户选：`choices`，每项带 `do`，或带 `ref`（选中后要服务端调中转）；
- 中转做的（选集、暂停、继续、跳转、上一集、下一集）：`relay`，由 session 带着设备名调用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from . import match

# 一次最多给几个候选：设备的面板放 6 行。
CHOICES_MAX = 6
# 最高分到这个数、并且领先第二名这么多，就直接执行（用户决定：清楚的单个结果不用确认）。
AUTO_SCORE = 0.85
AUTO_LEAD = 0.15
# 低于这个分不算候选。
CANDIDATE_SCORE = 0.5
EXACT_BONUS = 0.2
# 只有这一个候选时，到这个分就直接执行。
ALONE_SCORE = 0.65
VOLUME_STEP = 10
VOLUME_BIG_STEP = 30
SEEK_DEFAULT_S = 30
LABEL_COLUMNS = 20

# 与 server/relay/table.py、channel_groups.py 的 id 规则一致：中转推流的台。
_RELAY_ID = re.compile(r"^(cast|up\d+|sr\d+)$")
_SERIES_ID = re.compile(r"^sr\d+$")

REPLY_UNKNOWN = "没听懂，再说一次？"
REPLY_EMPTY = "没听清，再说一次？"
REPLY_SWITCH = "换台中…"
REPLY_HELP = "可以说：湖南卫视、熊出没第3集、大声一点、暂停、只听声音"


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    # 设备列表里的序号（CONFIG 频道表里的位置，从 1 数），也是「第 N 台」的 N。
    number: int
    group: str = ""
    # 中转给的全名（频道表里的名字可能被截短），只用于匹配。
    full_name: str = ""


@dataclass(frozen=True)
class Series:
    id: str
    name: str
    total: int = 0


@dataclass
class Catalog:
    """一次请求的全部背景：频道表、剧集、正在播什么、设备的音量亮度。"""

    channels: list[Channel]
    series: list[Series] = field(default_factory=list)
    current: str = ""
    # 中转报的：正在播第几集、是否暂停；中转连不上时为 0 / False。
    episode: int = 0
    paused: bool = False
    volume: int | None = None
    brightness: int | None = None
    steps: int | None = None
    audio_only: int | None = None
    # 上次静音前的音量，「取消静音」恢复它。
    unmute_volume: int | None = None

    def channel(self, cid: str) -> Channel | None:
        return next((c for c in self.channels if c.id == cid), None)


@dataclass
class Decision:
    kind: str
    reply: str = ""
    do: dict | None = None
    choices: list[dict] | None = None
    # 单个结果在面板上显示成一行：名字和序号。
    label: str = ""
    lead: str = ""
    # (动作, 参数)：episode / pause / resume / next / previous / seek。
    relay: tuple[str, dict] | None = None
    # 选集后设备不在这部剧的台上时要换过去的台。
    then_channel: str = ""


# ---- 文本整理 ----

_PREFIX_FILLERS = ("你好", "小智", "小电视", "麻烦你", "麻烦", "请你", "请", "帮我", "帮忙",
                   "给我", "我想要", "我想", "我要", "我希望", "能不能", "可不可以", "可以",
                   "嗯", "呃", "那个", "就", "再")
_SUFFIX_FILLERS = ("好不好", "好吗", "可以吗", "行吗", "行不行", "谢谢你", "谢谢", "一下",
                   "吧", "啊", "呀", "呢", "嘛", "哦", "喔", "哈", "啦", "ok")
_VERBS = ("切换到", "切换成", "切换", "切到", "换到", "换成", "转到", "调到", "跳到", "回到",
          "打开", "播放", "进入", "来个", "来点", "我看", "我听", "看看", "看", "听", "播", "放",
          "去", "来", "要")
_TAIL = ("这个频道", "这个台", "那个台", "的节目", "节目", "频道", "电视台", "台")


def _strip_fillers(text: str) -> str:
    text = text.replace("一下", "")
    changed = True
    while changed and text:
        changed = False
        for word in _PREFIX_FILLERS:
            if text.startswith(word) and len(text) > len(word):
                text, changed = text[len(word):], True
        for word in _SUFFIX_FILLERS:
            if text.endswith(word) and len(text) > len(word):
                text, changed = text[:-len(word)], True
    return text


def _strip_verbs(text: str) -> str:
    changed = True
    while changed and text:
        changed = False
        for word in _VERBS:
            if text.startswith(word) and len(text) > len(word):
                text, changed = text[len(word):], True
    return text


def _strip_tail(text: str) -> str:
    for word in _TAIL:
        if text.endswith(word) and len(text) > len(word):
            return text[:-len(word)]
    return text


def _label(text: str) -> str:
    """去掉括号里的说明，再按显示宽度截到 LABEL_COLUMNS（汉字 2 格）。"""
    kept, used = [], 0
    for char in " ".join(match.strip_parens(text).split()):
        width = 1 if ord(char) < 128 else 2
        if used + width > LABEL_COLUMNS:
            break
        kept.append(char)
        used += width
    return "".join(kept).strip()


# ---- 别名 ----

_CCTV = re.compile(r"^cctv(\d{1,2})(加)?(.*)$")
# 央视各套的内容，频道表里只写「CCTV5」时用来认「央视体育」「中央新闻」。
_CCTV_THEMES = {"1": "综合", "2": "财经", "3": "综艺", "4": "中文国际", "5": "体育", "5加": "体育赛事",
                "6": "电影", "7": "国防军事", "8": "电视剧", "9": "纪录", "10": "科教", "11": "戏曲",
                "12": "社会与法", "13": "新闻", "14": "少儿", "15": "音乐", "16": "奥林匹克", "17": "农业农村"}
# 外文台名里的常见词，用中文说也认：「BBC 新闻」。
_WORDS = (("news", "新闻"), ("sports", "体育"), ("music", "音乐"), ("documentary", "纪录片"),
          ("world", "国际"))
# 名字里看不出来、但大家都这么叫的：(名字开头, 别名)。
_KNOWN = (("凤凰中文", ("凤凰卫视", "凤凰卫视中文台", "凤凰台")), ("凤凰资讯", ("凤凰资讯台",)),
          ("凤凰香港", ("凤凰香港台",)), ("东方卫视", ("上海卫视", "上海台")),
          ("cgtn英语", ("cgtn",)), ("cgtn记录", ("cgtn纪录片", "cgtn纪录")),
          ("纪实科教", ("纪实频道",)), ("中国交通", ("交通台",)))
_PICKS = re.compile(r"^(.+?)精选\d*首?.*$")


def aliases(name: str) -> list[tuple[str, float]]:
    """频道名的各种说法：(规范化后的写法, 权重)。权重 1 的说法与名字同等；0.9 的是较弱的简称。"""
    base = match.normalize(match.strip_parens(name))
    out = {base: 1.0} if base else {}

    def add(alias: str, weight: float = 1.0) -> None:
        alias = match.normalize(alias)
        if alias and out.get(alias, 0) < weight:
            out[alias] = weight

    cctv = _CCTV.match(base)
    if cctv:
        n, plus, desc = cctv.group(1), "加" if cctv.group(2) else "", cctv.group(3)
        for head in ("中央", "央视", "中央电视台", "cctv"):
            add(f"{head}{n}{plus}")
            for unit in ("套", "台", "频道"):
                add(f"{head}{n}{unit}{plus}" if not plus else f"{head}{n}{plus}{unit}")
        if plus:
            add(f"{n}加", 0.9)
            add(f"中央{n}套加")
        else:
            add(f"{n}套", 0.9)
        desc = desc or _CCTV_THEMES.get(n + plus, "")
        if desc:
            add(f"央视{desc}", 0.95)
            add(f"中央{desc}", 0.95)
            add(f"cctv{desc}", 0.95)
            # 「体育赛事」「国防军事」这样够长的说明单说也算；「体育」「新闻」太泛，
            # 广东体育之类也叫体育，只让它们一起当候选。
            if len(desc) >= 3:
                add(desc, 0.95)
    elif base.endswith("卫视") and len(base) > 2:
        place = base[:-2]
        add(place, 0.95)
        add(f"{place}台", 0.95)
        add(f"{place}电视台", 0.95)
        add(f"{place}卫视台")
    elif "卫视4k" in base:
        place = base.replace("卫视4k", "")
        add(f"{place}4k")
    for english, chinese in _WORDS:
        if english in base:
            add(base.replace(english, chinese), 0.95)
    for head, known in _KNOWN:
        if base == head:
            for alias in known:
                add(alias, 0.95)
    picks = _PICKS.match(base)
    if picks:
        singer = picks.group(1)
        add(singer, 0.95)
        add(f"{singer}精选")
        add(f"{singer}的歌", 0.95)
    return sorted(out.items())


@lru_cache(maxsize=16)
def _alias_index(channels: tuple[Channel, ...]) -> tuple[tuple[Channel, tuple[tuple[str, float], ...]], ...]:
    rows = []
    for channel in channels:
        names = dict(aliases(channel.name))
        if channel.full_name:
            for alias, weight in aliases(channel.full_name):
                names[alias] = max(names.get(alias, 0), weight)
        rows.append((channel, tuple(names.items())))
    return tuple(rows)


def _channel_score(query: str, names: tuple[tuple[str, float], ...]) -> float:
    best = 0.0
    wanted = match.digits(query)
    for alias, weight in names:
        if query == alias:
            # 和某个说法一字不差：比任何模糊结果都可信（模糊最高 0.97），领先量足够直接执行。
            value = weight + EXACT_BONUS
        else:
            value = match.score(query, alias) * weight * 0.97
            covered = 0
            if len(alias) >= 2 and alias in query:
                covered = len(alias)            # 说的话里整个包含名字（「湖南卫视的」）
            elif len(query) >= 2 and query in alias:
                covered = len(query)            # 只说了名字的一部分（「体育」「熊出没之探险」）
            elif len(query) >= 3 and match.lcs_ratio(query, alias) * (len(query) + len(alias)) / 2 == len(query):
                covered = len(query) - 0.5      # 说的字都按顺序在名字里（「熊出没探险」）
            if covered:
                # 一半看说的话被名字解释了多少，一半看名字被说出了多少。
                value = max(value, (0.5 + 0.25 * covered / len(query) + 0.25 * covered / len(alias)) * weight)
            else:
                run = match.common_run(query, alias)
                if run >= 2:
                    # 有两个以上连着的字一样（「凤凰卫视」和「凤凰中文」）：列为候选。
                    value = max(value, 0.5 + 0.2 * run / len(alias))
            have = match.digits(alias)
            if wanted and have and wanted != have:
                # 数字不同就不是同一个台：「中央5套」不能因为字像就选成「中央15套」。
                value *= 0.5
        best = max(best, value)
    return best


def rank_channels(queries: list[str], channels: list[Channel]) -> list[tuple[float, Channel]]:
    """按分数从高到低；同一个台只出现一次。"""
    scored = []
    for channel, names in _alias_index(tuple(channels)):
        value = max((_channel_score(q, names) for q in queries if q), default=0.0)
        if value > 0:
            scored.append((value, channel))
    scored.sort(key=lambda item: (-item[0], item[1].number))
    return scored


def _pick(scored: list) -> tuple[str, list]:
    """("one", [最佳]) / ("some", 候选) / ("none", [])。"""
    if not scored or scored[0][0] < CANDIDATE_SCORE:
        return "none", []
    top = scored[0][0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if top >= AUTO_SCORE and top - second >= AUTO_LEAD:
        return "one", [scored[0][1]]
    if top >= ALONE_SCORE and second < CANDIDATE_SCORE:
        # 只有一个像样的候选（「老高」只可能是「老高与小茉」）：不必让用户再选一次。
        return "one", [scored[0][1]]
    floor = max(CANDIDATE_SCORE, top - 0.3)
    return "some", [item for score, item in scored if score >= floor][:CHOICES_MAX]


# ---- 固定说法 ----

def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern)


_CANCEL = _rx(r"^(算了|取消|不用了?|没事了?|不要了|不看了|关掉面板|退出|返回|没什么)$")
_HELP = _rx(r"(你能做什么|你会什么|能说什么|可以说什么|怎么用|帮助|使用说明|^help$)")
_SETUP = _rx(r"(配网|配置网络|设置网络|网络设置|重新联网|连接?wifi|连接?无线|换wifi|换网络|改wifi|"
             r"wifi设置|无线设置|设置wifi|连网络|换个网络)")
_STATUS = _rx(r"^(状态|状态页|看状态|打开状态页?|信号怎么样|信号|网络怎么样|网速|网络状态|电量|"
              r"电池|还有多少电|剩多少电|电还有多少|设备状态|状态信息)$")
_AUDIO_ON = _rx(r"(只听声音|只要声音|只放声音|只听|关掉画面|关闭画面|不要画面|关画面|音频模式|听声音模式|"
                r"息屏|关屏幕?|熄屏)")
_AUDIO_OFF = _rx(r"(打开画面|显示画面|^要画面$|恢复画面|开画面|看画面|有画面|退出只听声音|退出音频模式|"
                 r"不只听声音|亮屏|打开屏幕)")
_MUTE = _rx(r"^(静音|关掉声音|关闭声音|关声音|别出声|不要声音)$")
_UNMUTE = _rx(r"^(取消静音|打开声音|恢复声音|开声音|有声音|解除静音)$")
_VOLUME_VALUE = _rx(r"(?:音量|声音)(?:调整为|调整到|调整成|调整|调节到|调节为|调节|调到|调成|调为|开到|改成|改为|改到|放到|设为|设成|设置为|设置成|设置到|到|是|为|调|开)?(?:百分之)?(\d{1,3})(?:%|格|级|档)?$")
_VOLUME_UP = _rx(r"(大声|声音大|音量大|音量调大|调大音量|音量加|加音量|音量高|响(1|一)?点|太小声|听不见|听不清|大点声|"
                 r"大1点声|声音开大|开大点|再大|音量往上|音量增加|增大音量|音量增大|调大点?)")
_VOLUME_DOWN = _rx(r"(小声|声音小|音量小|音量调小|调小音量|音量减|减音量|音量低|轻(1|一)?点|太吵|太大声|太响|"
                   r"小点声|小1点声|声音关小|关小点|再小|音量往下|音量减少|减小音量|音量减小|调小点?)")
_VOLUME_MAX = _rx(r"(最大声|音量最大|声音最大|开到最大|调到最大)")
_VOLUME_MIN = _rx(r"(最小声|音量最小|声音最小|开到最小|调到最小)")
_MUCH = _rx(r"(很多|多1?点|大大|好多|非常)")
_BRIGHT_VALUE = _rx(r"(?:亮度|屏幕亮度|背光)(?:调整为|调整到|调整成|调整|调节到|调节为|调节|调到|调成|调为|开到|改成|改为|改到|设为|设成|设置为|设置成|设置到|到|是|为|调)?(\d{1,3})(?:%|格|级|档)?$")
_BRIGHT_UP = _rx(r"(亮(1|一)?点|调亮|更亮|太暗|亮度高|亮度调高|亮度加|调高亮度|亮度大|屏幕亮|再亮|看不清)")
_BRIGHT_DOWN = _rx(r"(暗(1|一)?点|调暗|更暗|太亮|亮度低|亮度调低|亮度减|调低亮度|亮度小|屏幕暗|再暗|刺眼)")
_BRIGHT_MAX = _rx(r"(最亮|亮度最大|亮度最高)")
_BRIGHT_MIN = _rx(r"(最暗|亮度最小|亮度最低)")
_PAUSE = _rx(r"^(暂停|暂停播放|停(1|一)?下|停1停|停一停|先停|停|停住|别放了|别播了|暂停1?下)$")
_RESUME = _rx(r"^(继续|继续播放?|继续看|接着播|接着放|接着看|恢复播放|开始播放|播放|开始|取消暂停|放吧|接着来)$")
_RESTART = _rx(r"^(从头播放?|从头看|从头开始|重新播放?|重播|重播这集|重看|从头放|重新开始|再看1遍|再放1遍)$")
_SEEK = _rx(r"^(快进|前进|往前|往后快进|快退|后退|倒退|往回|退回|倒回去?|回退)(?:到)?(\d+)?(秒钟?|分钟?|分)?$")
_SEEK_TO = _rx(r"^(?:跳到|跳转到|快进到|播到|从)(\d+)(分钟?|秒钟?|分)(?:开始)?(?:播放?|看)?$")
_NEXT_EP = _rx(r"^(下1集|下一集|下集|播下一集|看下一集|下1个视频|下一个视频|换1个视频|换一个视频|下个视频|"
               r"跳过|跳过这集|跳过这个|下1个|下一个|下个)$")
_PREV_EP = _rx(r"^(上1集|上一集|上集|播上一集|看上一集|上1个视频|上一个视频|上个视频|回上一集)$")
_NEXT_CH = _rx(r"^(下1个台|下一个台|下个台|下1个频道|下一个频道|下个频道|换个台|换一个台|换台|下1台|下一台|"
               r"下1频道|频道加|台加)$")
_PREV_CH = _rx(r"^(上1个台|上一个台|上个台|上1个频道|上一个频道|上个频道|上1台|上一台|频道减|台减|"
               r"回到上个台|返回上个台)$")
_NUMBER = _rx(r"^(?:第(\d+)(?:个)?(?:台|频道|号台)|(\d+)(?:号台|号频道|台)|频道(\d+)|(\d+)(?:号)?频道|第(\d+)个)$")
_EPISODE = _rx(r"^(.*?)(?:的)?第(\d+)(?:集|话|期)$")
_EPISODE_SHORT = _rx(r"^(.+?)(?:的)?(\d+)(?:集)$")
_LATEST = _rx(r"^(.*?)(?:的)?(?:最新|最后)(?:的)?(?:1|一)?(?:集|期)$")
_GROUPS = {"央视": "央视", "中央台": "央视", "卫视": "卫视", "卫视台": "卫视", "地方台": "卫视",
           "港澳台": "港澳台", "香港台": "港澳台", "台湾台": "港澳台", "国际": "国际", "国外": "国际",
           "外国台": "国际", "国际台": "国际", "英文台": "国际", "音乐": "音乐", "听歌": "音乐",
           "歌": "音乐", "动画": "动画影视", "动画片": "动画影视", "动画影视": "动画影视", "电影": "动画影视",
           "影视": "动画影视", "up主": "UP 主", "up": "UP 主", "其他": "其他"}


def _volume_value(n: int) -> int:
    """「音量5」是 50；「音量50」是 50。"""
    return n * 10 if n <= 10 else min(100, n)


def _device_says(kind: str, reply: str, **do) -> Decision:
    return Decision(kind, reply=reply, do=do)


def _volume(text: str, catalog: Catalog) -> Decision | None:
    current = catalog.volume if catalog.volume is not None else 50
    target = None
    if _MUTE.search(text):
        return _device_says("volume", "已静音", volume=0)
    if _UNMUTE.search(text):
        target = catalog.unmute_volume or (current if current > 0 else 50)
        return _device_says("volume", f"音量 {target}", volume=target)
    value = _VOLUME_VALUE.search(text)
    if value:
        target = _volume_value(int(value.group(1)))
    elif _VOLUME_MAX.search(text):
        target = 100
    elif _VOLUME_MIN.search(text):
        target = 10
    elif _VOLUME_UP.search(text):
        target = current + (VOLUME_BIG_STEP if _MUCH.search(text) else VOLUME_STEP)
    elif _VOLUME_DOWN.search(text):
        target = current - (VOLUME_BIG_STEP if _MUCH.search(text) else VOLUME_STEP)
    if target is None:
        return None
    target = max(0, min(100, int(round(target / 10.0)) * 10))
    if target == current and catalog.volume is not None:
        edge = "最大" if target == 100 else "最小" if target == 0 else str(target)
        return Decision("volume", reply=f"音量已经是{edge}了")
    return _device_says("volume", f"音量 {target}", volume=target)


def _brightness(text: str, catalog: Catalog) -> Decision | None:
    value = _BRIGHT_VALUE.search(text)
    wants = ("max" if _BRIGHT_MAX.search(text) else "min" if _BRIGHT_MIN.search(text)
             else "value" if value else "up" if _BRIGHT_UP.search(text)
             else "down" if _BRIGHT_DOWN.search(text) else None)
    if wants is None:
        return None
    steps, level = catalog.steps, catalog.brightness
    if not steps or level is None:
        return Decision("brightness", reply="这台电视的固件不报亮度，用按键调吧")
    # 固件的第 0 档最亮；用户说的「亮度 N」越大越亮，N 从 1 数到 steps。
    if wants == "max":
        target = 0
    elif wants == "min":
        target = steps - 1
    elif wants == "value":
        n = int(value.group(1))
        target = steps - n if 1 <= n <= steps else round((100 - min(100, n)) / 100 * (steps - 1))
    elif wants == "up":
        target = level - 1
    else:
        target = level + 1
    target = max(0, min(steps - 1, target))
    if target == level:
        return Decision("brightness", reply="已经最亮了" if target == 0 else
                        "已经最暗了" if target == steps - 1 else f"亮度已经是 {steps - target} 了")
    return _device_says("brightness", f"亮度 {steps - target}", brightness=target)


def _seek_seconds(amount: str | None, unit: str | None) -> int:
    n = int(amount) if amount else SEEK_DEFAULT_S if not unit or unit.startswith("秒") else 1
    return n * 60 if unit and unit.startswith("分") else n


def _step_channel(catalog: Catalog, step: int) -> Channel | None:
    """同一分组里的下一个 / 上一个台，到头了绕回来（和 ▲▼ 长按一样）。"""
    current = catalog.channel(catalog.current)
    if current is None:
        return catalog.channels[0] if catalog.channels else None
    same = [c for c in catalog.channels if c.group == current.group] or catalog.channels
    at = same.index(current) if current in same else 0
    return same[(at + step) % len(same)]


def _channel_decision(channel: Channel, catalog: Catalog) -> Decision:
    label, lead = _label(channel.name), f"{channel.number:02d}"
    if channel.id == catalog.current:
        return Decision("channel", reply=f"正在播{label}", label=label, lead=lead)
    return Decision("channel", reply=REPLY_SWITCH, do={"channel": channel.id}, label=label, lead=lead)


def _choices(channels: list[Channel], reply: str) -> Decision:
    rows = [{"label": _label(c.name), "lead": f"{c.number:02d}", "do": {"channel": c.id}}
            for c in channels[:CHOICES_MAX]]
    return Decision("choices", reply=reply, choices=rows)


def _series_list(catalog: Catalog) -> list[Series]:
    """中转给的剧集；中转连不上时退回到频道表里的剧集台（不知道有几集）。"""
    if catalog.series:
        return catalog.series
    return [Series(c.id, c.full_name or c.name) for c in catalog.channels if _SERIES_ID.match(c.id)]


def episode_ref(series_id: str, n: int) -> str:
    return f"ep:{series_id}:{n}"


def _episode_decision(series: Series, n: int, catalog: Catalog) -> Decision:
    name = _label(series.name)
    if n < 1 or (series.total and n > series.total):
        return Decision("episode", reply=f"{name}只有 {series.total} 集" if series.total
                        else f"没有第 {n} 集")
    decision = Decision("episode", reply=f"从第 {n} 集开始播", label=_label(f"{series.name} 第{n}集"),
                        lead=f"{n:02d}", relay=("episode", {"series": series.id, "episode": n}))
    if catalog.current != series.id:
        decision.then_channel = series.id
    return decision


def _series_episode(text: str, catalog: Catalog) -> Decision | None:
    full = _EPISODE.match(text)
    latest = None if full else _LATEST.match(text)
    found = full or (None if latest else _EPISODE_SHORT.match(text))
    if not found and not latest:
        return None
    named = (found or latest).group(1)
    named = "" if named in _VERBS else _strip_verbs(named)
    series = _series_list(catalog)
    if not named:
        current = next((s for s in series if s.id == catalog.current), None)
        if current is None:
            return Decision("episode", reply="现在不是在看剧，先说剧名，比如：熊出没第3集")
        chosen = [current]
    else:
        index = [Channel(s.id, s.name, n) for n, s in enumerate(series, 1)]
        kind, picked = _pick(rank_channels([named, _strip_tail(named)], index))
        if kind == "none":
            # 「X3集」也可能是频道名的一部分，交给后面的频道匹配；「X第3集」就是在找剧。
            return None if found is not None and full is None else Decision(
                "none", reply="没有找到这部剧")
        chosen = [series[c.number - 1] for c in picked]
    if latest:
        # 中转连不上时不知道有几集，「最新一集」做不了。
        if len(chosen) == 1 and not chosen[0].total:
            return Decision("episode", reply="现在不知道这部剧有几集，说第几集吧")
    if len(chosen) == 1:
        n = chosen[0].total if latest else int(found.group(2))
        return _episode_decision(chosen[0], n, catalog)
    rows = []
    for s in chosen[:CHOICES_MAX]:
        n = s.total if latest else int(found.group(2))
        if n >= 1 and (not s.total or n <= s.total):
            rows.append({"label": _label(f"{s.name} 第{n}集"), "lead": f"{n:02d}",
                         "ref": episode_ref(s.id, n)})
    if not rows:
        return Decision("episode", reply="没有这一集")
    return Decision("choices", reply=f"找到 {len(rows)} 部，选一个", choices=rows)


def _relay_playing(catalog: Catalog) -> bool:
    return bool(_RELAY_ID.match(catalog.current or ""))


def _playback(text: str, catalog: Catalog) -> Decision | None:
    """暂停、继续、跳转、上一集、下一集：交给中转；直播台做不了。"""
    seek = _SEEK.match(text)
    seek_to = _SEEK_TO.match(text)
    kind = ("pause" if _PAUSE.match(text) else "resume" if _RESUME.match(text)
            else "restart" if _RESTART.match(text) else "seek" if seek or seek_to
            else "next" if _NEXT_EP.match(text) else "previous" if _PREV_EP.match(text) else None)
    if kind is None:
        return None
    series = bool(_SERIES_ID.match(catalog.current or ""))
    if not _relay_playing(catalog):
        if kind == "next" and not text.endswith("集"):
            return None                         # 「下一个」在直播台上是换台
        if kind == "resume" and not catalog.paused:
            return Decision("resume", reply="正在播")
        return Decision(kind, reply={"pause": "直播台不能暂停", "resume": "直播台不能暂停",
                                     "restart": "直播台不能从头播", "seek": "直播台不能快进快退",
                                     "next": "直播台没有下一集", "previous": "直播台没有上一集"}[kind])
    if kind == "pause":
        return Decision("pause", reply="暂停", relay=("pause", {}))
    if kind == "resume":
        return Decision("resume", reply="继续播放", relay=("resume", {}))
    if kind == "restart":
        return Decision("seek", reply="从头播放", relay=("seek", {"position": 0}))
    if kind == "seek":
        if seek_to:
            seconds = _seek_seconds(seek_to.group(1), seek_to.group(2))
            return Decision("seek", reply=f"跳到 {seconds // 60} 分 {seconds % 60} 秒",
                            relay=("seek", {"position": seconds}))
        seconds = _seek_seconds(seek.group(2), seek.group(3))
        back = seek.group(1) in ("快退", "后退", "倒退", "往回", "退回", "倒回", "倒回去", "回退")
        words = f"{seconds // 60} 分钟" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} 秒"
        return Decision("seek", reply=f"{'后退' if back else '快进'} {words}",
                        relay=("seek", {"delta": -seconds if back else seconds}))
    if kind == "previous":
        if not series:
            return Decision("previous", reply="只有剧集台能回到上一集")
        return Decision("previous", reply="上一集", relay=("previous", {}))
    return Decision("next", reply="下一集" if series else "下一个", relay=("next", {}))


def decide(transcript: str, catalog: Catalog) -> Decision:
    """识别出来的一句话 → 决定。"""
    raw = match.normalize(transcript)
    if not raw:
        return Decision("none", reply=REPLY_EMPTY)
    text = _strip_fillers(raw)
    if _CANCEL.match(text) or _CANCEL.match(raw):
        return Decision("cancel", reply="好的", do={"page": "close"})
    if _HELP.search(text):
        return Decision("help", reply=REPLY_HELP)
    if _SETUP.search(text):
        return Decision("setup", reply="进入配网会停止播放",
                        choices=[{"label": "进入配网", "do": {"setup": 1}},
                                 {"label": "算了", "do": {"page": "close"}}])
    if _STATUS.match(text) or _STATUS.match(_strip_verbs(text)):
        return _device_says("page", "打开状态页", page="status")
    if _AUDIO_OFF.search(text):
        if catalog.audio_only == 0:
            return Decision("audio_only", reply="画面已经开着")
        return _device_says("audio_only", "打开画面", audio_only=0)
    if _AUDIO_ON.search(text):
        if catalog.audio_only == 1:
            return Decision("audio_only", reply="已经是只听声音了")
        return _device_says("audio_only", "只听声音", audio_only=1)
    for handler in (_volume, _brightness):
        decision = handler(text, catalog)
        if decision is not None:
            return decision
    decision = _playback(text, catalog)
    if decision is not None:
        return decision
    if _NEXT_CH.match(text) or _PREV_CH.match(text) or (_NEXT_EP.match(text) and not _relay_playing(catalog)):
        target = _step_channel(catalog, -1 if _PREV_CH.match(text) else 1)
        return Decision("none", reply=REPLY_UNKNOWN) if target is None else _channel_decision(target, catalog)
    decision = _series_episode(text, catalog)
    if decision is not None:
        return decision
    bare = _strip_tail(_strip_verbs(text))
    number = _NUMBER.match(_strip_verbs(text))
    if number:
        n = int(next(g for g in number.groups() if g))
        channel = next((c for c in catalog.channels if c.number == n), None)
        if channel is None:
            return Decision("channel", reply=f"没有第 {n} 台，一共 {len(catalog.channels)} 个台")
        return _channel_decision(channel, catalog)
    group = _GROUPS.get(bare) or _GROUPS.get(_strip_verbs(text))
    if group:
        members = [c for c in catalog.channels if c.group == group]
        if members:
            return _choices(members, f"{group}有 {len(members)} 个台" if len(members) > CHOICES_MAX
                            else f"{group}的台，选一个")
    queries = list(dict.fromkeys(q for q in (text, _strip_verbs(text), bare) if q))
    kind, picked = _pick(rank_channels(queries, catalog.channels))
    if kind == "one":
        return _channel_decision(picked[0], catalog)
    if kind == "some":
        return _choices(picked, f"找到 {len(picked)} 个，选一个")
    return Decision("none", reply=REPLY_UNKNOWN)
