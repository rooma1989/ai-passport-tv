"""台标补齐：频道表和节目单都没给台标的台，按台名到一个公开的台标库里找（logos.LogoCache 的最后一层）。

用的是 GitHub 上的 fanmingming/live（GPL-3.0，维护得勤，tv/ 下九百多个台标，文件名就是台名：CCTV1.png、
湖南卫视.png），经 jsDelivr 的镜像取（https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/<文件名>）：项目自己的
live.fanmingming.cn 换过好几次域名，jsDelivr 的地址格式多年不变，国内也连得上。我们不带走它的图片，只在用到时由中转
按 LogoCache 的规矩取（只取公网、只收图片、有大小上限、存在 logos/ 里），和频道表里写的台标一样。

- 文件名单（FILES）是 2026-10-28 从仓库的 tv/ 目录抄下来的：只对名单里有的名字发请求，不会对着一个台一个台地
  去碰 404。名单旧了也不要紧，最多少几个台标；以后的台标仓库加了文件，更新这份名单就行。
- 对名字用节目单的规整（epg.key_of：全角转半角、大写、去掉括号里的说明、结尾的「高清」「HD」，央视只留台号），
  先比原样，再比规整过的，再查 ALIASES（常见的别名：上海卫视 → 东方卫视、CCTV 新闻 → CCTV13 …），4K 台没有自己的
  台标时用不带 4K 的。台名、节目单里的台名（tvg-name）、节目单编号（tvg-id）依次试。
- 总后台频道表里能关掉（enabled / set_enabled，存在账号库的 meta 里，默认开着）；关了一个请求也不往台标库发。
"""

from __future__ import annotations

import urllib.parse

from . import epg

NAME = "fanmingming/live"
HOME = "https://github.com/fanmingming/live"
BASE = "https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/"
# 账号库 meta 里的开关："0" 是关；没有、别的值都是开着。
META_KEY = "logos.public"

# 台标库 tv/ 下的文件（不带 .png 的写了扩展名），用 | 和换行隔开。
FILES = """
4K电影|AMC|AQTV|AXN|ArirangTV|BBCEarth|BBCLifestyle|BBCWORLDNEWSASIA|BLUEAntExtreme|BOOMERANG
BeautifulLife|BloombergTV|BlueAntEntertainment|CBN幸福剧场|CCTV1|CCTV10|CCTV11|CCTV12|CCTV13|CCTV14
CCTV15|CCTV16|CCTV17|CCTV2|CCTV3|CCTV3D|CCTV4|CCTV4K|CCTV4欧洲|CCTV4美洲|CCTV5+|CCTV5|CCTV6|CCTV7|CCTV8
CCTV8K|CCTV9|CETV1|CETV2|CETV3|CETV4|CGTN|CGTNDoc|CGTN俄语|CGTN法语|CGTN纪录|CGTN西语|CGTN记录|CGTN阿语|CHC动作电影
CHC家庭影院|CHC影迷电影|CHC高清电影|CI罪案侦查频道|CLTV|CNBC|CNBCASIA财经台|CNC|CNN|CNNNews|CYZHTV|CrimeInvestigation
CtiEntertainment|CtiVariety|DMAX|DOX-CINEMA|DOXTV|DOX_YAQU|DOX_xinzhi|DW|Discovery-Asia|DiscoveryHD
Discovery科学|Disney|DisneyJunior|EBCDrama|EBCFinancialNews|EBCMovies|EBCNews|EBCSuper|EBCVariety
EBCWesternMovies|EBCYoYoTV|EFTV|ELEVENsports1|ELTA_Ent|ELTV生活英语台|EVE|EbcAsiaWeishi|FOXMOVIES
FOXSports|FOXSports2|FXHD|FoodNetwork|FoxFamilyMovies|FoxLive|FoxSports3|GDTV3|GDTV4|GDTV6|GDTV8
GDTVECO|GDTVENT|GDTVMOV|GDTVSHAO|GMTV|GTVDramaHD|GTVEntertainment|GTVOneHD|GTVVarietyHD|GlobalNews
GoldSunTV|G导视|HBLS|HDZHTV|HISTORY|HUBEI2|HUBEI3|HUBEI4|HUBEI5|HUBEI7|HUBEI8|History2|HistoryChannel
History历史频道|IFUN动漫台|IHOT爱世界|IHOT爱体育|IHOT爱动漫|IHOT爱历史|IHOT爱喜剧|IHOT爱奇谈|IHOT爱娱乐|IHOT爱家庭|IHOT爱幼教|IHOT爱怀旧
IHOT爱悬疑|IHOT爱探索|IHOT爱旅行|IHOT爱时尚|IHOT爱极限|IHOT爱江湖|IHOT爱浪漫|IHOT爱猎奇|IHOT爱玩具|IHOT爱电竞|IHOT爱科学|IHOT爱科幻
IHOT爱经典|IHOT爱美食|IHOT爱解密|IHOT爱谍战|IHOT爱赛车|IHOT爱都市|IHOT爱院线|IHOT爱青春|IPTV3+|IPTV5+|IPTV6+|IPTV8+|IPTVEFEL
IPTV少儿动画|IPTV热播剧场|IPTV相声小品|IPTV经典电影|IPTV魅力时尚|J2|JCYS|JETVariety|JMTV|JNDSTV|JNJYTV|JNSETV|JNSHTV
JNTV|JNWLTY|JNYLTV|JZTV|LNSY1|LNTV-FINANCE|LNTV-SPORT|LNTV2|LNTV3|LNTV5|LNTV6|LNTV7|LNTV8|LQTV
LSTIME|LSTV|LXZHTV|Lifetime|LoveNature|MAX极速汽车|MTV|MTVLive|MyCinemaEurope|NEWTV东北热剧|NEWTV中国功夫
NEWTV军事评论|NEWTV军旅剧场|NEWTV古装剧场|NEWTV家庭剧场|NEWTV惊悚悬疑|NEWTV明星大片|NEWTV欢乐剧场|NEWTV海外剧场|NEWTV潮妈辣婆|NEWTV炫舞未来
NEWTV精品体育|NEWTV精品大剧|NEWTV精品纪录|NEWTV精品萌宠|NEWTV超级体育|NEWTV超级电影|NEWTV超级电视剧|NEWTV超级综艺|NEWTV金牌综艺
NEXTTVMovie|NEXTTVZonghe|NHKWorld|NOW新闻台|Nickelodeon|PDTV|PiliPuppet|PublicTV|PublicTV2|PublicTV3HD
QTV|QTV1|QTV2|QTV3|QTV4|QTV5|QTV6|QZTV-1|QZTV-2|QZTV-3|QZTV-4|QZTV|RTDoc|RTHK31|RTHK32|RTNews|SGSCTV
SGTV|SinDaTV|SinJiTVHD|SkyNews|Smart知识台|StarChineseMovies|StarMoviesHD|TACT|TOPTVHD|TRACESportStars
TRT-World|TVB DRAMA|TVB 经典台|TVB+|TVBJ2|TVBPlus|TVBS|TVBS亚洲|TVBS新闻|TVBS欢乐|TVBS精采|TVBclassic|TVB明珠台
TVB星河|TVB翡翠台|TaiwanPlus|ThaiPBS|TienLiangTVHD|UniqueNews|UniqueUSTVHDUSTVHD|VOA美国之音|VideolandDrama
VideolandJapanese|VideolandMaxTV|VideolandMovies|VideolandOnTV|VideolandSportsHD|WETV|WFGXTV|WFKWTV
WFTV1|WFTV2|WFTV3|WFTV4|WHTV1|WHTV2|WHTV3|WHTV4|WHTV5|WHTV6|WHTV7|WILD PLANET|WarnerTV|ZCTV|ZChannel
abcaus|afc|aljazeera|anhui|asiatravel|asiazonghe|beijing|beijingjishi|bestv|bingtuan|catchplay|cdtv1
cdtv2|cdtv3|cdtv4|cdtv5|cdtv6|channelnewsasia|channelv|chongqing|cinemaworld|cmusic|cnex|cqccn
discovery|diyicaijing|dongfang|dongnan|dox_juchang|dox_xinyi|dox_yijia|dox_yinglun|doxyinghua
dreamworks|eata_sports1|eata_sports3|elevensports2|elta_sports2|elta_yingju|elta_zonghehd|eltv
emeidianying|etoday|euronews|eurosport|eyelvyou|eyexiju|fashionone|fashiontv|fenghuangxianggang
fenghuangzhongwen|fenghuangzixun|fooldplanet|france24|ftv-1|ftv-taiwan|ftv-zongyi|gansu|gstv
gtv_youxi|guangdong|guangxi|guizhou|hks|hongkongkai|huanyucaijing|huanyuzonghe|huayi_yingju|huayimbc
hubei|hunan|itv|jiajiakt|jiangsu|jiangxi|jilin|jinwenjiang|jinyingjishi|jinyingkatong|jisuqiche|kaku
liaoning|longhuadonghua|loupe|meilizuqiu|mtvlivehd|natgeo|natgeowild|natlgeo|nhkworldp|ningxia
outdoor|qicaixiju|qinghai|quanjishi|roller|sansha|sctv2|sctv3|sctv4|sctv5|sctv7|sctv8|sctv9|shandong
shanghaidongfangyingshi|shanghaijishi|shanghaiwaiyu|shangshixinwen|shanxi|shenghuoshishang|shengming
shenzhen|sichuan|starmov|starmovieyule|starsports|tfc|tianjin|tlc|traceurban|travelchannel|tv5monde
tvN|tvbfinanceinformationchannel|viutv|viutv6|wangluoqipai|weilaojingcai|wolvesvalley|wuxingtiyu
xingfucai|youxiancaijingzixun|youxianxinwen|youxifengyun|zgjt|七彩戏剧|三佳购物|三沙卫视|上海外语|上海教育|上海第一财经|上海都市
上视新闻|世界地理|东南卫视|东方卫视|东方影视|东方财经|东方购物|东莞新闻综合|东风卫视|中华特产|中华美食|中国交通|中国天气|中国教育1台|中国教育2台|中国教育3台|中国教育4台|中天新闻台
中学生|中视|中视新闻|中视新闻台|中视经典|中视菁采|中视购物|之江纪录|乌兰察布|乌海新闻综合|乌海都市生活|乐游|书画|云南卫视|云南娱乐|云南影视|云南都市|五星体育|亚洲新闻|京视剧场
人间卫视|优优宝贝|优漫卡通|优购物|佛山公共|佛山影视|佛山综合|先锋乒羽|全纪实|公视|公视戏剧台|兴安|兵器科技|兵团卫视|内蒙古IPTV|内蒙古农牧|内蒙古卫视|内蒙古少儿|内蒙古文体娱乐
内蒙古新闻综合|内蒙古经济生活|内蒙古蒙语卫视|内蒙古蒙语文化|农林卫视|凤凰中文|凤凰卫视中文台|凤凰卫视电影台|凤凰卫视资讯台|凤凰卫视香港台|凤凰资讯|动漫秀场|劲爆体育|包头新闻综合
包头生活服务|包头经济频道|北京IPTV4K|北京体育休闲|北京卫视|北京卫视4K|北京影视|北京文艺|北京新闻|北京生活|北京纪实科教|北京财经|北京青年|半岛新闻|华数4K|华视|华视新闻
华语影院|南宁公共|南宁影视娱乐|南宁新闻综合|南方卫视|卡酷少儿|卫生健康|卫视中文|卫视中文台|厦门1|厦门2|厦门3|厦门卫视|发现之旅|台视|台视新闻|台视综合|台视财经|吉林卫视|呼伦贝尔
呼伦贝尔文化旅游|呼伦贝尔新闻综合|呼伦贝尔生活资讯|呼和浩特|呼和浩特影视娱乐|呼和浩特新闻综合|呼和浩特都市生活|咪咕体育|咪咕视频|咪咕音乐|哈哈炫动|哒啵电竞|哒啵赛事|嘉佳卡通|四川乡村
四川卫视|四川妇女儿童|四川峨眉电影|四川影视文艺|四川文化旅游|四川新闻|四川科教|四川经济|四海钓鱼|国学|增城电视台|大湾区卫视|大爱一台|大爱电视|天元围棋|天津卫视|央广购物|央视台球
央视高网|女性时尚|好享购物|宁夏卫视|安多卫视|安徽公共|安徽农业科教|安徽卫视|安徽国际|安徽影视|安徽经济生活|安徽综艺体育|家家购物|家庭理财|家有购物|寰宇新闻|山东体育|山东农科|山东卫视
山东少儿|山东教育|山东文旅|山东新闻|山东生活|山东综艺|山东齐鲁|山西卫视|山西影视|山西文体生活|山西社会与法治|山西经济|山西黄河|岭南戏曲|巴彦淖尔影视娱乐|巴彦淖尔新闻综合
巴彦淖尔经济生活|年代ERANewsHD|年代MUCH|幸福娱乐|幸福空间居家台|广东4K|广东体育|广东公共|广东卫视|广东卫视4K|广东嘉佳卡通|广东少儿|广东广电|广东影视|广东新闻|广东民生
广东珠江|广东经济科教|广东综艺|广州影视|广州新闻|广州法治|广州竞赛|广州综合|广西乐思购|广西卫视|广西国际|广西影视|广西新闻|广西移动|广西综艺旅游|广西都市|康巴卫视|廉江综合|延边卫视
弈坛春秋|影迷数位电影|影迷电影|影迷纪实|快乐垂钓|快乐购|怀旧剧场|成都公共|成都少儿|成都影视文艺|成都新闻综合|成都经济资讯|成都蓉城先锋.jpg|成都都市生活|摄影|收藏天下|文化精品
文物宝库|新动漫|新疆体育健康|新疆卫视|新疆少儿|新疆汉语经济|新疆汉语综艺|新视觉|无线新闻台|无线财经台|早期教育|时尚购物|明珠台|星空卫视|春晚|晴彩中原|智林体育台|杭锦旗综合|梧州公共
梧州教育生活|梧州新闻综合|欢笑剧场|欢笑剧场4K|欢腾购物|武术世界|每日影院|民视|民视台湾台|民视新闻台|民视旅游台|民视第一台|民视综艺台|求索动物|求索生活|求索科学|求索纪录|汕头经济
汕头综合|江苏休闲体育|江苏优漫卡通|江苏公共新闻|江苏卫视|江苏国际|江苏城市|江苏影视|江苏教育电视台|江苏综艺|江西卫视|江西少儿|江西教育|江西新闻|江西经济生活|汽摩|河北卫视|河南4K
河南乡村|河南公共|河南卫视|河南国际|河南新闻|河南梨园|河南民生|河南法治|河南电视剧|河南都市|泉州一套|泉州三套|泉州二套|泉州四套|泉州影视|泉州新闻|泉州都市生活|法治天地|泰州一套
泰州三套|泰州二套|浙江卫视|浙江国际|浙江少儿|浙江教科影视|浙江数码时代|浙江新闻|浙江民生休闲|浙江经济生活|浦东电视台|海南公共|海南卫视|海南少儿|海南文旅|海南经济|海峡卫视|海豚综合
深圳卫视|深圳卫视4K|深圳都市|清华|游戏风云|湖北卫视|湖南卫视|湖南国际|湖南娱乐|湖南教育|湖南爱晚|湖南电影|湖南电视剧|湖南经视|湖南都市|湖南金鹰卡通|漳州新闻|潮州综合|澳亚卫视
澳视澳门|澳门莲花|爱上4K|爱尔达娱乐台|爱看导视|环球奇观|环球旅游|环球电视台|甘肃卫视|生态环境|生活时尚|电竞.jpeg|电竞天堂|电视指南|百姓健康|睛彩广场舞|睛彩竞技|睛彩篮球
睛彩青少|福州导视|福州少儿|福州影视|福州新闻|福州生活|福建公共|福建少儿|福建教育|福建文体|福建新闻|福建旅游|福建电视剧|福建经济|福建综合|移动戏曲.PNG|第一剧场|精品导视|精选
纪实人文|纯享4K|置业频道|翡翠台|耀才财经|老故事|聚鲨环球精选|芒果互娱|苏州4K|茶|莆田新闻|莲花卫视|西安1|西安丝路频道|西安商务资讯|西安影视频道|西安教育|西安新闻综合|西安都市频道
西藏卫视|西藏藏语卫视|视纳华仁纪实|视纳华仁纪实频道|证券服务|财富天下|贵州卫视|赤峰|赤峰影视娱乐|赤峰新闻综合|赤峰经济服务|足球频道|车迷|辽宁公共|辽宁北方|辽宁卫视|辽宁影视|辽宁生活
辽宁都市|辽宁青少|达拉特旗综合|通辽城市服务|通辽新闻综合|通辽蒙语频道|都市剧场|鄂尔多斯|鄂尔多斯新闻综合|鄂尔多斯经济服务|鄂尔多斯蒙语频道|采昌影剧|重广融媒|重庆卫视|重庆少儿|重庆影视
重庆文体娱乐|重庆新农村|重庆新闻|重庆时尚生活|重庆汽摩|重庆社会与法|重庆科教|重庆移动|重温经典|金砖电视|金色学堂|金鹰卡通|金鹰纪实|钱江都市|阿拉善|陕西体育休闲|陕西公共|陕西卫视
陕西影视|陕西新闻资讯|陕西生活|陕西西部电影|陕西都市青春|陶瓷|青海卫视|靓妆|靖天卡通|靖天国际|靖天戏剧|靖天日本|靖天日本台|靖天映画|靖天欢乐|靖天电影|靖天电影台|靖天综合|靖天育乐
靖天资讯|靖洋卡通|靖洋戏剧|风云剧场|风云足球|风云音乐|风尚生活|风尚购物|香港卫视|香港国际财经台|高尔夫网球|魅力足球|黑莓动画|黑莓电影|黑龙江卫视|龙华偶像|龙华影剧|龙华戏剧|龙华日韩
龙华日韩台|龙华洋片|龙华电影|龙华经典
"""

# 常见的别名 → 台标库里的文件名（不带 .png）。键按 epg.key_of 规整后比，所以「CCTV-13 新闻」这类已经对得上的不用写。
ALIASES = {
    "CCTV综合": "CCTV1", "CCTV财经": "CCTV2", "CCTV综艺": "CCTV3", "CCTV中文国际": "CCTV4", "CCTV体育": "CCTV5",
    "CCTV体育赛事": "CCTV5+", "CCTV电影": "CCTV6", "CCTV国防军事": "CCTV7", "CCTV军事": "CCTV7", "CCTV电视剧": "CCTV8",
    "CCTV纪录": "CCTV9", "CCTV科教": "CCTV10", "CCTV戏曲": "CCTV11", "CCTV社会与法": "CCTV12", "CCTV新闻": "CCTV13",
    "CCTV少儿": "CCTV14", "CCTV音乐": "CCTV15", "CCTV奥林匹克": "CCTV16", "CCTV农业农村": "CCTV17",
    "CCTV4K超高清": "CCTV4K", "CCTV8K超高清": "CCTV8K",
    "上海卫视": "东方卫视", "东方": "东方卫视", "福建卫视": "东南卫视", "福建东南卫视": "东南卫视", "旅游卫视": "海南卫视",
    "内蒙卫视": "内蒙古卫视", "黑龙江": "黑龙江卫视", "兵团": "兵团卫视",
    "凤凰卫视": "凤凰卫视中文台", "凤凰中文台": "凤凰卫视中文台", "凤凰资讯台": "凤凰卫视资讯台",
    "凤凰卫视资讯": "凤凰卫视资讯台", "凤凰香港": "凤凰卫视香港台", "凤凰香港台": "凤凰卫视香港台",
    "凤凰卫视香港": "凤凰卫视香港台", "凤凰电影": "凤凰卫视电影台", "凤凰电影台": "凤凰卫视电影台",
    "CGTN英语": "CGTN", "CGTNNEWS": "CGTN", "CGTN新闻": "CGTN", "CGTN英文": "CGTN", "CGTN纪录片": "CGTN纪录",
    "CGTNDOCUMENTARY": "CGTN纪录", "CGTN西班牙语": "CGTN西语", "CGTN阿拉伯语": "CGTN阿语",
}

_INDEX: tuple[dict[str, str], dict[str, str]] | None = None


def _file(entry: str) -> str:
    return entry if "." in entry else entry + ".png"


def _stem(entry: str) -> str:
    return entry.rsplit(".", 1)[0] if "." in entry else entry


def _index() -> tuple[dict[str, str], dict[str, str]]:
    """({原样的名字（不分大小写）: 文件名}, {规整过的名字: 文件名})；规整后撞了的留原样就是那个键的，再留短的。"""
    global _INDEX
    if _INDEX is None:
        plain, keyed = {}, {}
        entries = [e.strip() for e in FILES.replace("\n", "|").split("|") if e.strip()]
        for entry in sorted(entries, key=lambda e: (len(_stem(e)), e)):
            stem = _stem(entry)
            plain.setdefault(stem.casefold(), _file(entry))
            key = epg.key_of(stem)[0]
            if key and (key not in keyed or key == stem.upper()):
                keyed[key] = _file(entry)
        for alias, stem in ALIASES.items():
            found = plain.get(stem.casefold())
            if found:
                keyed.setdefault(epg.key_of(alias)[0], found)
        _INDEX = plain, keyed
    return _INDEX


def file_for(*names) -> str:
    """台名（和节目单里的台名、编号）→ 台标库里的文件名；都对不上时空串。"""
    plain, keyed = _index()
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        found = plain.get(name.strip().casefold())
        if found:
            return found
        key = epg.key_of(name)[0]
        found = keyed.get(key) or keyed.get(epg.uhd_base(key))
        if found:
            return found
    return ""


def url_for(*names) -> str:
    found = file_for(*names)
    return BASE + urllib.parse.quote(found) if found else ""


def enabled(relay) -> bool:
    """总后台开着「台标补齐」没有（默认开着）。读一次记在中转上。"""
    cached = getattr(relay, "_logoset_on", None)
    if cached is None:
        accounts = getattr(relay, "accounts", None)
        cached = relay._logoset_on = accounts is None or accounts.meta(META_KEY, "1") != "0"
    return cached


def set_enabled(relay, on: bool) -> None:
    relay.accounts.set_meta(META_KEY, "1" if on else "0")
    relay._logoset_on = bool(on)
