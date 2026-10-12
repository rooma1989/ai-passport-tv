"""多用户：账号、会话、邀请、设备绑定和每个用户的状态，存在一个 SQLite 文件里。

设计见 docs/superpowers/specs/2026-10-12-multi-tenant-design.md（第 4、8、9 节，阶段 1）。
中转（server/relay）用它分用户、分设备；电视服务（server/tv_server.py）用 gate.py 认设备。
只用标准库。
"""
