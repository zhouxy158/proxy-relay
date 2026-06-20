# proxy-relay-demo（学习用途）

演示「**重叠接力**」如何绕过 GitHub Actions 单 job 6 小时上限，把临时 CI 容器接力成一个准常驻服务。
载荷示例为 **Xray VLESS-WS + cloudflared 命名隧道**。

> ⚠️ **重要**：用 GitHub Actions 当常驻代理 **违反 GitHub 服务条款**（明确禁止把 Actions 当免费 serverless / 代理中转托管），
> 可能导致**账号封禁**。本仓库仅供一次性学习实验，请在**可丢弃的账号 + 公开仓库**上运行，被封即弃。

---

## 它怎么工作（这是你要学的核心）

```
旧 run:  |=============== 5h45m ==触发继任者== 6h|（被 GitHub 强杀）
新 run:                    开机→连同一隧道→ |==================== ...
                                  ↑ 这十几分钟新旧两个 cloudflared 同时连同一隧道
                                    Cloudflare 自动负载（HA），客户端无感，空窗≈0
```

三个关键点：

1. **重叠续期**：不是等 job 死了再起下一个，而是在 `5h45m`（`RELAY_AT=20700`）时就用 API 触发继任者，
   再重叠 `10min`（`OVERLAP=600`）让继任者完全接管，然后本段 flush 上报最后流量、清理停服退出（≈5h55m，早于 6h 硬杀）。
2. **必须用 PAT 自触发**：GitHub 默认的 `GITHUB_TOKEN` 触发的 `workflow_dispatch` **不会**产生新 run
   （防递归机制）。所以接力必须用一个带 `workflow` 权限的 **Personal Access Token**（secret `GH_PAT`）。
3. **状态在外部**：每段都是全新 VM，本地什么都不留。这里靠 **Cloudflare 命名隧道固定公网域名 + 固定 UUID**
   把「会话状态」放在 Cloudflare 侧，所以换机器客户端无需改配置。命名隧道原生支持**多 connector 并存**（HA），
   这正是重叠交接能无缝的原因。

---

## 部署步骤

### 1. Cloudflare：建命名隧道
前提：有一个**托管在 Cloudflare 的域名**（免费套餐即可，便宜域名也行）。

1. Cloudflare 控制台 → **Zero Trust** → **Networks → Tunnels** → **Create a tunnel** → 选 **Cloudflared**。
2. 给隧道起个名，创建后页面会显示安装命令，里面有 `--token eyJ....` 这串 **token**，复制下来。
3. 进入该隧道的 **Public Hostname** 标签 → **Add a public hostname**：
   - Subdomain/Domain：例如 `proxy.yourdomain.com`
   - Service：**Type = HTTP**，URL = `localhost:8080`（与工作流里的 `XRAY_PORT` 一致；本地明文，TLS 由 CF 边缘终结）
4. 确认该 hostname 的 DNS 是 **橙云代理（Proxied）** 状态。

### 2. GitHub：新建一个空仓库（在可丢弃的账号上）
把本目录下的内容（含 `.github/workflows/proxy-relay.yml`、`discover.py`）复制到该仓库**根目录**并推送。
**仓库设为 Public** —— 否则私有仓库每月只有 2000 分钟免费额度，24×7 约 1.4 天就用光；
公开仓库 Actions 分钟「无限」（这也正是该用法被滥用、会被风控盯上的原因）。

### 3. 配置 Secrets
仓库 → Settings → Secrets and variables → Actions → New repository secret：

| Secret | 必填 | 说明 |
|---|---|---|
| `CF_TUNNEL_TOKEN` | ✅ | 第 1 步复制的 cloudflared 隧道 token（`eyJ...`）。同时作为 VLESS uuid 的派生种子（`uuid5`，同一 token 永远得同一 uuid） |
| `CF_HOSTNAME` | ✅(注册需要) | Cloudflare 隧道公网域名，如 `proxy.yourdomain.com`。= 客户端连接地址 / SNI / Host 头。runner 内无法自动探测，故需手动传 |
| `GATEWAY_URL` | ✅(接入网关) | 网关**基址**（含你自己的私有路径前缀），如 `https://your-gateway.example.com/<你的前缀>`。代码只拼通用后缀 `/discover`(注册)、`/users`(拉用户)、`/traffic`(流量)。整段放 Secrets 避免公开仓库泄露 |
| `GH_PAT` | ✅(接力需要) | classic PAT 勾选 `workflow` 范围；或 fine-grained PAT 对本仓库授予 **Actions: Read and write**。没有它就只能跑单段 6h |

> 节点名称 `PROXY_NAME`（默认 `Github Actions`）和排序权重 `ORDER`（默认 `200`）直接写在工作流 `env` 里，
> 不想用 secret 可在那里改。

### 4. 启动
仓库 → **Actions** → 选 `proxy-relay` → **Run workflow**。它会自我接力下去。
（首次可先不配 `GH_PAT`，跑一段 6h 验证代理本身通不通，再加 `GH_PAT` 开启接力。）

### 5. 每小时任务：注册 + 用户同步 + 每用户出站 + 流量上报（discover.py 默认模式）
xray + cloudflared 起来约 25 秒后，**每小时**跑一次 `python3 discover.py`（仿 `cron.py`），一次做四件事：

**① 注册节点**
- 自动用 `ip-api.com` 查 runner 公网 IP → `countryCode` 转**国旗 emoji**；节点名 = `国旗 + PROXY_NAME`，如 **`🇺🇸 Github Actions`**
- 生成 VLESS-WS 的 qrcode + Clash 配置（`server`/`SNI`/`Host` 都用 `CF_HOSTNAME`），POST 到 `${GATEWAY_URL}/discover?order=200&ai=<自动>`（`ai` 按归属地：CN/HK/MO=false）

**② 用户同步（免重启）**
- 从 `${GATEWAY_URL}/users` 拉启用用户 `{userId,uuid,enabled[,proxy]}`，只取 enabled
- 用 xray **HandlerService API**（`xray api adu`/`rmu`）对运行中的 xray **动态增删用户，不重启**，对比 `inbounduser` 实时状态做增量
- 用户**不落盘**（xray.json 的 clients 为空），只活在运行中的 xray 内存里；crash 重启后下轮同步自动补回；拉取失败则不动现有用户

**③ 每用户专属 socks5 出站（免重启）**
- 用户带 `proxy`（`{type:socks5,addr[,username,password]}`）时，用 xray **RoutingService API**（`ado`/`rmo` + `adrules`/`rmrules`）给该用户下发专属 socks5 出站 + 路由规则（`tag=puo-<userId>`，按 user email 路由），**不重启**
- 无「列出出站」的 API，故用本地状态文件（`PUO_STATE`，默认 `puo_state.json`，**仅记 userId、不存凭证、不进日志**）算移除集；desired 每轮全量 upsert（覆盖地址/密码变更）
- 代理凭证同样**不落盘**，只在 xray 内存里

**④ 流量上报**
- xray 配置含 `StatsService`，用 `xray api statsquery -reset 'user>>>'` 读每用户上下行（读即清零=增量）
- POST 到 `${GATEWAY_URL}/traffic`（`{nodeId, ip, deltas:[{userId,up,down}]}`），无增量则跳过

> 未设置 `GATEWAY_URL` 时全部跳过，代理本身照常可用。`RoutingService` 是新加的——正在跑的旧棒没有它，出站同步会优雅失败（打 warning），**6h 后接力换新棒即生效**。

---

## 停止 / 清理
- 要停：Actions 里把工作流 **Disable**，并 cancel 正在跑的 run（注意继任者可能已排队，需一并取消）。
- 彻底干净：删仓库 + 在 Cloudflare 删隧道。

## 时间参数（写死在 Run 步骤里，按需改脚本）
| 参数 | 值 | 含义 |
|---|---|---|
| `RELAY_AT` | `20700` (5h45m) | 触发继任者的时刻 |
| `OVERLAP` | `600` (10min) | 接力后重叠时长：继任者接管 → flush 上报最后流量 → 清理停服退出本段 |
| `HOURLY` | `3600` (1h) | 每小时跑一次 注册+用户同步(免重启)+流量上报 |

> `XRAY_PORT`（默认 `8080`）仍在 `env` 里，需与 CF 隧道 Service 端口一致。

## 已知局限（也是学习点）
- 每段换 VM/IP，虽有重叠但偶发触发失败会断链，且无自动恢复——需手动 Run 一次重启。
- 公开仓库才有「无限」分钟，但日志/仓库公开 = 滥用更易被发现。
- 这套方案的稳定性、隐蔽性都远不如一台正经常驻机（Oracle 永久免费 ARM / Fly.io / VPS）。
