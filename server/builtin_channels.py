"""内置兜底频道：没有频道表文件时电视服务用它们（server/live.py 的 load_channels）。

取电视台自有的公开流。单独放一个文件，是为了自建镜像不带它：.dockerignore 排除这个文件，
镜像的内容检查（tools/selfhost_content_check.py）也查这些地址不在镜像里。自建模式本来就不用内置频道
（TV_NO_BUILTIN_CHANNELS=1）。
"""

BUILTIN_CHANNELS = {
    "cgtn": ("CGTN", "https://english-livebkali.cgtn.com/live/encgtn.m3u8"),
    "france24": ("France 24",
                 "https://live.france24.com/hls/live/2037218/F24_EN_HI_HLS/master_500.m3u8"),
    "dw": ("DW English",
           "https://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/master.m3u8"),
    "tagesschau": ("Tagesschau24",
                   "https://tagesschau.akamaized.net/hls/live/2020115/tagesschau/tagesschau_1/master.m3u8"),
}
