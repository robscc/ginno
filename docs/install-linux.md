# Linux 安装指南（Install on Linux）

> 适用：在 Linux 服务器 / VPS 上部署 **`ginno node`**（headless A2A 节点），
> 供主 Ginno（macOS 桌面版）跨机调度。本文全程沉淀自一台真实 VPS 的部署
> （Ubuntu 22.04.3 LTS，ginno-runtime 0.3.3），关键命令均在该机上核验；
> 标注「未核验」的命令为标准用法，谨慎照抄。
> Linux 上只有 Python runtime 可用——桌面版没有 Linux 包（见文末「形态边界」）。

## 0. 部署形态一览

本文采用的布局（下文命令与之一致）：

| 项 | 值 |
|---|---|
| 系统用户（跑服务，不可登录） | `ginno`（shell `/usr/sbin/nologin`） |
| Python | 3.12（uv 托管，装在 `/opt/uv-python/`；**Ubuntu 22.04 自带 3.10，不满足 ≥3.11 要求**） |
| venv | `/opt/ginno-venv`（uv 创建，**默认不含 pip**，安装一律走 `uv pip install --python`） |
| CLI 入口 | `/usr/local/bin/ginno` → `/opt/ginno-venv/bin/ginno`（symlink） |
| 数据目录（GINNO_HOME） | `/var/lib/ginno-node`（属主 `ginno`，700） |
| 任务工作区（exec_root） | `/srv/ginno-a2a-workspace`（属主 `ginno`） |
| 密钥环境文件 | `/etc/ginno-node/env`（600，root:root；目录 700） |
| systemd 单元 | `ginno-node.service`，仅监听 `127.0.0.1:8790` |
| 内存 | 1G 级小机即可（实测常驻 ~50MB），**建议配 2G swap** |

## 1. 安装

### 1.1 系统用户与目录

```bash
# 服务专用系统用户：不可交互登录，家目录仅作占位（GINNO_HOME 另指 /var/lib）
useradd --system --create-home --shell /usr/sbin/nologin ginno
# 核验（真机值：ginno:x:997:997::/home/ginno:/usr/sbin/nologin）
getent passwd ginno

# 数据目录与任务工作区
install -d -m 700 -o ginno -g ginno /var/lib/ginno-node
install -d -o ginno -g ginno /srv/ginno-a2a-workspace
```

### 1.2 Python ≥ 3.11（uv 托管）

Ubuntu 22.04 系统 Python 是 3.10，低于 ginno-runtime 的 `requires-python >= 3.11`；
用 uv 装独立的 3.12，不污染系统 Python（Ubuntu 24.04 自带 3.12，可省此步——
**未在 24.04 上核验**）：

```bash
# uv 本体（官方安装脚本，装到 ~/.local/bin；本部署机核验为 uv 0.13.8）
curl -LsSf https://astral.sh/uv/install.sh | sh

# Python 3.12 装到统一位置（真机布局：/opt/uv-python/cpython-3.12-linux-x86_64-gnu/）
UV_PYTHON_INSTALL_DIR=/opt/uv-python uv python install 3.12

# venv（真机 /opt/ginno-venv/pyvenv.cfg 核验：uv = 0.13.8, version_info = 3.12）
UV_PYTHON_INSTALL_DIR=/opt/uv-python uv venv /opt/ginno-venv --python 3.12
/opt/ginno-venv/bin/python --version    # → Python 3.12.16（真机值）
```

注意：`uv venv` 默认**不装 pip**——不要试图 `/opt/ginno-venv/bin/pip`，不存在。
后续安装/升级一律 `uv pip install --python /opt/ginno-venv/bin/python …`。

### 1.3 构建 wheel 并安装（正规路径）

在**开发机**（仓库根目录）构建：

```bash
cd packages/runtime
uv build
# 产物（真机部署时为 0.3.3，本文示例用当前源码版本号）：
#   dist/ginno_runtime-0.4.0-py3-none-any.whl
```

上传并在 VPS 安装：

```bash
scp dist/ginno_runtime-0.4.0-py3-none-any.whl root@<vps>:/tmp/

# VPS 上：
uv pip install --python /opt/ginno-venv/bin/python /tmp/ginno_runtime-0.4.0-py3-none-any.whl

# 全局 CLI 入口（真机核验：lrwxrwxrwx /usr/local/bin/ginno -> /opt/ginno-venv/bin/ginno）
ln -s /opt/ginno-venv/bin/ginno /usr/local/bin/ginno
ginno --version    # → ginno 0.3.3（真机部署时版本；随源码版本走）
```

### 1.4 swap（1G 小机建议加）

真机实测：933Mi 内存 + 2G `/swapfile`，服务常驻 ~50MB，但 LLM 长任务 /
多任务并发时余量吃紧。真机现状核验（`swapon --show`）：

```
NAME       TYPE SIZE   USED    PRIO
/swapfile  file 2G     35.1M   -2
```

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile          # 真机核验：-rw------- root root
mkswap /swapfile
swapon /swapfile
```

⚠️ 真机当时**漏写了 fstab 持久化行**（重启后 swap 不会自动挂载，靠 systemd
的 `swapfile.swap` unit 撑着）。新部署建议补上（此行真机未执行）：

```bash
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

## 2. 配置

### 2.1 密钥环境文件（600，root 独占）

模型网关 API key 不进任何 JSON / 日志，放独立 env 文件，systemd 以
`EnvironmentFile` 注入。settings.json 里用 `env:GINNO_NODE_API_KEY` 引用
（真机 settings.json 核验存在该引用）：

```bash
install -d -m 700 -o root -g root /etc/ginno-node
cat > /etc/ginno-node/env <<'EOF'
GINNO_NODE_API_KEY=<YOUR-TOKEN>
EOF
chmod 600 /etc/ginno-node/env
# 核验（真机值：-rw------- 1 root root /etc/ginno-node/env；目录 drwx------）
ls -l /etc/ginno-node /etc/ginno-node/env
```

### 2.2 settings.json（GINNO_HOME 下）

首次启动 `ginno node` 会自动生成 `/var/lib/ginno-node/settings.json` 并把
CLI 参数合并写回。需要手工补两处：

**模型 provider**——`model_configs` 里 api_key 用 env 引用（同桌面版格式）：

```json
"model_configs": [
  {
    "name": "your-model",
    "base_url": "https://<your-gateway>",
    "api_key": "env:GINNO_NODE_API_KEY"
  }
]
```

**A2A 节点策略**——`connectors.a2a` 段（真机值脱敏后照抄；`trust`/`caps`
默认 fail-closed 为 `readonly`/`["L0"]`，按需放宽）：

```json
"connectors": {
  "a2a": {
    "trust": "normal",
    "caps": ["L0", "L1", "L2", "L3"],
    "a2a_enabled": true,
    "inbound_enabled": true,
    "bind_mode": "loopback",
    "bind_interface": "",
    "port": 8790,
    "node_enabled": true,
    "node_name": "Ginno Node (vps)",
    "inbound_exec_root": "/srv/ginno-a2a-workspace"
  }
}
```

trust 取值与 caps 分级的语义见 A2A 连接器文档 / `connectors/a2a.py` 的
fail-closed 规整逻辑；不确定就保持 `readonly` + `["L0"]`（默认值）。

### 2.3 能力地址（Capability Address）

节点首次启动会自生成能力 token 并打印完整地址（**明文只出现这一次**，服务端
只存 SHA-256 hash，之后重启不再回显）：

```
http://<host>:8790/a2a/<capability-token>
```

把该地址填进主 Ginno 的 A2A 连接器即完成配对。**务必在首次启动后立刻从日志
捞**（见 §4 日志命令）；丢了只能重置 token 重新签发。

## 3. 服务化（systemd）

`/etc/systemd/system/ginno-node.service`——真机单元原文（无敏感值，可照抄；
`Documentation=` 一行指向的是部署机的文档路径，可按需删改）：

```ini
# /etc/systemd/system/ginno-node.service
# ginno node —— systemd 单元
# 设计：node 进程「不 fork、不 daemonize，交给 systemd/launchd 管」。
# 长跑纪律：node 自身阻塞至 SIGTERM/SIGINT，所以 systemd 直接 Type=simple 即可。

[Unit]
Description=Ginno headless A2A node (push-mode A2A server)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/ginno-node/env
Type=simple
User=ginno
Group=ginno

# GINNO_HOME：node 的状态/凭据落点（settings.json、a2a/tokens.json 只存 hash、
# a2a/inbound-token 0600 明文、identity 私钥 0600）。
# 等价于 CLI 的 --home；优先级 CLI > 本环境变量 > ${HOME}/.ginno-node。
Environment=GINNO_HOME=/var/lib/ginno-node

WorkingDirectory=/srv/ginno-a2a-workspace

ExecStart=/usr/local/bin/ginno node \
  --listen :8790 \
  --root /srv/ginno-a2a-workspace \
  --name "Ginno Node (vps)"

# 起服即打印能力地址（node.py 里逐行 flush，正是为了 nohup/systemd 块缓冲下
# 地址能立刻可见）。首次签发的明文 token 只出现这一次，务必先捞日志：
#   journalctl -u ginno-node -b 0 | grep -A1 能力地址
StandardOutput=journal
StandardError=journal

Restart=on-failure
RestartSec=5
TimeoutStopSec=30          # 给 SIGTERM 走完优雅停
KillMode=mixed
KillSignal=SIGTERM

# 资源与权限加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
# 只有这两处可写：GINNO_HOME（凭据/会话）+ exec_root（任务产物）
ReadWritePaths=/var/lib/ginno-node /srv/ginno-a2a-workspace
MemoryMax=1.5G

[Install]
WantedBy=multi-user.target
```

`--listen :8790` 空主机名意味着 **loopback 绑定**（`ginno node --help` 原文：
"localhost/empty implies loopback"），这是安全设计的根基。启用：

```bash
systemctl daemon-reload
systemctl enable --now ginno-node
# 核验（真机值）：is-active → active；is-enabled → enabled
systemctl is-active ginno-node && systemctl is-enabled ginno-node
```

日志（真机核验输出形态）：

```bash
journalctl -u ginno-node -n 20 --no-pager   # 最近 20 行
journalctl -u ginno-node -f                  # 跟随（未在本机长跑核验，journalctl 标准用法）
```

正常启动的日志长这样（真机摘录）：

```
ginno node: listening on http://127.0.0.1:8790 (loopback)
home=/var/lib/ginno-node exec_root=/srv/ginno-a2a-workspace trust=normal caps=L0,L1,L2,L3
能力地址已签发过，明文 token 不再回显（服务端只存 SHA-256 hash）。
地址（token 掩码）：http://127.0.0.1:8790/a2a/****
```

## 4. 网络与防火墙（两种模式）

### 模式 A（推荐）：纯环回 + SSH 隧道

**节点只绑 127.0.0.1，公网零暴露**；ufw 只放行 SSH。这是本部署机采用的
形态（真机 `ufw status` 核验）：

```
Status: active

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW       Anywhere
22/tcp (v6)                ALLOW       Anywhere (v6)
```

从你的电脑建隧道（`-f` 后台驻留；保活参数防 NAT/防火墙掐空闲连接——
命令形态已实测隧道可用，收到节点 401 应答即证明穿透）：

```bash
ssh -i ~/.ssh/<your-key> \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
    -f -N -L 18790:127.0.0.1:8790 root@<vps>

# 之后本机访问 127.0.0.1:18790 即节点（能力地址里的 host:port 相应替换）
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:18790/api/health
# → 401（节点鉴权应答，证明隧道已通；带 token 才会 200）
```

仓库还提供 `ginno tunnel` CLI（建隧道 + 断线自动重连一条龙，默认本地
18790 → 远端 8790）：

```bash
ginno tunnel --host <vps> --user root --key ~/.ssh/<your-key> \
             --local-port 18790 --remote-port 8790 --token <capability-token>
```

（参数取自 `ginno tunnel --help` 实测输出；`--token` 给了会顺带打印
本地可用能力地址。）

### 模式 B：ufw 放行 8790（LAN/公网直连，未在本部署采用）

需要非 SSH 客户端直连时才用；节点监听地址也要相应改（`--listen 0.0.0.0:8790`
或 `--bind-mode lan`）。**以下 ufw 规则未在真机启用**，为 ufw 标准用法：

```bash
ufw allow 22/tcp     # 先保 SSH，再动别的（见 §6 坑 1）
ufw allow 8790/tcp
ufw enable
```

公网直连务必配 TLS（`ginno node --tls-cert/--tls-key`）并收紧 ufw 来源
（`ufw allow from <主控机IP> to any port 8790 proto tcp`）。

## 5. 验证清单

逐项核验（命令均实测；`<capability-token>` 替换为 §2.3 捞到的明文 token）：

```bash
# ① 服务状态：active (running)，CGroup 里是 /usr/local/bin/ginno node …
systemctl status ginno-node --no-pager -l

# ② 只绑环回（真机值：LISTEN 127.0.0.1:8790，users:(("ginno",...))）
ss -tlnp | grep 8790

# ③ Agent Card：带能力 token 应 200，不带/错 token 一律 401（实测三态：
#    200 / 401 / 401；401 响应体统一 {"error":"unauthorized"}，不泄露差异）
CAP="http://127.0.0.1:8790/a2a/<capability-token>"
curl -s -o /dev/null -w "%{http_code}\n" "$CAP/.well-known/agent-card.json"   # → 200
curl -s "$CAP/.well-known/agent-card.json" | head -c 200
# 响应开头（结构实测；name/version 随部署——name 来自 --name，VPS 为
# "Ginno Node (vps)"，version 即 ginno --version）：
#   {"name":"…","version":"…","protocolVersion":"1.0.0",
#    "capabilities":{"streaming":true,"pushNotifications":false},...}

# 隧道模式下把 host:port 换成 127.0.0.1:18790 即可（穿透实测同上三态）
```

④ **跨机任务**：主 Ginno 的 A2A 连接器里添加能力地址（隧道模式下用
`http://127.0.0.1:18790/a2a/<token>`），发一条任务，回到 VPS 看
`/var/lib/ginno-node/a2a/inbound-audit.jsonl` 有新记录、
`inbound-progress-*.jsonl` 出现（真机均有）。

## 6. 常见坑

1. **ufw 零放行就 `enable` → 当场锁死 SSH**。真机的正确姿势是先
   `ufw allow 22/tcp` 再 `enable`（§4 模式 A 的 status 输出即终态）。
   万一锁死只能靠 VPS 商家的救援控制台。
2. **关 `PasswordAuthentication` 前先确认密钥登录可用**。真机 sshd 核验：
   `PasswordAuthentication no` + `PermitRootLogin prohibit-password`
   （/etc/ssh/sshd_config 与 sshd_config.d 均如此）。顺序错了同样锁死。
3. **venv 里没有 pip**：`uv venv` 默认不装。装/升级一律
   `uv pip install --python /opt/ginno-venv/bin/python <wheel>`，
   别混用系统 pip。真机环境实测正常组合：anyio 4.16.0 + fastapi 0.163.1
   （site-packages dist-info 核验）；若手工乱升级 anyio 出现 task/cancel
   相关报错，回到 wheel 声明的版本。
4. **能力地址明文 token 只在首启日志出现一次**（服务端只存 SHA-256 hash）。
   首启后立刻 `journalctl -u ginno-node -b 0 | grep -A1 能力地址` 捞存。
5. **代理/网络拦截**：node 出站调模型网关被拦时，先分离层排查——
   `curl -sS -o /dev/null -w "%{http_code}\n" https://api.github.com`
   （真机实测 → 200）确认出站正常，再直测你的网关 endpoint；服务侧报错看
   `journalctl -u ginno-node`。通用排障见
   [`troubleshooting.md`](troubleshooting.md)。
6. **系统 Python 太老**：Ubuntu 22.04 是 3.10，`import ginno_runtime`
   直接报错（要求 ≥3.11）。按 §1.2 用 uv 装 3.12，别动系统 Python。

## 7. 形态边界

- **Linux 仅支持 Python runtime / headless**：`ginno node`（本篇）、
  `ginno exec`（一次性任务，见 [`headless.md`](headless.md)）、
  `ginno tunnel`。无桌面 UI。
- **Tauri 桌面包只有 macOS**（dmg）。Linux 桌面版在
  [`ROADMAP.md`](../ROADMAP.md) 中为「评估中、无承诺」（原文：*"Windows &
  Linux builds — Tauri is cross-platform; the Python sidecar needs per-OS
  PyInstaller validation. Evaluation in progress; no promise before it
  survives dogfooding."*）——**当前不提供，也没有时间表**。
- Windows：同上，未提供。

## 附录：源码直拷 + SHA-256 比对（降级方案）

没有构建环境时，可把源码文件直接 scp 进 venv 的 site-packages（本部署机
在 wheel 路径定型前曾这么过渡，改动文件用逐一 SHA-256 比对验收）。升级个别
文件时同理：

```bash
# 开发机：算源文件 hash（真机部署时在 macOS 上执行）
shasum -a 256 packages/runtime/src/ginno_runtime/a2a/node.py

# VPS：算已部署文件 hash，两边比对
sha256sum /opt/ginno-venv/lib/python3.12/site-packages/ginno_runtime/a2a/node.py
```

注意：直拷绕过了依赖声明与 entry-point 生成——只适合**热修个别 .py**；
整包安装/升级请走 §1.3 的 wheel。改动清单建议落盘保存（本次部署的临时
清单脚本放在 /tmp，已被系统清理，未留档——前车之鉴）。
