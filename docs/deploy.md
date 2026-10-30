# 部署与多设备访问

触见是一个标准的 Python HTTP 服务，前端是静态文件。它不需要 Node 构建；账号和会话存放在一个 SQLite 文件里，不需要单独安装数据库。本文给出三条从轻到重的路径：

| 场景 | 入口 | 适合 | 代价 |
|---|---|---|---|
| 同一 Wi-Fi | `http://<电脑局域网 IP>:8765` | 比赛现场、手机扫码 | 电脑必须开着，手机和电脑同一网络 |
| 临时公网 | `https://*.trycloudflare.com` | 异地演示、发给评委快速体验 | 地址随进程变化，电脑休眠即失效 |
| 固定公网（推荐） | `deploy/` + Docker Compose + Caddy + 自己的服务器 | 长期展示、随时访问、自动 HTTPS | 需要一台云服务器；域名可选，无域名可用 IP + 端口模式；选型见 [deploy/README.md](../deploy/README.md) |
| 固定公网（托管平台） | Render / Railway | 不想管服务器、先看效果 | 免费实例会休眠，产物盘可能不持久 |

> 无论走哪条路，公网部署都必须至少配置一种验证码通道（SMTP 邮件或阿里云短信），并关闭 `TOUCHLEARN_AUTH_DEV_MODE`。分析和生成接口需要登录；已经生成的产物链接按不可猜测的作业 ID 公开，方便手机扫码，不需要登录。

## 一、同一 Wi-Fi（零配置）

```bash
python scripts/run_agent.py --port 8765
```

- 默认监听 `0.0.0.0`，终端会打印本机和局域网地址。
- 手机浏览器打开 `http://<局域网 IP>:8765`。
- Windows 第一次运行可能弹出防火墙提示，放行“专用网络”即可。
- 二维码基础地址会自动使用手机访问时的 Host。用局域网 IP 打开，二维码就指向局域网 IP。

如果扫码打不开：

1. 确认手机和电脑在同一 Wi-Fi，且没有开启 AP 隔离；
2. 用 `ipconfig` 核对电脑当前 IPv4；
3. 临时关闭防火墙测试，或添加入站规则放行 TCP 8765；
4. 换端口：`python scripts/run_agent.py --port 8877`。

## 二、临时公网（Cloudflare Quick Tunnel）

电脑上安装一次 cloudflared：

```powershell
winget install --id Cloudflare.cloudflared
```

然后：

```bash
python scripts/run_public.py
```

脚本会：

1. 启动触见服务；
2. 启动 Cloudflare Quick Tunnel；
3. 打印公网地址，并写入 `outputs/public_access.json`。

典型输出：

```text
==========================================================
  公网地址（任何设备可访问）
  https://random-words.trycloudflare.com
==========================================================
```

首次登录会自动创建账号。没有配置 SMTP 或短信时，服务会进入开发模式，验证码直接显示在页面上，启动日志会打印警告；这只适合本机验收，不要把开发模式直接发给外部用户。

常用参数：

```bash
python scripts/run_public.py --port 8877         # 换端口
python scripts/run_public.py --no-tunnel         # 只开局域网
```

限制必须说清楚：

- Quick Tunnel 地址每次重启都变；
- 电脑休眠、断网或关闭终端后链接立即失效；
- Cloudflare 不保证该地址的长期可用性；
- 它只适合演示，不是“永久在线”。

如果只要一个临时公网地址做验收，这一条就够了。要真正“随时可访问”，继续看下一节。

## 三、账号与验证码通道（公网必配）

登录不需要密码：用户输入手机号或邮箱，收到 6 位验证码后即可登录；第一次验证会自动创建账号。同一手机号或邮箱在实例内只能属于一个账号，登录后可以把另一种联系方式绑定到同一账号。

### 3.1 至少配置一种通道

**邮件（SMTP）**

```dotenv
TOUCHLEARN_AUTH_DEV_MODE=false
TOUCHLEARN_SMTP_HOST=smtp.exmail.qq.com
TOUCHLEARN_SMTP_PORT=465
TOUCHLEARN_SMTP_USER=no-reply@example.com
TOUCHLEARN_SMTP_PASSWORD=客户端授权码
TOUCHLEARN_SMTP_FROM=触见 <no-reply@example.com>
TOUCHLEARN_SMTP_TLS=ssl
```

- `TOUCHLEARN_SMTP_TLS` 可选 `ssl`（465）、`starttls`（587）、`none`（内网中继）。
- QQ 邮箱、163、企业微信邮箱通常要单独生成“客户端授权码”，不能直接用登录密码。
- 发件域名最好配置 SPF/DKIM，否则验证码容易进垃圾箱。

**手机短信（阿里云）**

```dotenv
TOUCHLEARN_AUTH_DEV_MODE=false
TOUCHLEARN_SMS_PROVIDER=aliyun
TOUCHLEARN_ALIYUN_SMS_KEY_ID=...
TOUCHLEARN_ALIYUN_SMS_KEY_SECRET=...
TOUCHLEARN_ALIYUN_SMS_SIGN_NAME=触见
TOUCHLEARN_ALIYUN_SMS_TEMPLATE_CODE=SMS_...
TOUCHLEARN_ALIYUN_SMS_REGION=cn-hangzhou
```

短信模板必须包含验证码变量，并在阿里云控制台审核通过。模板变量名按 `code` 传递；如果模板使用其他变量名，需要调整 `accounts.py` 里的发送参数。

本机直接运行 `python scripts/run_agent.py` 或 `scripts/run_public.py` 时，服务会读取仓库根目录的 `.env`；Docker 部署读取 `deploy/.env`（由 compose 的 `env_file` 注入）。两处不要混用，改了哪一处就对应用哪一种启动方式。

### 3.2 开发模式

如果既没有 SMTP 也没有短信配置，服务会自动进入开发模式：

- 验证码打印在服务端控制台；
- 同时通过 `/api/auth/request-code` 返回给浏览器，页面直接显示并自动填入；
- 启动日志会打印 `[警告] 公网部署请配置 SMTP 或阿里云短信，并关闭 TOUCHLEARN_AUTH_DEV_MODE。`

开发模式让任何人都能登录，只适合本机测试。显式关闭：

```dotenv
TOUCHLEARN_AUTH_DEV_MODE=false
```

关闭后如果没有任何可用通道，用户会看到“部署者尚未配置短信或邮件通道”，无法登录。

### 3.3 账号库与备份

- 默认路径 `<outputs>/accounts.sqlite3`，可用 `TOUCHLEARN_AUTH_DB` 改到持久卷。
- Docker Compose 已把 `/app/outputs` 挂成命名卷，账号、会话和作业一起持久化。
- 备份时同时复制 `accounts.sqlite3` 和 `outputs/jobs/`；只备份作业会丢账号，只备份账号会丢产物。
- 直接复制正在写入的 SQLite 文件可能得到损坏副本；备份前先停服务，或用 `sqlite3 accounts.sqlite3 ".backup backup.sqlite3"`。

### 3.4 限流与有效期

| 项目 | 默认值 | 环境变量 |
|---|---|---|
| 验证码有效期 | 10 分钟 | `TOUCHLEARN_AUTH_CODE_TTL` |
| 重发冷却 | 60 秒 | `TOUCHLEARN_AUTH_RESEND_AFTER` |
| 单标识每小时上限 | 8 次 | `TOUCHLEARN_AUTH_CODES_PER_HOUR` |
| 单 IP 每小时上限 | 40 次 | `TOUCHLEARN_AUTH_CODES_PER_IP_HOUR` |
| 登录状态 | 30 天 | `TOUCHLEARN_AUTH_SESSION_DAYS` |

验证码最多尝试 5 次，错误 5 次后作废；会话 Cookie 为 HttpOnly、SameSite=Lax，HTTPS 下自动加 Secure。反向代理必须保留 `Host` 和 `X-Forwarded-Proto`，否则 Secure Cookie 可能不生效。

## 四、固定公网（Render + Docker）

> 如果你已经有自己的云服务器，直接看 [deploy/README.md](../deploy/README.md)：里面包含 ECS/VPS 选型、配置建议、备案说明、一键 Compose、Caddy 自动 HTTPS、备份与排障。目标是“中国大陆任意网络都能打开”时，看 [deploy/mainland.md](../deploy/mainland.md)，按大陆服务器 + 备案域名 + 443 的路径走；暂时不买域名可以用 `DEPLOY_MODE=ip` 走公网 IP + 8765。本节是托管平台路线，适合不想维护服务器的场景。

仓库已经包含 `Dockerfile`、`.dockerignore`、`render.yaml` 和 `Procfile`。Render 可以直接读取 Blueprint。

### 4.1 前置条件

1. 代码在 GitHub 仓库里（当前远程：`CAG1218/touchlearn-fudan-Team_Stella`）；
2. 有 Render 账号；
3. 准备一种验证码通道（SMTP 邮箱或阿里云短信），否则只能以开发模式运行；
4. 注意：Render 免费实例没有持久磁盘，重新部署会丢账号和产物。正式使用请升级到带 Persistent Disk 的实例，或改用自有服务器。

### 4.2 Render Blueprint

1. 登录 Render Dashboard；
2. **New → Blueprint**；
3. 选择仓库，Render 会读取 `render.yaml`；
4. 在环境变量页填写：
   - `TOUCHLEARN_AUTH_DEV_MODE=false`：必填，防止开发模式上线；
   - `TOUCHLEARN_SMTP_*` 或 `TOUCHLEARN_ALIYUN_SMS_*`：至少一组；
   - `TOUCHLEARN_LLM_*` / `TOUCHLEARN_VLM_*`：按需；
   - `TOUCHLEARN_PUBLIC_URL`：只有绑定自定义域名时才填，例如 `https://touch.example.com`。留空时按请求 Host 自动推导，二维码会跟着当前访问地址走。
5. Apply，等待构建完成；
6. 打开 `https://<service>.onrender.com`，用手机号或邮箱验证码登录。

`render.yaml` 使用 Docker 运行时，健康检查路径是 `/api/health`。免费实例在闲置后休眠，下一次打开需要几十秒冷启动；这是 Render 的套餐行为，不是触见卡死。

### 4.3 直接使用 Docker

本地或任意 VPS：

```bash
docker build -t touchlearn .
docker run --rm -p 8765:8765 \
  -e TOUCHLEARN_AUTH_DEV_MODE=false \
  -e TOUCHLEARN_SMTP_HOST=smtp.exmail.qq.com \
  -e TOUCHLEARN_SMTP_USER=no-reply@example.com \
  -e TOUCHLEARN_SMTP_PASSWORD=... \
  -e TOUCHLEARN_SMTP_FROM="触见 <no-reply@example.com>" \
  -e TOUCHLEARN_LLM_PROVIDER=tokenhub \
  -e TOUCHLEARN_LLM_KEY=sk-... \
  -e TOUCHLEARN_LLM_MODEL=deepseek-v4.1-flash \
  -v touchlearn-outputs:/app/outputs \
  touchlearn
```

浏览器打开 `http://127.0.0.1:8765`。前面放 Nginx 时，记得转发 `Host` 和 `X-Forwarded-Proto`，否则二维码可能指回内网地址；Caddy 的 `reverse_proxy` 默认会保留 Host 并补全转发头，仓库里的 Caddyfile 已验证。

Railway/Heroku 一类平台可以使用 `Procfile`；平台会注入 `PORT`，服务已适配。

### 4.4 持久化与容量

- 免费容器通常使用临时文件系统，重启/重新部署后 `outputs/` 里的作业和 `accounts.sqlite3` 里的账号会一起消失，历史二维码链接随之失效。
- 要保留产物和账号，给 `/app/outputs` 挂持久卷（Render 的 Persistent Disk 需要付费实例），或自行把作业目录同步到对象存储；账号库不要单独放临时盘。
- 单文件上限 30 MB、单次总计 80 MB、最多 12 个附件；公开实例建议在反向代理再加请求频率限制和磁盘配额。
- 作业产物按 12 位十六进制 ID 公开读取，这是为了让扫码音频页不依赖登录 Cookie；不要把它当成权限边界，敏感内容不要上传。

## 五、接入大模型

不配置也能跑：分类用关键词，函数用正则，场景用 8 个离线模板；但图片理解、自由文字生成场景、复杂图结构抽取会降级。

最少配置一个文本模型：

```dotenv
TOUCHLEARN_LLM_PROVIDER=tokenhub
TOUCHLEARN_LLM_KEY=sk-...
# 可选；留空用预置默认值。这里只是初始默认，用户可在页面模型菜单里切换
TOUCHLEARN_LLM_MODEL=deepseek-v4.1-flash
# 可选：同一 provider 会复用上面的 KEY
TOUCHLEARN_VLM_PROVIDER=tokenhub
TOUCHLEARN_VLM_MODEL=deepseek/deepseek-flash
# 可选：长场景 JSON 用更快的模型并关闭思考，避免被 max_tokens 截断
TOUCHLEARN_SCENE_MODEL=deepseek-v4.1-flash
TOUCHLEARN_LLM_EFFORT=low
TOUCHLEARN_SCENE_EFFORT=off
```

模型与思考强度是**用户可选项**，不是部署时锁死的配置：服务启动后，页面输入框上方的模型按钮会列出当前实例提供的模型，用户可以自行切换；`.env` 里的值只决定初始默认。选择保存在浏览器本地，下一次请求生效，不影响其他设备。

也可以把 `TOUCHLEARN_LLM_PROVIDER` 换成 `deepseek`，模型改为 `deepseek-v4-pro` / `deepseek-flash`。

图片理解需要视觉模型。预置里自带视觉模型的包括：

| 预置 | 文本模型 | 视觉模型 |
|---|---|---|
| `tokenhub` | `deepseek-v4.1-flash` | `deepseek/deepseek-flash` |
| `deepseek` | `deepseek-v4-pro` | `deepseek-flash` |
| `openai` | `gpt-4o-mini` | `gpt-4o-mini` |
| `dashscope` | `qwen-plus` | `qwen-vl-max` |
| `moonshot` | `moonshot-v1-8k` | `moonshot-v1-8k-vision-preview` |
| `zhipu` | `glm-4-flash` | `glm-4v-flash` |
| `ollama` | `qwen2.5:7b` | `qwen2.5vl:7b` |

如果更偏好百炼的 Qwen-VL，也可以文本用 DeepSeek、视觉单独用百炼：

```dotenv
TOUCHLEARN_LLM_PROVIDER=deepseek
TOUCHLEARN_LLM_KEY=sk-...
TOUCHLEARN_VLM_PROVIDER=dashscope
TOUCHLEARN_VLM_KEY=sk-...
TOUCHLEARN_VLM_MODEL=qwen-vl-max
```

完整预置、端点规则和失败行为见 [providers.md](providers.md)。密钥只从环境变量或本机 `.env` 读取，不会写进作业目录，也不会通过 `/api/providers` 返回。

## 六、语音与 PDF 的平台差异

- 本地 Windows：使用系统 SAPI 生成 `narration.wav`，当前机器有 `Microsoft Huihui Desktop` 中文语音。
- Linux 容器：没有 SAPI，WAV 生成会失败并记录到 `manifest.audio.tts_message`；前端会自动切换为浏览器 `speechSynthesis` 朗读讲解稿。讲解稿文本、音频页和二维码仍然可用。
- PDF：Docker 镜像安装了 `pypdf`，能读文字型 PDF 的文字层。扫描件没有文字层时不会假装识别，需要配置视觉模型并上传关键页图片；当前不做服务端 PDF 光栅化。

## 七、排障

### 页面显示“待登录”或 401

浏览器没有有效的登录 Cookie。用手机号或邮箱验证码重新登录；如果收不到验证码：

1. 看服务端日志有没有 SMTP/短信报错；
2. 确认 `TOUCHLEARN_AUTH_DEV_MODE` 不是误开的 `true`（开发模式会在页面显示验证码）；
3. 检查发件箱/垃圾箱和短信签名、模板审核状态；
4. 同一标识 1 小时最多 8 次、同一 IP 40 次，触发限流会返回 `retry_after`。

### 账号库被锁或写入失败

确认 `TOUCHLEARN_AUTH_DB` 所在目录可写。Docker 部署要放在 `/app/outputs` 这类持久卷里；只读文件系统会让登录和首次注册失败。备份时不要直接复制正在写入的数据库文件。

### 页面能开，但二维码指向 127.0.0.1

服务前面有反向代理，但没有传 `Host` / `X-Forwarded-Host`。修代理配置，或显式设置 `TOUCHLEARN_PUBLIC_URL`。

### 公网地址打开是 502

cloudflared 还在，但本地服务已经退出。看终端日志；重新运行 `python scripts/run_public.py`。

### 生成一直停在“建模中”

看终端日志和 `outputs/jobs/<id>/error.txt`。常见原因是上传文件损坏、STL 非流形、或磁盘写满。生成是单进程串行锁，多个作业会排队。

### 免费实例冷启动很慢

Render 免费实例会休眠。升级实例，或改用常驻 VPS；不建议用“定时 ping”绕过平台策略。

## 八、当前还需要人来做的事

1. **大模型 API Key**：没有也能离线演示；要演示照片/复杂教材理解，需要至少一个文本模型，最好再加一个视觉模型。
2. **固定托管环境**：渲染平台或自有服务器任选其一。仓库侧配置已经准备好，账号、域名和备案需要人来完成；自有服务器的完整步骤见 [deploy/README.md](../deploy/README.md)。
3. **自定义域名（可选）**：没有域名也能用 `onrender.com` 子域；有域名时在平台绑定后设置 `TOUCHLEARN_PUBLIC_URL`。
4. **实体打印与盲人用户测试**：仍未完成。内部 PASS 只代表几何和工艺检查通过，不等于国家标准认证，也不等于真实触觉可用性验证。
