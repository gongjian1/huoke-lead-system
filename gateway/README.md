# 数据接口中转网关

把真正的 TikHub Key 只放在你自己的服务器上，给每个使用者发一个**子 Key**（`hk_` 开头）。

```
使用者的获客系统 ──(子 Key)──▶ 你的网关 ──(真 Key)──▶ TikHub
```

好处：

- 真 Key 不出你的服务器，不会随源码、数据库、聊天记录泄露。
- 每个子 Key 有独立的**评论条数额度、到期时间、每分钟限速**，随时可以停用、续费，不用换真 Key。
- 想让别人用采集功能，就得找你要子 Key。

网关只转发采集用到的各平台数据接口（抖音、小红书、快手、B 站、微博、知乎、微信、西瓜、头条、皮皮虾）。TikHub 的账号、余额、充值、Key 管理等接口**一律不转发**。

## 部署

需要和获客系统同一套 Python 环境（只依赖 Flask 和 gunicorn）。

```bash
cd /opt/huoke/gateway            # 代码随仓库一起，放在 gateway/ 目录
export TIKHUB_API_KEY=你的真Key
gunicorn -w 1 --threads 16 -b 127.0.0.1:5002 gateway:app
```

**必须只开 1 个进程**（`-w 1`）：每分钟限速记在内存里。线程数不用超过 16。

长期运行请用 systemd，真 Key 放在只有 root 能读的环境文件里：

```ini
# /etc/systemd/system/huoke-gateway.service
[Unit]
Description=Huoke data API gateway
After=network.target

[Service]
User=huoke
WorkingDirectory=/opt/huoke/gateway
EnvironmentFile=/etc/huoke-gateway.env     # 里面写 TIKHUB_API_KEY=...，chmod 600
ExecStart=/opt/huoke/venv/bin/gunicorn -w 1 --threads 16 -b 127.0.0.1:5002 gateway:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

然后用 nginx / OpenResty / 1Panel 把一个域名（或路径）反向代理到 `127.0.0.1:5002`，并**务必开 HTTPS**——子 Key 在请求头里传输。例如：

```nginx
location /gw/_admin/ { return 404; }   # 管理接口不对公网开放
location /gw/ {
    proxy_pass http://127.0.0.1:5002/;
    proxy_read_timeout 60;
    client_max_body_size 64k;
}
```

检查：`curl https://你的域名/gw/healthz` 应返回 `{"ok":true,"upstream_key_set":true}`。

## 管理子 Key

### 在获客系统的管理后台里管理（推荐）

管理后台 → **接口 Key**：填备注、可用评论条数、有效天数，点「生成子 Key」，复制 Key 和接口地址发给对方；列表里可以加额度、续期、停用、删除，能看到剩余额度和近 7 天用量。

要打开这个页面，需要让获客系统能访问网关的管理接口：在**网关和获客系统各自的环境文件里写同一个令牌**，然后重启两个服务。

```bash
# 生成一个随机令牌
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# 把同一行 GATEWAY_ADMIN_TOKEN=... 分别加进 /etc/huoke-gateway.env 和 /etc/huoke.env
systemctl restart huoke huoke-gateway
```

管理接口只接受本机直连：网关会拒绝任何经过反向代理（带 `X-Forwarded-For`）的请求，再加上令牌，公网上访问不到。建议在反代里也把 `/gw/_admin/` 直接 `return 404`。

### 或者用命令行

在网关服务器上（和网关用同一个 `GATEWAY_DB`，默认是 `gateway/gateway.db`）：

```bash
python manage.py create --note 张三 --quota 5000 --days 30   # 发一个：可用 5000 条评论，30 天有效
python manage.py create --note 内部 --unlimited              # 不限额度
python manage.py list                                        # 所有 Key 和用量
python manage.py topup 2 2000                                # 2 号 Key 加 2000 条评论额度
python manage.py disable 2                                   # 停用（enable 恢复，delete 删除）
python manage.py usage 2 7                                   # 2 号 Key 最近 7 天调了什么
```

子 Key 的明文**只在创建时显示一次**，库里只存哈希，丢了只能重新发一个。

额度怎么扣：额度的单位是**条评论**，卖的时候就说「筛选 1000 条评论 ≈ ¥X」。网关数的其实是请求，按平均每 1000 条评论约 500 次请求折算：**每次成功（2xx）的普通请求扣 2 条额度**（环境变量 `GATEWAY_UNITS_PER_REQUEST` 可调）；**微信类接口（视频号、公众号、微信搜索）每次扣 20 条**，因为 TikHub 对它们每次收 $0.01，是抖音等（$0.001）的 10 倍。额度不够一次扣的数量时会直接拒绝，不会多扣。上游报错、你的真 Key 欠费、超大请求等没有取到数据的情况都**不扣额度**。抖音实测：1 个关键词快速搜索（5 个作品 × 100 条）约扣 500 条，深度（20 × 200）约扣 4000 条；小红书、快手略多。这是估算，评论特别少的小作品会多花一点请求，上线后以「接口 Key」页里的真实用量为准。

## 使用者怎么接入

使用者登录自己的获客系统 → 管理后台 → 设置：

- **数据接口 Key**：填你发的 `hk_...`
- **接口地址**：填你的网关地址，例如 `https://你的域名/gw`

页面上"API 余额"处显示的是该子 Key **剩余的评论额度**（约 N 条评论），不是美元。

## 安全说明

- 日志只记路径、状态码和耗时，**不记查询参数**（里面是客户搜的关键词）；保留 30 天。
- 使用者看不到上游报错细节：你的真 Key 欠费或失效时，他们只会看到"上游数据服务暂时不可用，请联系提供者"，同时网关日志里会有记录，你可以及时充值。
- 这是一个"按评论条数卖数据接口"的网关，不是防滥用系统：想限制某个人的用量，调小他的额度和限速即可。

## 配置项（环境变量）

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TIKHUB_API_KEY` | 无 | 你的真 Key，必填 |
| `GATEWAY_DB` | `gateway/gateway.db` | 子 Key 和用量日志的库，请备份、不要上传 |
| `GATEWAY_UPSTREAM` | `https://api.tikhub.io` | 上游地址 |
| `GATEWAY_UNITS_PER_REQUEST` | `2` | 1 次普通请求扣几条评论额度（微信类再乘 10） |
