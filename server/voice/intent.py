"""语音指令 → 决定。纯函数，不碰网络、不碰套接字，单测覆盖（tests/test_voice_intent.py）。

规则优先：先认固定说法（音量、亮度、暂停、配网……），再认「有哪些 YouTube 频道」这类问一类台的
话，再认「X 第 N 集」，最后把剩下的话当成频道名做精确、别名、编号、分组、模糊匹配。设计见
docs/superpowers/specs/2026-10-12-voice-control-design.md 第 3 节。

结果分三类，由 session.py 发给设备或交给中转：
- 设备自己做的：`do` 里是 AV_SWITCH 认识的键（channel、volume、brightness、audio_only）
  外加新的 page、setup；
- 几个候选让用户选：`choices`，每项带 `do`，或带 `ref`（选中后设备发 pick，服务端再回一个结果：
  选集 `ep:…`，列表翻页或打开一组 `ls:…`）；
- 中转做的（选集、暂停、继续、跳转、上一集、下一集）：`relay`，由 session 带着设备名调用；
- 搜索（「搜视频猫和老鼠」「油管搜频道 lofi」「我想看猫和老鼠的视频」）：`search`，由 session
  调中转去网站搜；结果页、追问和选中 UP 主后的两个动作也在这里拼（search_page、search_question、
  creator_menu），选项的 ref 是 `s?:…`。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from ..channel_groups import GROUPS as GROUP_ORDER
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
# 列一类台时一页几个：放不下时第 6 行是「下一页」。
PAGE_SIZE = 5

# 与 server/relay/table.py、channel_groups.py 的 id 规则一致：中转推流的台。
_RELAY_ID = re.compile(r"^(cast|up\d+|sr\d+)$")
_SERIES_ID = re.compile(r"^sr\d+$")
_CREATOR_ID = re.compile(r"^up\d+$")

REPLY_UNKNOWN = "没听懂，再说一次？"
REPLY_EMPTY = "没听清，再说一次？"
REPLY_SWITCH = "换台中…"
REPLY_HELP = "可以说：湖南卫视、熊出没第3集、有哪些台、大声一点、暂停、只听声音"


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    # 设备列表里的序号（CONFIG 频道表里的位置，从 1 数），也是「第 N 台」的 N。
    number: int
    group: str = ""
    # 中转给的全名（频道表里的名字可能被截短），只用于匹配。
    full_name: str = ""
    # 中转的台来自哪个网站：「bilibili」「youtube」；直播台、或中转没说时为空。
    site: str = ""


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
    # 搜索：{"site": bilibili|youtube, "kind": video|up|""（没说清，要追问）, "query"}。
    search: dict | None = None


# ---- 文本整理 ----

_PREFIX_FILLERS = ("你好", "小智", "小电视", "麻烦你", "麻烦", "请你", "请", "帮我", "帮忙",
                   "给我", "我想要", "我想", "我要", "我希望", "能不能", "可不可以", "可以",
                   "嗯", "呃", "那个", "就", "再")
_SUFFIX_FILLERS = ("好不好", "好吗", "可以吗", "行吗", "行不行", "谢谢你", "谢谢", "一下",
                   "吧", "啊", "呀", "呢", "嘛", "哦", "喔", "哈", "啦", "ok")
_VERBS = ("切换到", "切换成", "切换", "切到", "换到", "换成", "转到", "调到", "跳到", "回到",
          "打开", "播放", "进入", "来个", "来点", "我看", "我听", "看看", "看", "听", "播", "放",
          "去", "来", "要")
# 说了这些词，后面就是一个台的名字（找不到时回答「没有找到「X」」）。
_NAMING_VERBS = ("切换到", "切换成", "切换", "切到", "换到", "换成", "转到", "调到", "打开", "播放",
                 "我看", "看看", "看", "播", "来个", "来点")
# 这些字说明后面不是名字（「看什么好」「看不了」）。
_NOT_A_NAME = re.compile(r"(什么|哪|啥|怎么|吗|不|没|了)")
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
_RESTART = _rx(r"^(从头播放?|从头看|从头开始|从头开始播放?|从头开始看|重新播放?|重播|重播这集|重看|从头放|"
               r"重新开始|再看1遍|再放1遍|回到开头|回到最开始|回到开始|跳到开头|跳回开头|从第1秒开始(?:播放?|看)?|"
               r"重新看这集|重新看1遍|重新看)$")
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


# ---- 跳转：「快进1分30秒」「跳到3分20秒」「跳到一半」「跳到百分之六十」「跳到最后」 ----
#
# 用自己的规范化（seek_text）：识别结果里的「12:30」规范化后会变成「1230」，「百分之六十」和「60%」
# 会变成「60」，这里先把它们换成「12分30秒」和「‰60」再规范化。

# 「跳到最后」停在结尾前这么多秒。
SEEK_END_S = 30
SEEK_MAX_S = 24 * 3600
_PERCENT = "‰"
_CLOCK3 = re.compile(r"(?<!\d)(\d{1,2})[:：](\d{2})[:：](\d{2})(?!\d)")
_CLOCK2 = re.compile(r"(?<!\d)(\d{1,3})[:：](\d{2})(?!\d)")
_HOURS = re.compile(r"^(?:(\d{1,2}|一)个?(半)?(?:小时|钟头)|半个?(?:小时|钟头))")
_MINUTES = re.compile(r"^(?:(\d{1,4})(?:分钟|分)(半)?|半分钟?)")
_CLOCK_MINUTES = re.compile(r"^(\d{1,3})点(?=\d)")
_SECONDS = re.compile(r"^(\d{1,5})秒钟?")
_SEEK_ABS = re.compile(r"^(?:直接)?(?:跳转到|跳转至|跳到|跳至|跳去|跳回|快进到|快退到|快到|播到|放到|拖到|定位到|"
                       r"回到|退到|倒回到|倒到|从|到)")
# 「跳到」后面：要跳到的地方，再加可有可无的「的地方」「开始播」。
_SEEK_ABS_REST = re.compile(r"^(.+?)(?:的地方|的位置|位置|那里|那儿|处|左右)?(?:开始)?(?:播放?|放|看)?$")
_SEEK_FORWARD = ("往后快进", "往前快进", "向前快进", "快进", "前进", "往前", "向前", "往后跳", "向后跳")
_SEEK_BACK = ("往回倒", "往回退", "往后退", "向后退", "倒回去", "快退", "后退", "倒退", "往回", "退回", "倒回",
              "回退", "倒带")
_SEEK_REL = re.compile(rf"^({'|'.join(sorted(_SEEK_FORWARD + _SEEK_BACK, key=len, reverse=True))})(?:了|个)?(.*)$")
_START_WORDS = ("开头", "最开始", "最开头", "开始", "头", "起点", "片头")
_END_WORDS = ("最后", "最后面", "结尾", "末尾", "片尾", "快结束", "结束")
_HALF_WORDS = ("1半", "一半", "中间", "正中间", "中间位置", "1半的地方")
_FRACTION = re.compile(r"^(\d{1,2})分之(\d{1,2})$")
_PERCENTAGE = re.compile(rf"^(?:{_PERCENT}(\d{{1,3}})|(\d{{1,3}}){_PERCENT})$")


def seek_text(transcript: str) -> str:
    text = _CLOCK3.sub(r"\1小时\2分\3秒", transcript or "")
    text = _CLOCK2.sub(r"\1分\2秒", text)
    for percent in ("百分之", "%", "％"):
        text = text.replace(percent, _PERCENT)
    return _strip_fillers(match.normalize(text))


def _number(value: str) -> int:
    return 1 if value == "一" else int(value)


def seek_seconds(text: str) -> int | None:
    """「3分20秒」「1个小时5分30秒」「12分半」「半小时」「第90秒」「3分20」「12点30」「90」→ 秒；看不懂为 None。"""
    text = text.removeprefix("第")
    total, last = 0, ""
    found = _HOURS.match(text)
    if found:
        hours = _number(found.group(1)) if found.group(1) else 0
        total += hours * 3600 + (1800 if found.group(2) or not found.group(1) else 0)
        text, last = text[found.end():].removeprefix("零"), "h"
    found = _MINUTES.match(text) or _CLOCK_MINUTES.match(text)
    if found:
        minutes = int(found.group(1)) if found.group(1) else 0
        half = found.re is _MINUTES and (found.group(2) or not found.group(1))
        total += minutes * 60 + (30 if half else 0)
        text, last = text[found.end():].removeprefix("零"), "m"
    found = _SECONDS.match(text)
    if found:
        total += int(found.group(1))
        text, last = text[found.end():], "s"
    elif text.isdigit() and len(text) <= 2 and last in ("h", "m"):
        # 「3分20」「1小时5」：最后一个单位没说，就是下一级。
        total += int(text) * (60 if last == "h" else 1)
        text, last = "", "s"
    elif text.isdigit() and len(text) <= 5 and not last:
        total, text, last = int(text), "", "s"      # 只说数字：秒（「快进10」）
    if text or not last or total > SEEK_MAX_S:
        return None
    return total


def time_words(seconds: int) -> str:
    """「3 分 20 秒」「1 小时 5 分」「20 秒」「10 分」。"""
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    parts = ([f"{hours} 小时"] if hours else []) + ([f"{minutes} 分"] if minutes else []) + \
        ([f"{secs} 秒"] if secs or not (hours or minutes) else [])
    return " ".join(parts)


def _span_words(seconds: int) -> str:
    """相对跳转说多少：整分钟说「N 分钟」，否则「1 分 30 秒」「20 秒」。"""
    return f"{seconds // 60} 分钟" if 60 <= seconds < 3600 and seconds % 60 == 0 else time_words(seconds)


def _seek_target(text: str) -> tuple | None:
    """跳到哪：("restart",)、("position", 秒)、("fraction", 比例, 说法)、("from_end", 秒)；不是为 None。"""
    if text in _START_WORDS:
        return ("restart",)
    if text in _END_WORDS:
        return ("from_end", SEEK_END_S)
    if text in _HALF_WORDS:
        return ("fraction", 0.5, "一半")
    found = _FRACTION.match(text)
    if found:
        whole, part = int(found.group(1)), int(found.group(2))
        if 0 < part < whole:
            return ("fraction", round(part / whole, 4), f"{part}/{whole}")
        return None
    found = _PERCENTAGE.match(text)
    if found:
        percent = int(found.group(1) or found.group(2))
        if percent > 100:
            return None
        return (("restart",) if percent == 0 else ("from_end", SEEK_END_S) if percent == 100
                else ("fraction", percent / 100, f"{percent}%"))
    seconds = None if text.isdigit() else seek_seconds(text)     # 「跳到5」：不知道是秒还是分
    if seconds is None:
        return None
    return ("restart",) if seconds == 0 else ("position", seconds)


def seek_request(transcript: str) -> tuple | None:
    """一句话里的跳转：("delta", 秒)、("restart",)、("position", 秒)、("fraction", 比例, 说法)、
    ("from_end", 秒)；不是跳转为 None。「一半」「三分之一」单说不算，要有「跳到」「到」之类。"""
    text = seek_text(transcript)
    relative = _SEEK_REL.match(text)
    if relative and not relative.group(2).startswith("到"):
        verb, amount = relative.group(1), relative.group(2)
        seconds = SEEK_DEFAULT_S if not amount else seek_seconds(amount)
        if not seconds:
            return None
        return ("delta", -seconds if verb in _SEEK_BACK else seconds)
    found = _SEEK_ABS.match(text)
    if not found:
        return None
    rest = text[found.end():]
    if not rest:
        return None
    target = _SEEK_ABS_REST.match(rest).group(1)
    return _seek_target(target) or (_seek_target(rest) if target != rest else None)


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
            return None if found is not None and full is None else _not_found(named)
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


def _not_found(named: str, hint: bool = False) -> Decision:
    """说了一个名字，但目录里没有：仍是 none（设备放低音），回答里说出没找到的是什么；
    hint：再提示可以搜索它（「我要看葫芦娃」）。"""
    if not named:
        return Decision("none", reply=REPLY_UNKNOWN)
    label = _label(named)
    return Decision("none", reply=f"没有找到「{label}」，可以说「搜索{label}」" if hint
                    else f"没有找到「{label}」")


def _relay_playing(catalog: Catalog) -> bool:
    return bool(_RELAY_ID.match(catalog.current or ""))


def _playback(text: str, catalog: Catalog, transcript: str = "") -> Decision | None:
    """暂停、继续、跳转、上一集、下一集：交给中转；直播台做不了。"""
    kind = ("pause" if _PAUSE.match(text) else "resume" if _RESUME.match(text)
            else "restart" if _RESTART.match(text) else None)
    jump = None
    if kind is None:
        jump = seek_request(transcript or text)
        kind = None if jump is None else "restart" if jump[0] == "restart" else "seek"
    if kind is None:
        kind = "next" if _NEXT_EP.match(text) else "previous" if _PREV_EP.match(text) else None
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
        if jump[0] == "delta":
            seconds = jump[1]
            return Decision("seek", reply=f"{'后退' if seconds < 0 else '快进'} {_span_words(abs(seconds))}",
                            relay=("seek", {"delta": seconds}))
        if jump[0] == "position":
            # 超过结尾时中转回答「这个视频只有 N 分钟」，不跳（session 调中转时带 within）。
            return Decision("seek", reply=f"跳到 {time_words(jump[1])}", relay=("seek", {"position": jump[1]}))
        if jump[0] == "fraction":
            # 比例由中转按正在播的视频的时长算；回答里的时间由 session 按中转的回答改写。
            return Decision("seek", reply=f"跳到{jump[2]}", relay=("seek", {"fraction": jump[1]}))
        return Decision("seek", reply="跳到最后", relay=("seek", {"from_end": jump[1]}))
    if kind == "previous":
        if not series:
            return Decision("previous", reply="只有剧集台能回到上一集")
        return Decision("previous", reply="上一集", relay=("previous", {}))
    return Decision("next", reply="下一集" if series else "下一个", relay=("next", {}))


# ---- 问一类台：「我有哪些 YouTube 频道」「B站的 UP 主有哪些」「有什么可以看的」 ----
#
# 整句话切成下面这些词（每处取最长的词），切得完、并且有问的词（哪些、什么、列出……）才算在问；
# 有一个字切不出来（「有没有湖南卫视」的「湖南」）就不是，交给后面的频道匹配。只说类别本身
# （「卫视」「YouTube 频道」「B站 UP 主」）也算，在频道匹配前、编号之后认（_bare_category）。

_SITE_WORDS = {"youtube": "youtube", "油管": "youtube", "优兔": "youtube", "u管": "youtube",
               "you管": "youtube", "u2b": "youtube", "b站": "bilibili", "哔哩哔哩": "bilibili",
               "哔哩": "bilibili", "bilibili": "bilibili", "bili": "bilibili", "小破站": "bilibili"}
_KIND_WORDS = {"up主": "up", "up": "up", "博主": "up", "创作者": "up", "作者": "up",
               "剧集": "series", "剧": "series", "电视剧": "series", "连续剧": "series",
               "番剧": "series", "合集": "series", "播放列表": "series", "系列": "series"}
_GROUP_WORDS = {"央视": "央视", "中央台": "央视", "中央电视台": "央视", "cctv": "央视",
                "卫视": "卫视", "卫视台": "卫视", "地方台": "卫视", "省台": "卫视",
                "港澳台": "港澳台", "港台": "港澳台", "港澳": "港澳台", "香港台": "港澳台",
                "台湾台": "港澳台", "国际": "国际", "国际台": "国际", "国外": "国际", "外国": "国际",
                "外国台": "国际", "英文台": "国际", "海外": "国际", "境外": "国际",
                "音乐": "音乐", "歌": "音乐", "听歌": "音乐", "歌曲": "音乐", "mv": "音乐",
                "动画": "动画影视", "动画片": "动画影视", "动画影视": "动画影视", "动漫": "动画影视",
                "卡通": "动画影视", "影视": "动画影视", "电影": "动画影视", "其他": "其他", "其它": "其他"}
# 只是「台」的意思的词。
_NOUN_WORDS = ("频道", "台", "电视台", "节目", "内容", "东西", "视频", "账号", "号", "电视", "直播",
               "可以看的", "能看的", "好看的", "可看的", "看的", "可以播的", "能播的", "可以听的", "能听的")
# 问的词。
_ASK_WORDS = ("有没有", "都有", "有啥", "还有", "一共有", "哪些", "哪几个", "哪一些", "哪个", "哪一个",
              "哪位", "哪几位", "哪", "谁", "什么", "啥", "几个", "多少", "列出来", "列出", "列", "列表",
              "清单", "目录", "显示", "告诉我", "所有", "全部", "说说", "讲讲", "介绍")
# 没说类别也是在问所有台的词（「列出所有」）；另外带「有」的也是（「都有什么」）。
_ASK_ALL = ("所有", "全部", "列出", "列出来", "列表", "清单", "目录")
# 不影响意思的字词。
_PLAIN_WORDS = ("我", "我们", "你", "你们", "咱", "咱们", "家", "这里", "这儿", "现在", "目前", "都",
                "一共", "总共", "有", "没有", "些", "的", "了", "过", "订阅", "关注", "收藏", "添加",
                "加了", "存了", "里", "里面", "上", "中", "在", "还", "是", "个", "们", "吗", "呢",
                "啊", "呀", "和", "给我", "看看", "瞧瞧", "能", "可以", "那", "这", "分别", "请问",
                "帮我", "出来", "看", "听", "哦", "嗯", "呗", "问", "知道", "想知道")
# 只说类别时（「我的 YouTube 频道」）除了类别和「台」只能有这几个字。
_BARE_PLAIN = ("我", "我们", "的", "上", "里")
# 拼在类别前面：「YouTube 剧集」「B站剧集」。
_SITE_LABEL = {"youtube": "YouTube ", "bilibili": "B站"}
_KIND_LABEL = {"up": "UP 主", "series": "剧集"}
_SITE_CODE = {"youtube": "yt", "bilibili": "bl"}
_KIND_CODE = {"up": "up", "series": "sr"}


def _vocabulary() -> dict[str, tuple[str, str]]:
    # 与说的话一样规范化（「一共」在识别结果里是「1共」）。
    words: dict[str, tuple[str, str]] = {}
    for word in _PLAIN_WORDS:
        words[match.normalize(word)] = ("plain", word)
    for word in _ASK_WORDS:
        words[match.normalize(word)] = ("ask", word)
    for word in _NOUN_WORDS:
        words[match.normalize(word)] = ("noun", "")
    for table, kind in ((_SITE_WORDS, "site"), (_KIND_WORDS, "kind"), (_GROUP_WORDS, "group")):
        for word, value in table.items():
            words[match.normalize(word)] = (kind, value)
    return words


_VOCABULARY = _vocabulary()
_LONGEST = max(len(word) for word in _VOCABULARY)


@dataclass(frozen=True)
class Category:
    """要列出的台：三个条件都满足的；全空表示「所有台」，先列分组。"""

    site: str = ""      # youtube / bilibili
    kind: str = ""      # up / series
    group: str = ""     # 设备列表里的分组名

    @property
    def everything(self) -> bool:
        return not (self.site or self.kind or self.group)

    def holds(self, channel: Channel) -> bool:
        kind = "up" if _CREATOR_ID.match(channel.id) else "series" if _SERIES_ID.match(channel.id) else ""
        # 只说网站（「我有哪些 YouTube 频道」「B站有哪些频道」）是问订阅的 UP 主（频道）；
        # 那个网站的剧集要说「剧」「剧集」（用户决定）。
        wanted = self.kind or ("up" if self.site and not self.group else "")
        return ((not self.site or channel.site == self.site) and (not wanted or kind == wanted)
                and (not self.group or channel.group == self.group))

    def noun(self) -> str:
        """「YouTube 频道」「B站 UP 主」「YouTube 剧集」「卫视」。"""
        noun = self.group
        if self.kind:
            noun = f"{noun} {_KIND_LABEL[self.kind]}" if noun else _KIND_LABEL[self.kind]
        noun, site = noun or "频道", _SITE_LABEL.get(self.site, "")
        # 「B站 UP 主」：中文后面接外文时空一格。
        return site + (" " if site and not site.endswith(" ") and noun[0].isascii() else "") + noun


def _words(text: str) -> list[tuple[str, str]] | None:
    """按词表切开；有切不出来的字返回 None。"""
    out, at = [], 0
    while at < len(text):
        for size in range(min(_LONGEST, len(text) - at), 0, -1):
            found = _VOCABULARY.get(text[at:at + size])
            if found is not None:
                out.append(found)
                at += size
                break
        else:
            return None
    return out


def _category_of(words: list[tuple[str, str]]) -> Category | None:
    """词里的类别；同一种类别说了两个不同的（「央视电影」）就不是在问一类台。"""
    found: dict[str, set[str]] = {"site": set(), "kind": set(), "group": set()}
    for kind, value in words:
        if kind in found:
            found[kind].add(value)
    if any(len(values) > 1 for values in found.values()):
        return None
    return Category(*(next(iter(found[k]), "") for k in ("site", "kind", "group")))


def _asked_category(text: str) -> Category | None:
    """问一类台的话：「我有哪些youtube频道告诉我」「央视有哪些台」「有什么可以看的」。"""
    words = _words(text)
    if not words:
        return None
    asks = [value for kind, value in words if kind == "ask"]
    if not asks:
        return None
    category = _category_of(words)
    if category is None:
        return None
    if category.everything and not any(kind == "noun" for kind, _ in words) and "有" not in text \
            and not any(value in _ASK_ALL for value in asks):
        # 只有「什么」「哪个」：不知道在问什么。
        return None
    return category


def _bare_category(text: str) -> Category | None:
    """只说了类别：「卫视」「YouTube 频道」「B站的UP主」「港台频道」。"""
    words = _words(text)
    if not words or any(kind not in ("site", "kind", "group", "noun") and value not in _BARE_PLAIN
                        for kind, value in words):
        return None
    category = _category_of(words)
    return None if category is None or category.everything else category


def list_ref(category: Category, offset: int) -> str | None:
    """「下一页」和分组行的 ref：ls:站.种类.分组序号:从第几个（设备限 31 个字符）。"""
    group = ""
    if category.group:
        if category.group not in GROUP_ORDER:
            return None
        group = str(GROUP_ORDER.index(category.group))
    return f"ls:{_SITE_CODE.get(category.site, '')}.{_KIND_CODE.get(category.kind, '')}.{group}:{offset}"


_LIST_REF = re.compile(r"^ls:(yt|bl|)\.(up|sr|)\.(\d{0,2}):(\d{1,3})$")


def parse_list_ref(ref: str) -> tuple[Category, int] | None:
    found = _LIST_REF.match(ref or "")
    if not found:
        return None
    site = {code: site for site, code in _SITE_CODE.items()}.get(found.group(1), "")
    kind = {code: kind for kind, code in _KIND_CODE.items()}.get(found.group(2), "")
    group = ""
    if found.group(3):
        index = int(found.group(3))
        if index >= len(GROUP_ORDER):
            return None
        group = GROUP_ORDER[index]
    return Category(site, kind, group), int(found.group(4))


def _page(rows: list[dict], category: Category, offset: int) -> tuple[list[dict], int, int]:
    """这一页的行（放不下时最后一行是「下一页」）、这一页从第几个开始、列出了几个。"""
    if offset >= len(rows):
        offset = 0          # 列表在两次之间变短了：从头列
    rest = rows[offset:]
    ref = list_ref(category, offset + PAGE_SIZE)
    if len(rest) <= CHOICES_MAX or ref is None:
        page = rest[:CHOICES_MAX]
        return page, offset, len(page)
    page = rest[:PAGE_SIZE] + [{"label": f"下一页 · 还有 {len(rest) - PAGE_SIZE} 个", "lead": "", "ref": ref}]
    return page, offset, PAGE_SIZE


def _group_order(catalog: Catalog) -> list[tuple[str, int]]:
    """有台的分组和台数，按设备列表的分组顺序。"""
    counts: dict[str, int] = {}
    for channel in catalog.channels:
        if channel.group:
            counts[channel.group] = counts.get(channel.group, 0) + 1
    known = [g for g in GROUP_ORDER if g in counts]
    return [(g, counts[g]) for g in known + [g for g in counts if g not in GROUP_ORDER]]


def list_decision(category: Category, catalog: Catalog, offset: int = 0, kind: str = "list") -> Decision:
    """列出一类台（每行换到那个台），或者（什么类别都没说）列出分组（每行打开那一组）。"""
    if category.everything:
        groups = [(g, n) for g, n in _group_order(catalog) if list_ref(Category(group=g), 0)]
        if not groups:
            return Decision("none", reply="频道表是空的")
        rows = [{"label": f"{g} · {n} 个台", "lead": "", "ref": list_ref(Category(group=g), 0)}
                for g, n in groups]
        page, start, shown = _page(rows, category, offset)
        reply = (f"一共 {sum(n for _, n in groups)} 个台，分 {len(groups)} 组" if start == 0
                 else f"第 {start + 1}-{start + shown} 组，共 {len(groups)} 组")
        return Decision(kind, reply=reply, choices=page)
    noun = category.noun()
    if category.site:
        relay_channels = [c for c in catalog.channels if _CREATOR_ID.match(c.id) or _SERIES_ID.match(c.id)]
        if relay_channels and not any(c.site for c in relay_channels):
            # 中转没回应（或是旧版本，不报网站）：分不出哪些是 YouTube 的。
            return Decision("none", reply="视频中转没回应，分不出是哪个网站的")
    members = [c for c in catalog.channels if category.holds(c)]
    if not members:
        return Decision("none", reply=f"{category.group}里还没有台" if category.group and not
                        (category.site or category.kind) else f"还没有 {noun}")
    rows = [{"label": _label(c.name), "lead": f"{c.number:02d}", "do": {"channel": c.id}} for c in members]
    page, start, shown = _page(rows, category, offset)
    if start:
        reply = f"{noun} 第 {start + 1}-{start + shown} 个，共 {len(members)} 个"
    elif category.group and not (category.site or category.kind):
        reply = f"{category.group}有 {len(members)} 个台"
    else:
        reply = f"{noun}有 {len(members)} 个"
    return Decision(kind, reply=reply, choices=page)


def _named_exactly(said: str, catalog: Catalog) -> bool:
    """说的话和某个台的名字或别名一字不差（只有这时分数超过 1，见 _channel_score）。"""
    ranked = rank_channels([said], catalog.channels)
    return bool(ranked) and ranked[0][0] > 1.0


def list_page(ref: str, catalog: Catalog) -> Decision | None:
    """设备选了「下一页」或一个分组（pick 的 ref）；ref 不认识时返回 None。"""
    parsed = parse_list_ref(ref)
    if parsed is None:
        return None
    return list_decision(parsed[0], catalog, parsed[1])


# ---- 搜索：「搜视频猫和老鼠」「油管搜频道lofi」「我想看猫和老鼠的视频」「找罗翔这个UP主」 ----
#
# 只在开头有明确的搜索动词（搜、搜索……；「找」还要说出视频或 UP 主），或者以「…的视频」结尾时才算，
# 免得抢了「我想看湖南卫视」「看体育」这些换台的话。网站默认 B 站，说了油管 / YouTube / 优兔是
# YouTube。是搜视频还是搜 UP 主没说清（「搜索李子柒」）时不猜，追问（search_question）。
# 结果、追问、选中 UP 主后的两个动作都是带 ref 的候选，ref 是「s<操作>:<第几次搜索>:<参数>」：
#   sq:G:v / sq:G:u   追问的回答：搜视频 / 搜 UP 主
#   sv:G:i / su:G:i   第 i 个视频 / UP 主（从 0 数）
#   sp:G:o            下一页，从第 o 个开始
#   sf:G:i / sl:G:i   第 i 个 UP 主：订阅为频道 / 播放最新视频

SEARCH_QUERY_MAX = 30
# 面板一行大约这么多格（汉字 2 格）：标题后面放得下时再加时长、粉丝数。
ROW_COLUMNS = 22
# 设备存一行文字的字节数（含结尾的 0 是 48）。
LABEL_BYTES = 47
REPLY_ASK = "要搜视频，还是搜 UP 主？"
_SITE_SHORT = {"bilibili": "B站", "youtube": "YouTube"}


def _alternation(words) -> str:
    return "|".join(sorted({re.escape(match.normalize(w)) for w in words}, key=len, reverse=True))


_SITE_BY_WORD = {match.normalize(word): site for word, site in _SITE_WORDS.items()}
_SITE_HEAD = re.compile(rf"^(?:在|到|去|用|从)?({_alternation(_SITE_WORDS)})(?:上面|上边|上|里面|里|中|的)*")
_SEARCH_VERB = re.compile(r"^(?:去|再|来)?(搜索|搜1搜|搜一搜|搜搜|搜寻|搜|查找|找找|找)")
_VIDEO_WORDS = ("视频", "影片", "片子")
_UP_WORDS = ("up主", "博主", "频道", "作者", "创作者", "主播", "账号", "用户", "youtuber")
_KIND_OF = {match.normalize(w): "video" for w in _VIDEO_WORDS} | {match.normalize(w): "up" for w in _UP_WORDS}
_KIND_HEAD = re.compile(rf"^(?:的)?({_alternation(_VIDEO_WORDS + _UP_WORDS)})(?:叫做|名叫|叫|是)?")
# 结尾的「up」单说也算（「搜罗翔up」）；开头不算，免得「搜uptown funk」被拆开。
_KIND_TAIL = re.compile(rf"(?:这个|那个|这位|那位|的)?(?:相关)?({_alternation(_VIDEO_WORDS + _UP_WORDS + ('up',))})$")
# 「我想看猫和老鼠的视频」「放一个lofi视频」：没有搜索动词，靠结尾的「的视频」。
_VIDEO_LEAD = re.compile(r"^(看看|看|来1个|来一个|来个|来点|来|放1个|放一个|放个|放|播放1个|播放一个|播放|"
                         r"播1个|播一个|播个|播)")
_COUNTED_LEADS = ("来1个", "来一个", "来个", "来点", "放1个", "放一个", "放个", "播放1个", "播放一个",
                  "播1个", "播一个", "播个")
_OF_VIDEO = re.compile(r"^(.+?)(?:的(?:相关)?视频|相关视频)$")
_PLAIN_VIDEO = re.compile(r"^(.+?)视频$")
_QUERY_HEAD = re.compile(r"^(?:1些|一些|些|1点|一点|点|几个|1个|一个|个|关于|有关|叫做|叫)")
_QUERY_TAIL = re.compile(r"(?:来看看|给我看看|看看|吧|呗)$")
_ASKING = re.compile(r"(什么|哪|啥|吗|多少|有没有)")
# 说了等于没说的词：「我想看点好看的视频」不是在搜「好看」。
_VAGUE = ("好看", "好玩", "有意思", "有趣", "随便", "随机", "任意", "别的", "其他", "其它")
# 规范化会去掉空格；外文词之间的空格搜索时要留着（「lofi girl」不是「lofigirl」），先换成这个字。
_SPACE = "\u2581"
_LATIN_GAP = re.compile(r"(?<=[A-Za-z0-9])\s+(?=[A-Za-z0-9])")


def _take_site(text: str, site: str) -> tuple[str, str]:
    found = _SITE_HEAD.match(text)
    if not found:
        return text, site
    return text[found.end():], _SITE_BY_WORD.get(found.group(1), site)


def _query(text: str) -> str:
    text = _QUERY_TAIL.sub("", _QUERY_HEAD.sub("", text)).strip(_SPACE).replace(_SPACE, " ")
    return "" if _ASKING.search(text) or len(text) > SEARCH_QUERY_MAX or text in _VAGUE else text


def search_text(transcript: str) -> str:
    """搜索用的规范化：和 decide 一样，只是外文词之间的空格留下（换成 _SPACE）。"""
    return _strip_fillers(match.normalize(_LATIN_GAP.sub(_SPACE, transcript or "")))


def search_request(text: str) -> dict | None:
    """search_text() 过的一句话 → {"site", "kind", "query"}；不是在搜索时为 None。

    kind 是 video / up，没说清时为空（要追问）；query 可能为空（「搜索」「搜视频」）。"""
    rest, site = _take_site(text, "")
    verb = _SEARCH_VERB.match(rest)
    if verb:
        weak = verb.group(1) in ("找", "找找", "查找")
        rest, site = _take_site(rest[verb.end():], site)
        kind = ""
        head = _KIND_HEAD.match(rest)
        if head:
            kind = _KIND_OF[head.group(1)]
            rest, site = _take_site(rest[head.end():], site)
        else:
            tail = _KIND_TAIL.search(rest)
            if tail and tail.start() > 0:
                kind = _KIND_OF.get(tail.group(1), "up")
                rest = rest[:tail.start()]
        # 「找」本身太泛（「帮我找湖南卫视」是换台）：要说出视频或 UP 主才是搜索。
        if weak and (not kind or _NOT_A_NAME.search(rest)):
            return None
        return {"site": site or "bilibili", "kind": kind, "query": _query(rest)}
    lead = _VIDEO_LEAD.match(rest)
    counted = bool(lead) and lead.group(1) in _COUNTED_LEADS
    if lead:
        rest, site = _take_site(rest[lead.end():], site)
    found = _OF_VIDEO.match(rest) or (_PLAIN_VIDEO.match(rest) if counted else None)
    if not found:
        return None
    query = _query(found.group(1))
    words = _words(query) if query else None
    if not query or (words is not None and all(kind in ("plain", "noun", "ask", "site") for kind, _ in words)):
        # 「看B站的视频」「看点好看的视频」：没有要搜的词。
        return None
    return {"site": site or "bilibili", "kind": "video", "query": query}


_SEARCH_EXAMPLE = {"video": ("视频", "搜视频猫和老鼠"), "up": (" UP 主", "搜UP主老番茄"),
                   "": ("", "搜索猫和老鼠")}


def _search(transcript: str) -> Decision | None:
    request = search_request(search_text(transcript))
    if request is None:
        return None
    if not request["query"]:
        what, example = _SEARCH_EXAMPLE[request["kind"]]
        return Decision("none", reply=f"要搜什么{what}？比如：{example}")
    return Decision("search", reply="正在搜索…", search=request)


# ---- 搜索的回答：追问、结果页、选中的 UP 主（session 记着搜到了什么，这里只管拼） ----

_SEARCH_REF = re.compile(r"^s([qvupfl]):(\d{1,4}):([0-9a-z]{1,3})$")


def search_ref(op: str, generation: int, arg) -> str:
    return f"s{op}:{generation}:{arg}"


def parse_search_ref(ref: str) -> tuple[str, int, str] | None:
    found = _SEARCH_REF.match(ref or "")
    return (found.group(1), int(found.group(2)), found.group(3)) if found else None


def _columns(text: str) -> int:
    return sum(1 if ord(ch) < 128 else 2 for ch in text)


def _plain(text: str) -> str:
    """设备的字库是 ASCII 加 GB2312：去掉表情、控制符这类画不出来的，空白并成一个。"""
    kept = []
    for ch in unicodedata.normalize("NFKC", text or ""):
        if ord(ch) > 0xFFFF or unicodedata.category(ch) in ("So", "Cs", "Co", "Cn", "Cc", "Cf"):
            kept.append(" ")
        else:
            kept.append(ch)
    return " ".join("".join(kept).split())


def _fit(text: str, size: int = LABEL_BYTES) -> str:
    """按 UTF-8 字节截短（设备再按像素加「…」）。"""
    raw = text.encode("utf-8")
    if len(raw) <= size:
        return text
    return raw[:size].decode("utf-8", "ignore").rstrip()


def row_label(main: str, extras: tuple[str, ...] = ()) -> str:
    """候选行的文字：画得出来的字，放得下时再接上 extras（「 · 12:34」），按设备的字节数截短。"""
    label = _plain(main)
    for extra in extras:
        if extra and _columns(label) + 3 + _columns(extra) <= ROW_COLUMNS:
            label = f"{label} · {extra}"
    return _fit(label)


def _clock(seconds: int) -> str:
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def _fans(count: int) -> str:
    for unit, size in (("亿", 100_000_000), ("万", 10_000)):
        if count >= size:
            value = f"{count / size:.1f}".removesuffix(".0")
            return f"{value}{unit}粉"
    return f"{count}粉" if count > 0 else ""


def _result_label(kind: str, item: dict) -> str:
    if kind == "video":
        duration = item.get("duration") if isinstance(item.get("duration"), int) else 0
        return row_label(str(item.get("title") or ""),
                          (_clock(duration) if duration > 0 else "", _plain(str(item.get("uploader") or ""))))
    fans, videos = item.get("fans"), item.get("videos")
    extra = _fans(fans) if isinstance(fans, int) and fans > 0 else \
        f"{videos}个视频" if isinstance(videos, int) and videos > 0 else ""
    return row_label(str(item.get("name") or ""), (extra,))


def search_noun(site: str, kind: str) -> str:
    """「B站视频」「YouTube 视频」「B站 UP 主」「YouTube 频道」。"""
    if kind == "video":
        return f"{_SITE_LABEL[site]}视频"
    return "YouTube 频道" if site == "youtube" else "B站 UP 主"


def search_page(site: str, kind: str, query: str, results: list[dict], offset: int,
                generation: int) -> Decision:
    """一页搜索结果：每行一个视频或 UP 主（ref 选中），放不下时最后一行是「下一页」。"""
    total = len(results)
    if not total:
        return Decision("none", reply=f"{_SITE_SHORT[site]}没搜到「{_label(query)}」")
    if offset >= total:
        offset = 0
    rest = results[offset:]
    shown = rest if len(rest) <= CHOICES_MAX else rest[:PAGE_SIZE]
    op = "v" if kind == "video" else "u"
    rows = [{"label": _result_label(kind, item) or "（没有标题）", "lead": f"{n + 1:02d}",
             "ref": search_ref(op, generation, n)} for n, item in enumerate(shown, offset)]
    if len(rest) > len(shown):
        rows.append({"label": f"下一页 · 还有 {len(rest) - len(shown)} 个", "lead": "",
                     "ref": search_ref("p", generation, offset + len(shown))})
    reply = (f"找到 {total} 个{search_noun(site, kind)}" if offset == 0
             else f"第 {offset + 1}-{offset + len(shown)} 个，共 {total} 个")
    return Decision("search", reply=reply, choices=rows)


def search_question(generation: int) -> Decision:
    """搜视频还是搜 UP 主没说清：问一句（面板留着，按键选或者直接说「视频」「UP主」）。"""
    return Decision("ask", reply=REPLY_ASK, choices=[
        {"label": "搜视频", "lead": "", "ref": search_ref("q", generation, "v")},
        {"label": "搜 UP 主", "lead": "", "ref": search_ref("q", generation, "u")}])


def creator_menu(item: dict, index: int, generation: int) -> Decision:
    """选中了一个 UP 主（频道）：订阅成台，还是播他最新的视频。"""
    name = _label(_plain(str(item.get("name") or ""))) or "这个 UP 主"
    return Decision("creator", reply=f"「{name}」：订阅还是看最新？", label=name, choices=[
        {"label": "订阅为频道", "lead": "", "ref": search_ref("f", generation, index)},
        {"label": "播放最新视频", "lead": "", "ref": search_ref("l", generation, index)}])


# 面板上有搜索的追问或结果时，下一句话可以直接回答（session 只在这时才问这几个函数）。
_ANSWER_LEAD = re.compile(r"^(?:我要|是|要|就|选|搜索|搜|找|看)+")
_ANSWER_KIND = re.compile(rf"^(?:的)?({_alternation(_VIDEO_WORDS + _UP_WORDS + ('up',))})(?:的|吧)?$")
_ORDINAL = re.compile(r"^(?:播放?|放|看|选|要|打开|订阅|就|我要)?第(\d{1,2})(?:个|条|项|位)?"
                      r"(?:视频|up主|博主|频道)?$")
_NEXT_PAGE = re.compile(r"^(?:下1页|下一页|下页|翻页|翻1页|翻一页|更多|还有|还有吗|后面的|再来几个|"
                        r"换1批|换一批|下1批|下一批)$")
_WANTS_LATEST = re.compile(r"(最新|新视频|播放|播1下|放1下|看看他|看他)")
_WANTS_FOLLOW = re.compile(r"(订阅|关注|添加|加为频道|加到频道|加成频道|收藏)")


def _said(transcript: str) -> str:
    return _strip_fillers(match.normalize(transcript))


def search_answer(transcript: str) -> tuple[str, str] | None:
    """对追问的回答：(video|up, 网站或空)。「视频」「搜UP主」「YouTube的视频」「第二个」。"""
    text = _said(transcript)
    number = pick_number(transcript)
    if number in (1, 2):
        return ("video" if number == 1 else "up"), ""
    text = _ANSWER_LEAD.sub("", text)
    text, site = _take_site(text, "")
    found = _ANSWER_KIND.match(text)
    if not found:
        return None
    return _KIND_OF.get(found.group(1), "up"), site


def pick_number(transcript: str) -> int | None:
    """「第二个」「播放第三个」「选第3个视频」→ 2、3、3。"""
    found = _ORDINAL.match(_said(transcript))
    return int(found.group(1)) if found and int(found.group(1)) >= 1 else None


def wants_next_page(transcript: str) -> bool:
    return bool(_NEXT_PAGE.match(_said(transcript)))


def creator_answer(transcript: str) -> str | None:
    """选中 UP 主后的回答：follow（「订阅」「第一个」）、latest（「播放最新的」「第二个」）。"""
    number = pick_number(transcript)
    if number in (1, 2):
        return "follow" if number == 1 else "latest"
    text = _said(transcript)
    if _WANTS_LATEST.search(text):
        return "latest"
    return "follow" if _WANTS_FOLLOW.search(text) else None


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
    # 搜索要以搜索动词开头或以「的视频」结尾，比音量、换台这些先认（搜的词里可能有「大声」）。
    decision = _search(transcript)
    if decision is not None:
        return decision
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
    decision = _playback(text, catalog, transcript)
    if decision is not None:
        return decision
    if _NEXT_CH.match(text) or _PREV_CH.match(text) or (_NEXT_EP.match(text) and not _relay_playing(catalog)):
        target = _step_channel(catalog, -1 if _PREV_CH.match(text) else 1)
        return Decision("none", reply=REPLY_UNKNOWN) if target is None else _channel_decision(target, catalog)
    asked = _asked_category(text)
    if asked is not None:
        return list_decision(asked, catalog)
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
    for said in dict.fromkeys((bare, _strip_verbs(text))):
        category = _bare_category(said)
        # 恰好是某个台的名字或说法（「电视剧」是 CCTV-8）时还是换台。
        if category is not None and not _named_exactly(said, catalog):
            return list_decision(category, catalog, kind="choices")
    queries = list(dict.fromkeys(q for q in (text, _strip_verbs(text), bare) if q))
    kind, picked = _pick(rank_channels(queries, catalog.channels))
    if kind == "one":
        return _channel_decision(picked[0], catalog)
    if kind == "some":
        return _choices(picked, f"找到 {len(picked)} 个，选一个")
    if text.startswith(_NAMING_VERBS) and 2 <= len(bare) <= 10 and bare not in _VERBS \
            and not _NOT_A_NAME.search(bare) and _words(bare) is None:
        # 「我要看葫芦娃」：显然是在点一个台，只是没有这个台（「看电视」「看看」不算）。
        return _not_found(bare, hint=True)
    return Decision("none", reply=REPLY_UNKNOWN)
