小鼠电视（RooTV，原名小智电视）电脑客户端（Linux，附带支持，测试版）
====================================================================

1. 解压到你的主目录：tar xzf rootv-*-linux-x86_64.tar.gz，再把文件夹改名成 ~/rootv。
2. 运行 ~/rootv/rootv。有托盘的桌面会出现托盘图标；没有托盘（服务器、没有桌面）时
   加 --no-tray，设置页的网址会打印在终端里，用同一个网络里的另一台电脑或手机打开。
3. 想开机自动运行：桌面上用托盘菜单的「登录时启动」；没有桌面时用 rootv.service（文件里有步骤）。
4. 想在桌面的应用列表里看到它：把 rootv.desktop 复制到 ~/.local/share/applications/，
   把 rootv.png 复制到 ~/.local/share/icons/。

数据在 ~/.local/share/rootv/，日志在 ~/.local/state/rootv/logs/。
从小智电视升级：先退出旧版（用了 xiaozhi-tv.service 的先 systemctl --user disable --now xiaozhi-tv），
第一次运行时会把 ~/.local/share/xiaozhi-tv/ 和旧日志搬过来，旧的自启项换成新的。
防火墙（ufw 等）要放行 TCP 8080、8097、8098，只对家里的网络。

RooTV PC client (formerly Xiaozhi TV; Linux, bonus platform, beta)
------------------------------------------------------------------
Unpack into your home folder as ~/rootv and run ~/rootv/rootv
(add --no-tray on a machine without a tray; the setup link is printed).
Data: ~/.local/share/rootv/. Logs: ~/.local/state/rootv/logs/.
Upgrading from Xiaozhi TV: quit it first; the first run moves
~/.local/share/xiaozhi-tv/ and the old logs over and replaces the old autostart entry.
Allow TCP 8080, 8097 and 8098 from your home network only.
