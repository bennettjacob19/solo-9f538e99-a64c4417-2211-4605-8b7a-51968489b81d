# DNS 值班解析系统（serve-stale 演练）

平台值班场景：内部服务的权威 DNS 失联时，解析器继续为已确认的地址提供解析，
但记录被删除（NXDOMAIN）后绝不把旧地址交给调用方。

## 组成

| 服务 | 模块 | 端口 | 说明 |
|---|---|---|---|
| `auth` | `app.auth_server` | UDP 5354 | 本地权威端，行为可由网页实时切换 |
| `resolver` | `app.resolver` | UDP 5353 | 缓存/serve-stale UDP 解析器 |
| `web` | `app.web` | HTTP 8000 | FastAPI 操作网页 |

三者通过共享卷上的 SQLite（WAL 模式）协作：`control` 表存权威端行为，
`cache` 表存解析器缓存（**绝对时间戳**），`query_log`/`auth_log` 表供网页展示。

## 运行

```bash
docker compose up --build
# 网页:  http://localhost:8000
# 查询:  dig @127.0.0.1 -p 5353 service.example A
```

无 Docker 的本地运行（等价三进程）：

```bash
pip install -r requirements.txt
DB_PATH=/tmp/dns/app.db AUTH_PORT=15354 python -m app.auth_server &
DB_PATH=/tmp/dns/app.db RESOLVER_PORT=15353 AUTH_PORT=15354 python -m app.resolver &
DB_PATH=/tmp/dns/app.db RESOLVER_PORT=15353 WEB_PORT=8000 python -m app.web &
```

自测（自动拉起三进程，跑全部关键交错，约 110 秒）：

```bash
python tests/selftest.py
```

## 语义（默认值，可用环境变量调整）

- 成功答复 TTL = **5 秒**（`FRESH_TTL`），缓存命中时 TTL 倒数。
- 过期后重新回源；上游超时（`UPSTREAM_TIMEOUT`=2s）时旧 A 最多沿用 **20 秒**
  （`STALE_WINDOW`），应答 TTL 为剩余可用时长；超过即 **SERVFAIL**。
- **NXDOMAIN 只作 5 秒新鲜缓存，绝不作过期备用**：过期+上游超时 → SERVFAIL。
- 解析器确认 NXDOMAIN 时**立即持久清除旧 A**：此后任何答复都不再含旧 A。
- 每次回源用**独立 socket + 随机事务 ID + 问题匹配**，超时后迟到的旧答复
  无人接收、也不会被错配给后续查询，因此无法复活旧记录。
- 缓存期限以绝对 epoch 存于 SQLite，**重启不延长旧 A 的有效期**
  （窗口继续倒数，不会重新计时）。

网页可切换权威端行为：`a`（可改 IP）/ `nxdomain` / `delay`（延迟应答，默认 3s，
大于上游超时，用于演练迟到应答）/ `timeout`（不应答，模拟权威端失联）。

**延迟模式的收包快照语义**：权威端对每个收到的查询包只读取一次 `control` 表，
把当时已提交的模式/地址/延迟整体快照给该请求。延迟从**该请求在权威端的收包时刻**
起算，到点**只应答一次**；等待期间在网页上改地址或延迟，不影响仍在等待的在途请求
（它仍按提交时的旧地址、旧延迟应答），新配置只对之后收到的包生效。
权威端日志以 `delayed`（收包快照）+ `answered`（到点单次应答）成对记录每次延迟应答。

**网页表单的字段级乐观锁**：每 2 秒自动刷新只更新“生效值”，不会覆盖本页未保存的
草稿（草稿有标记并保留）；点「应用」只提交本页改动过的字段，每个字段附带开始编辑时
的生效值作为基线。两页改不同字段时互不冲突、分别生效；若某字段当前生效值同时不同于
开始编辑时的基线值和本页拟保存的值，则**整次提交拒绝**（HTTP 409，不写入任何字段），
页面列出冲突项的生效值并保留全部草稿，核对后可直接重提。拟保存值恰好等于新生效值
（两页收敛）时不算冲突。

- 新接口 `POST /api/control/apply`，请求体 `{"changes": {字段: 新值},
  "expected": {字段: 基线值}}`，字段限 `mode` / `ip` / `delay_seconds`；
  成功返回 `{"ok": true, "control": <本次保存的完整配置>}`，
  冲突返回 `{"ok": false, "error": "conflict", "conflicts": {字段: 生效值},
  "control": <当前完整配置>}`。
- 旧 `POST /api/mode` 保持可用：缺省的 `ip` / `delay_seconds` 按**提交时**的生效值
  补齐，成功响应同样返回本次保存的完整配置。

网页“发起查询”经解析器发真实 DNS 查询，展示实际答复（rcode、应答、TTL）
与来源（`upstream` / `cache-fresh` / `cache-stale` / `none`，由解析器在
additional 段 TXT `_src.<域名>` 中带回），并展示双方日志。

固定域名默认 `service.example`（`FIXED_DOMAIN` 可改）。解析器只服务该域名，
其余名称 REFUSED，非 A 查询返回 NODATA。

## 文件

```
app/common.py       配置 + SQLite 状态面
app/auth_server.py  可控权威端（dnslib 编解码，线程并发）
app/resolver.py     缓存/serve-stale 解析器
app/web.py          FastAPI 操作网页
tests/selftest.py   端到端自测（A–H 八组场景）
Dockerfile / docker-compose.yml / requirements.txt
```
