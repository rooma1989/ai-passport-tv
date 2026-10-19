小智电视电脑客户端（Linux，附带支持，测试版）
============================================

1. 解压到你的主目录：tar xzf xiaozhi-tv-*-linux-x86_64.tar.gz，再把文件夹改名成 ~/xiaozhi-tv。
2. 运行 ~/xiaozhi-tv/xiaozhi-tv。有托盘的桌面会出现托盘图标；没有托盘（服务器、没有桌面）时
   加 --no-tray，设置页的网址会打印在终端里，用同一个网络里的另一台电脑或手机打开。
3. 想开机自动运行：桌面上用托盘菜单的「登录时启动」；没有桌面时用 xiaozhi-tv.service（文件里有步骤）。
4. 想在桌面的应用列表里看到它：把 xiaozhi-tv.desktop 复制到 ~/.local/share/applications/，
   把 xiaozhi-tv.png 复制到 ~/.local/share/icons/。

数据在 ~/.local/share/xiaozhi-tv/，日志在 ~/.local/state/xiaozhi-tv/logs/。
防火墙（ufw 等）要放行 TCP 8080、8097、8098，只对家里的网络。

Xiaozhi TV PC client (Linux, bonus platform, beta)
-------------------------------------------------
Unpack into your home folder as ~/xiaozhi-tv and run ~/xiaozhi-tv/xiaozhi-tv
(add --no-tray on a machine without a tray; the setup link is printed).
Data: ~/.local/share/xiaozhi-tv/. Logs: ~/.local/state/xiaozhi-tv/logs/.
Allow TCP 8080, 8097 and 8098 from your home network only.
