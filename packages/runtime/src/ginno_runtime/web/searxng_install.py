"""System-provided SearXNG install prompt（web-search 一键修复，2026-11-15）。

诊断面板「一键修复」点击后，前端建会话并把本 prompt 作为首条消息触发安装
turn（api/diagnostics.py 的 fix/web-search-searxng）。内容沉淀自一次真实排障：
国内 + ClashX 代理环境下 DDG 抓取端点被反爬验证码/连接重置双重夹击，改用
自托管 SearXNG 聚合引擎（bing 主力）恢复 web_search。

prompt 面向一个拥有 shell、能访问 sidecar API（127.0.0.1:8787）的全新代理
会话，自足、按步执行、遇网络抖动重试。改这里即改「系统提供的安装指令」。"""

from __future__ import annotations

SEARXNG_INSTALL_PROMPT = """你是 Ginno 的运维代理。任务：为本机安装并配置自托管 SearXNG，替代当前脆弱的 DuckDuckGo 抓取引擎，让 web_search 恢复可用。全程用中文汇报，结论先行。

【背景】Ginno 的 web_search 默认走 DuckDuckGo HTML 抓取端点（html.duckduckgo.com），频繁被反爬（返回 HTTP 202 验证码页）或连接重置，在国内 + 代理环境尤其严重。SearXNG 是免费自托管元搜索引擎，聚合多引擎返回 JSON，Ginno 原生支持（searxng 引擎，只需 base_url）。

【按步执行，每步先检查再动手；EOF / TLS 握手超时多为瞬时网络抖动，重试即可】
1. 前置检查：`docker version` 确认 Docker Desktop 在跑；`curl -s http://127.0.0.1:8787/api/health` 确认 sidecar 在线。Docker 没起就提示用户先启动 Docker Desktop。
2. 拉镜像：`docker pull searxng/searxng:latest`。若 EOF/超时（Docker Hub 被墙），改用国内镜像 `docker pull docker.m.daocloud.io/searxng/searxng:latest` 成功后 `docker tag` 回 searxng/searxng:latest。注意 Docker Desktop 守护进程拉镜像默认不走系统代理；若镜像源也失败，可在 Docker Desktop 设置手动代理 http://127.0.0.1:7890 并把国内镜像域名加入排除列表（改 settings.json 后需重启 Docker）。
3. 写配置 ~/searxng/settings.yml：`use_default_settings: true`；server 段 `limiter: false`、`public_instance: false`、随机 `secret_key`（openssl rand -hex 32）；search 段 `formats: [html, json]`；outgoing 段 `request_timeout: 9.0`、`max_request_timeout: 18.0`。检测宿主是否有 Clash 等代理（`lsof -nP -iTCP:7890 -sTCP:LISTEN`）：有则 outgoing.proxies 配 `all://: [http://host.docker.internal:7890]`。engines 段：启用 bing（实测最稳）、google/brave/duckduckgo/baidu/sogou 视可达性保留，禁用 wikipedia/wikidata/ahmia/torch（缺 contact_url 或需 tor）。
4. 运行：`docker run -d --name searxng --restart unless-stopped -p 8080:8080 -v ~/searxng/settings.yml:/etc/searxng/settings.yml:ro searxng/searxng:latest`（已存在同名容器先 docker rm -f）。
5. 验证：`curl -s 'http://127.0.0.1:8080/search?q=OpenAI&format=json'` 应返回 results>0（通常来自 bing）。若 0 结果，看响应里的 unresponsive_engines 和 `docker logs searxng`，据此调整引擎/代理后 `docker restart searxng` 重试。容器内可用 `docker exec searxng sh -c 'wget -q -O /dev/null --timeout=8 -e use_proxy=yes -e https_proxy=http://host.docker.internal:7890 https://www.baidu.com && echo OK'` 探代理连通性。
6. 配置 Ginno（务必走 API，勿直接写受保护的 ~/.ginno 文件）：先 `GET http://127.0.0.1:8787/api/settings` 取当前完整设置，合并 web 块为 {enabled:true, default_engine:"searxng", engines:{searxng:{base_url:"http://localhost:8080"}}, max_results:5, timeout_s:25}，再 `PUT /api/settings` 全量写回（PUT 是全文件替换，必须先 GET 再合并，绝不覆盖 providers/permissions 等其它块）。
7. 终验：`POST http://127.0.0.1:8787/api/web/test-search` body {"engine":"searxng"} 应返回 ok:true。

【收尾】向用户汇报：SearXNG 访问地址 http://localhost:8080、web_search 已切到 searxng（新会话生效；运行中的旧会话因工具缓存了旧配置，重启应用后切换）、以及哪些引擎可用。若某步因环境（如无 Docker、代理节点全挂）实在无法完成，明确说明卡点与用户需手动做的事，不要谎报成功。"""
