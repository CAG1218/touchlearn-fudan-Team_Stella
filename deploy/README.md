# 固定托管：ECS 还是 VPS？

先给结论：**ECS 和 VPS 在这个项目里没有技术差别**。两者都是你租到的一台 Linux 虚拟机，区别主要在厂商叫法、控制台和计费方式。真正决定体验的是**配置、地区、磁盘是否持久、有没有 HTTPS 域名**，而不是“ECS”这三个字母。

触见的服务端是纯 CPU 的 Python 进程，模型理解走外部 API，几何计算在本地。**不需要 GPU**，也不需要 Kubernetes。一台 2 核 2G、40GB 盘的轻量云服务器就够长期展示；1 核 1G 能跑，但 Docker 构建容易吃紧。

> 如果你的目标是“中国大陆任意地区、任意手机或电脑、任意网络都能直接打开”，请先看 [mainland.md](mainland.md)：需要大陆服务器 + 已备案域名 + 443 HTTPS，里面有大厂选型、备案材料、时间线和大陆特有坑。

## 一、选型建议

| 方案 | 适合 | 不适合 | 说明 |
|---|---|---|---|
| 轻量应用服务器（腾讯云 / 阿里云） | 本项目的默认选择 | 需要复杂 VPC、负载均衡时 | 控制台简单，自带防火墙和流量包，价格通常低于同配 ECS |
| ECS / CVM | 学校或实验室已有云账号、要统一管理 | 个人只为一个小服务 | 能力更全，但多数能力本项目用不上 |
| 海外 VPS（DigitalOcean、Vultr、Hetzner 等） | 没有备案、想快速上线 | 对大陆访问延迟敏感 | 香港/新加坡节点通常 30–80ms，欧美节点更慢 |
| Render / Railway | 先看效果、不想管服务器 | 长期展示 | 免费实例会休眠；免费盘不持久，重启后历史作业丢失 |
| Cloudflare Quick Tunnel | 临时演示 | 固定托管 | 依赖你的电脑开机，地址每次变 |

### 推荐配置

- **CPU**：2 核。几何构建是单进程串行，2 核足够；4 核不会明显更快。
- **内存**：2GB。Python 服务常驻约 150–300MB，构建峰值取决于网格复杂度；1GB 在 `docker build` 阶段可能被 OOM 杀掉。
- **磁盘**：40GB SSD。镜像约 400–600MB，系统与 Docker 占 5–8GB，其余留给 `outputs/`。一个作业通常几 MB 到几十 MB。
- **带宽**：3–5Mbps 足够。上传限制是单文件 30MB、单次 80MB，下载主要是 STL 和 ZIP。
- **系统**：Ubuntu 22.04 / 24.04 LTS，或 Debian 12。不要用 Windows Server。

### 地区与备案

- **大陆节点**：访问最快，但域名走 80/443 必须完成 ICP 备案，否则会被拦截。用 IP 直接访问可以临时绕过，但不适合正式展示，也没有可信 HTTPS。
- **香港 / 新加坡节点**：不需要备案，绑定域名即可签发证书；大陆访问延迟略高，但比赛演示足够。
- **如果学校能提供已备案域名**：优先大陆节点 + 备案域名，体验最好。
- **如果没有备案**：选香港轻量服务器，不要为了省钱买大陆节点再想办法绕备案。

### 费用量级

以下只是量级，具体以厂商当期价格为准：

- 轻量服务器：活动价常见几十元到一百多元一年；常规价约 24–60 元/月。
- 域名：`.com` 约 60–100 元/年，`.cn` 通常更便宜但备案要求更明确。
- LLM / VLM API：按 token 计费，演示阶段通常每月几元到几十元，取决于图片数量和模型。
- 对象存储备份：可选，几元/月起。

**不建议**为了这个项目买 GPU 服务器。模型调用是 API，GPU 只会增加成本。

## 二、部署架构

```
Internet
   │  80 / 443
   ▼
Caddy 2（自动申请和续期 Let's Encrypt 证书）
   │  反向代理到内网 8765
   ▼
touchlearn 容器（Python HTTP 服务）
   │
   ▼
命名卷 touchlearn-outputs → /app/outputs
```

- 只有 Caddy 暴露 80/443；应用端口 8765 不对外。
- `outputs/` 放在 Docker 命名卷里，重新部署容器不会丢作业。
- 健康检查走 `/api/health`，Caddy 等应用健康后才启动。
- 访问码由 `TOUCHLEARN_ACCESS_CODE` 控制；作业产物按不可猜测的 12 位 ID 公开，方便扫码。

## 三、从零部署

### 1. 准备

- 一台 Ubuntu 22.04/24.04 服务器，公网 IP；
- 一个域名，例如 `touch.example.com`；
- 安全组/防火墙放行 22、80、443（TCP），443 同时放行 UDP 以启用 HTTP/3；
- 不要对公网开放 8765。

### 2. 解析域名

在 DNS 服务商添加 A 记录：

```
touch.example.com  →  你的服务器公网 IP
```

等待解析生效：

```bash
dig +short touch.example.com
```

### 3. 安装 Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo systemctl enable --now docker
docker --version
docker compose version
```

### 4. 取代码

```bash
git clone https://github.com/CAG1218/touchlearn-fudan-Team_Stella.git
cd touchlearn-fudan-Team_Stella
```

如果服务器访问 GitHub 不稳定，可以在本地打包后 `scp` 上传，或使用镜像加速；不要把带密钥的 `.env` 提交到仓库。

### 5. 配置环境变量

```bash
cd deploy
cp .env.example .env
```

编辑 `.env`，至少改这三项：

```dotenv
SITE_ADDRESS=touch.example.com
TOUCHLEARN_PUBLIC_URL=https://touch.example.com
TOUCHLEARN_ACCESS_CODE=TL-换成你自己的强随机码
```

生成访问码：

```bash
python3 -c "import secrets; print('TL-'+secrets.token_hex(4).upper())"
```

需要图片理解和自由场景生成时，再填 `TOUCHLEARN_LLM_*` / `TOUCHLEARN_VLM_*`。密钥只存在服务器的 `.env` 里，`deploy/.env` 已被 gitignore。

### 6. 启动

```bash
bash deploy.sh
```

脚本会检查 Docker、拒绝占位符配置、构建镜像、启动应用和 Caddy，并打印健康检查结果。首次启动需要几分钟构建。

### 7. 验证

```bash
curl -fsS https://touch.example.com/api/health
```

浏览器打开 `https://touch.example.com`，输入访问码，跑一遍“函数图像 → 生成 → 下载 STL”。

证书首次签发通常需要几十秒；如果失败，先确认 DNS 已生效、80/443 没有被占用、云防火墙已放行。

## 四、日常运维

### 更新版本

```bash
cd touchlearn-fudan-Team_Stella
bash deploy/backup.sh          # 先备份作业
git pull
bash deploy/deploy.sh          # 重新构建并滚动替换
```

`outputs` 在命名卷里，更新不会删除历史作业。数据库没有，迁移成本为零。

### 备份

```bash
bash deploy/backup.sh
```

产物在 `deploy/backups/touchlearn-outputs-<时间>.tar.gz`，脚本默认清理 14 天前的归档。建议再用 `scp` 拉到另一台机器或对象存储——**同机备份不算备份**。

恢复：

```bash
docker run --rm \
  -v touchlearn-outputs:/data \
  -v "$PWD/deploy/backups:/backup" \
  alpine sh -c "rm -rf /data/* && tar xzf /backup/touchlearn-outputs-XXXX.tar.gz -C /data"
```

compose 文件固定了卷名 `touchlearn-outputs`；如果你的环境改过卷名，先用 `docker volume ls` 确认。恢复会先清空当前产物，执行前确认归档可用且已另行备份。

### 查看状态与日志

```bash
cd deploy
docker compose --env-file .env ps
docker compose --env-file .env logs -f --tail=200 touchlearn
docker compose --env-file .env logs -f --tail=100 caddy
```

Caddy 访问日志在 `touchlearn-caddy-data` 卷的 `/data/access.log`，已按 20MB 轮转、保留 5 份。

### 重启与停止

```bash
docker compose --env-file .env restart touchlearn
docker compose --env-file .env down          # 停止，保留卷
docker compose --env-file .env down -v       # 连卷一起删，慎用
```

`down -v` 会删除全部作业产物，执行前先备份。

## 五、安全清单

- [ ] `TOUCHLEARN_ACCESS_CODE` 是强随机值，不是 `TL-XXXXXXXX`。
- [ ] 安全组只开放 22、80、443；8765 不暴露。
- [ ] SSH 使用密钥登录，禁用密码登录（`/etc/ssh/sshd_config` 中 `PasswordAuthentication no`）。
- [ ] 定期 `apt upgrade`，Docker 镜像随代码更新重建。
- [ ] `.env` 权限设为 `chmod 600`，不提交、不截图、不发群。
- [ ] 备份文件不放在 Web 可访问目录。
- [ ] 公开实例建议在 Caddy 或云 WAF 层再加请求频率限制；当前应用只做访问码和上传大小限制。
- [ ] 作业产物链接是“不可猜测”而不是“有权限”，不要上传敏感教材或个人信息。

## 六、排障

### 80 端口被占用，Caddy 起不来

```bash
sudo ss -lntp | grep ':80'
```

常见是宿主机自带 Nginx/Apache。停掉它们，或改 Caddy 端口并前置一层代理。本项目的 compose 固定使用 80/443，因为自动 HTTPS 需要。

### 证书签发失败

- DNS 是否已经指向这台机器；
- 80 端口是否可从公网访问（云防火墙 + 系统防火墙都要放行）；
- 域名是否被 CDN 代理且未回源；
- Let's Encrypt 是否有速率限制，看 Caddy 日志里的具体错误。

### 页面 502

应用没起来或健康检查未通过：

```bash
docker compose --env-file .env ps
docker compose --env-file .env logs --tail=200 touchlearn
```

常见原因是 `.env` 缺 `TOUCHLEARN_ACCESS_CODE`、磁盘满、或镜像构建失败。

### 二维码指向内网地址

反向代理没有传 `Host`，或 `TOUCHLEARN_PUBLIC_URL` 没设置。本仓库的 Caddyfile 已正确处理；自己改 Nginx 时保留 `Host` 和 `X-Forwarded-Proto`。

### 重启后历史作业消失

`/app/outputs` 没有挂命名卷，或执行了 `down -v`。检查 compose 文件中的 `touchlearn-outputs` 卷。

### 磁盘满

```bash
df -h
docker system df
```

清理无用镜像和构建缓存；作业产物按需归档，不要在生产机上无限保留。

## 七、验收清单

- [ ] `https://你的域名/api/health` 返回 `ok: true`；
- [ ] 浏览器地址栏是锁标识，证书由 Let's Encrypt 签发；
- [ ] 输入访问码后能完成一次函数图像构建；
- [ ] 作业页“制造证据”卡片显示 STL SHA-256 和合规判定；
- [ ] 点“校验产物完整性”返回全部一致；
- [ ] 扫码能打开音频页；
- [ ] 重启容器后历史作业仍在；
- [ ] `deploy/backup.sh` 能产出归档。

## 八、和通用大模型的差异在哪

托管只是把服务放到公网。触见相对通用多模态大模型的差异、答辩口径和当前缺口，见 [../docs/positioning.md](../docs/positioning.md)。一句话：大模型负责理解，确定性内核负责毫米级几何，标准检查负责放行，清单与哈希负责复核。
