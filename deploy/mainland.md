# 面向中国大陆任意网络的正式上线方案

目标：任何用户、手机或电脑、移动/联通/电信/校园网/公司网络，在中国大陆任意地区都能直接打开，不需要装 App，不需要翻墙，不需要特殊端口。

## 一、结论先说

要满足这个目标，**必须用中国大陆地域的云服务器 + 已备案域名 + 443 端口 HTTPS**。香港、新加坡或海外 VPS 不需要备案，但无法保证大陆任意网络都能稳定访问；只用 IP + 非标准端口，也会被不少校园网、企业网和手机浏览器拦。

推荐二选一：

| 方案 | 配置 | 公开活动价（2026-10-30 查询） | 适合 |
|---|---|---|---|
| **阿里云 ECS 经济型 e 实例** | 2 核 2G、3M 固定带宽、40GB ESSD Entry | 99 元/年，续费同价 | 长期正式使用，完整 ECS、安全组、快照、备案能力 |
| **腾讯云轻量应用服务器** | 2 核 2G、3M 带宽、40GB SSD、200GB/月 | 45 元/月，459 元/年 | 不想折腾 VPC，控制台简单，备案和建站一条龙 |

如果只想把第一年成本压到最低：

- 阿里云轻量 2 核 2G、40GB、200M 峰值：新客秒杀 38 元/年，常规新客 68 元/年；但续费约 459 元/年，且轻量没有完整 VPC。活动价每天限量，实际以下单页为准。
- 腾讯云轻量 2 核 2G、4M、50GB、300GB/月：50 元/月，510 元/年。

**不推荐**：香港/海外 VPS（大陆访问不保证）、Render 免费实例（休眠 + 临时盘）、家里电脑 + Cloudflare 临时隧道（电脑关机就失效）。

购买入口：

- 阿里云 ECS：<https://www.aliyun.com/product/ecs>
- 阿里云轻量：<https://www.aliyun.com/product/swas>
- 腾讯云轻量：<https://cloud.tencent.com/product/lighthouse>
- 腾讯云 CVM：<https://cloud.tencent.com/product/cvm>
- 阿里云备案：<https://beian.aliyun.com/>
- 腾讯云备案：<https://console.cloud.tencent.com/beian>
- 工信部备案查询：<https://beian.miit.gov.cn/>
- 公安备案：<https://beian.mps.gov.cn/>

## 二、地域和配置怎么选

### 地域

- 首选 **上海、杭州、南京**：覆盖华东和全国，延迟均衡。
- 南方用户多选 **广州、深圳**；北方用户多选 **北京**；西南选 **成都**。
- 只买一台就够。触见是单进程 CPU 服务，不需要多可用区、负载均衡或 GPU。
- 不建议为了“全国加速”再买 CDN。当前产物是动态生成的小文件，CDN 收益有限，还会增加回源和备案复杂度。

### 配置

- **2 核 2G**：推荐。Python 服务常驻约 150–300MB，构建峰值更高；1G 在 Docker 构建时容易 OOM。
- **40GB SSD**：足够。镜像约 400–600MB，系统与 Docker 占 5–8GB，其余放作业产物。
- **3M 固定带宽**：够用。云服务器的“带宽”通常指公网出方向；手机上传图片走入方向，通常更快。3M 出方向下载几 MB 的 STL 只需几秒。
- **5M 更从容**：如果预算允许，或预计多人同时下载，选 5M。
- **不需要 GPU**：模型理解走 API，几何和 STL 在本地 CPU 计算。

### 计费

- 包年包月最省心。轻量套餐含月流量包，超出后限速或按量计费；ECS 固定带宽不按流量计费。
- 先买 1 年，不要一上来买 3 年；备案和比赛周期都可能变化。
- 买完先确认：能否开 80/443、是否支持备案、是否提供免费快照。

## 三、上线关键路径

### 第 0 步：账号与实名

1. 注册阿里云或腾讯云账号，完成个人/企业实名认证。
2. 同一个账号下买域名和服务器，备案时最省事。
3. 域名完成实名认证；`.com` / `.cn` 都可以，首次购买通常 60–100 元/年。

### 第 1 步：买服务器

按上面的配置购买，地域选上海/杭州/南京/广州/北京/成都之一，系统选 Ubuntu 22.04 或 24.04 LTS。

安全组先放行：

- 22（SSH，最好限制来源 IP）
- 80（HTTP，备案和证书需要）
- 443（HTTPS）
- 8765（临时测试用，备案通过后关闭）

### 第 2 步：解析域名

在域名 DNS 添加 A 记录：

```
touch.你的域名.com  →  服务器公网 IP
```

用 `dig +short touch.你的域名.com` 确认解析生效。TTL 先设 600 秒，切换方便。

### 第 3 步：备案前先跑起来

备案审核期间，域名走 80/443 可能被云厂商拦截，但你可以先用 IP + 8765 验证服务：

```bash
ssh root@服务器IP
apt update && apt install -y git
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

git clone https://github.com/CAG1218/touchlearn-fudan-Team_Stella.git
cd touchlearn-fudan-Team_Stella
docker build -t touchlearn:0.2.0 .
docker run -d --name touchlearn-test -p 8765:8765 \
  -e TOUCHLEARN_ACCESS_CODE=TL-临时测试码 \
  -v touchlearn-outputs:/app/outputs \
  touchlearn:0.2.0
```

浏览器打开 `http://服务器IP:8765`，用手机蜂窝网络也试一次。确认能上传、能生成、能下载 STL。

> 这一步只是你自己验证。备案通过前不要把域名对外宣传或提供公开服务；正式上线必须走域名 + 443，并在备案通过后关闭 8765。

### 第 4 步：提交 ICP 备案

在服务器所在云厂商的备案系统提交。流程：

1. 填写主办者信息（个人身份证 / 企业营业执照）；
2. 填写网站信息：域名、网站名称、服务内容、服务器实例；
3. 上传证件、完成人脸核验；
4. 云厂商初审（通常 1–2 个工作日）；
5. 工信部短信核验（收到短信后按提示操作）；
6. 省通信管理局审核（官方最长 20 个工作日，常见 3–15 个工作日）；
7. 审核通过后下发 ICP 备案号。

备案期间注意：

- 保持手机畅通，管局可能电话核验；
- 网站名称与服务内容要真实、非经营性；
- 不要放广告、支付、会员收费等内容；
- 网站底部必须展示备案号，并链接到 <https://beian.miit.gov.cn/>。

**网站名称建议**：`触见触觉教材生成工具`。服务内容写“触觉图形设计与打印辅助工具，非经营性”。避免使用“教育培训、新闻、出版、医疗、药品、文化、广播电影电视节目”等可能触发前置审批的词。

### 第 5 步：公安备案

ICP 通过后，通常要求在 30 日内到 <https://beian.mps.gov.cn/> 完成公安联网备案。按网站实际内容填写，审核通过后同样会得到备案号。具体时限和材料以当地公安要求为准。

### 第 6 步：正式部署

```bash
cd touchlearn-fudan-Team_Stella/deploy
cp .env.example .env
```

填写：

```dotenv
SITE_ADDRESS=touch.你的域名.com
TOUCHLEARN_PUBLIC_URL=https://touch.你的域名.com
TOUCHLEARN_ACCESS_CODE=TL-换成强随机码
TOUCHLEARN_ICP_BEIAN=沪ICP备XXXXXXXX号-X
```

启动：

```bash
bash deploy.sh
```

Caddy 会自动申请 Let's Encrypt 证书。首次签发需要 80 端口可从公网访问，且 DNS 已生效。

验证：

```bash
curl -fsS https://touch.你的域名.com/api/health
```

然后：

1. 浏览器打开域名，确认锁标识和证书；
2. 输入访问码，完整跑一次函数或电路生成；
3. 用手机移动网络（关闭 Wi-Fi）再跑一次；
4. 扫码打开音频页；
5. 在作业页点“校验产物完整性”；
6. 确认设置页显示 ICP 备案号；
7. 关闭安全组的 8765 端口。

## 四、代码侧已经准备好的东西

- `TOUCHLEARN_ICP_BEIAN`：备案号展示在首页底部、登录页和设置页，点击跳转工信部备案系统；不填则不显示。
- `TOUCHLEARN_PUBLIC_URL`：二维码和音频页使用固定公网地址，不会指回内网 IP。
- `PIP_INDEX_URL` 构建参数：大陆构建慢时可切阿里云/腾讯云 PyPI 镜像。
- Caddy 自动 HTTPS、HTTP/2/3、请求体上限、日志轮转。
- 前端不依赖任何外部 CDN、字体或统计脚本，断网环境也能加载界面；这一点已经审计过。
- 模型预置支持 TokenHub、DeepSeek、阿里云百炼、月之暗面、智谱和本地 Ollama，大陆服务器可直接调用。

## 五、大陆环境特有的坑

### 1. Docker Hub 拉取慢或失败

`python:3.12-slim` 和 `caddy:2-alpine` 都来自 Docker Hub。大陆服务器建议在云厂商控制台开通“容器镜像加速器”，然后配置：

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
{
  "registry-mirrors": ["https://你的专属加速地址.mirror.aliyuncs.com"]
}
EOF
sudo systemctl daemon-reload
sudo systemctl restart docker
```

加速地址从阿里云“容器镜像服务 → 镜像工具 → 镜像加速器”或腾讯云“容器镜像服务”获取。不要使用来源不明的公共镜像站。

### 2. PyPI 安装慢

构建时传镜像：

```bash
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ bash deploy.sh
```

或者写进 `deploy/.env`：

```dotenv
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
```

### 3. 模型接口在大陆不可达

不要在大陆服务器上配置 OpenAI、Anthropic、Google 等直连地址。推荐：

```dotenv
TOUCHLEARN_LLM_PROVIDER=deepseek
TOUCHLEARN_LLM_KEY=sk-...
TOUCHLEARN_LLM_MODEL=deepseek-v4-pro

TOUCHLEARN_VLM_PROVIDER=dashscope
TOUCHLEARN_VLM_KEY=sk-...
TOUCHLEARN_VLM_MODEL=qwen-vl-max
```

也可以文本和视觉都用 TokenHub 或百炼。配置后在页面上传一张教材图片，确认能返回结构化草稿；失败时看 `docker compose logs touchlearn`。

### 4. Let's Encrypt 签发失败

先检查：

- DNS 是否已生效；
- 80 端口是否对公网开放（安全组 + 系统防火墙）；
- 备案是否已通过（未备案时云厂商可能拦截 80）；
- 域名是否被 CDN 代理且未正确回源。

如果 ACME 长期不可用，可在云厂商申请免费 SSL 证书，把证书挂进 Caddy 容器，改用 Caddyfile 的 `tls /etc/caddy/cert.pem /etc/caddy/key.pem`。这条路需要手动续期，能走自动签发就不要选。

### 5. 访问码与公开访问

当前默认用 `TOUCHLEARN_ACCESS_CODE` 保护分析和生成接口，适合比赛演示，防止陌生人消耗你的模型额度。

如果要真正“任何用户直接打开”，需要同时做三件事，否则不要去掉访问码：

1. 在云 WAF 或带限流插件的 Caddy 上做 IP 限流；
2. 给作业产物设置保留期，定期清理旧作业；
3. 监控磁盘和模型账单，设置预算告警。

去掉访问码只需把 `TOUCHLEARN_ACCESS_CODE` 留空；但公开实例被刷接口时，消耗的是你的 API Key 和磁盘。

### 6. 作业产物会一直增长

`outputs/jobs` 不会自动删除。正式实例建议每天清理 14 天前的作业。先 dry-run 看会删什么：

```bash
docker run --rm -v touchlearn-outputs:/data alpine \
  find /data/jobs -mindepth 1 -maxdepth 1 -type d -mtime +14 -print
```

确认后再加 `-exec rm -rf {} +`。清理前先跑 `bash deploy/backup.sh`。不要把 `drafts` 或正在生成的作业误删。

### 7. 时区与日志

```bash
sudo timedatectl set-timezone Asia/Shanghai
```

Caddy 访问日志在 `touchlearn-caddy-data` 卷的 `/data/access.log`，已按 20MB 轮转。Docker 日志也已限制大小，不会无限增长。

### 8. 移动端与浏览器兼容

- iOS Safari、Android Chrome、微信内置浏览器都走标准 HTTPS 和原生 fetch，没有外部依赖。
- 页面是移动端优先布局，上传、草稿确认、STL 下载和二维码都在同一页完成。
- 微信内置浏览器对 `blob:` 下载支持有限；如果用户反馈下载没反应，提示用系统浏览器打开，或直接扫码音频页。
- 不要求 WebGL；三维查看是服务端渲染的预览，低端手机也能打开。

## 六、费用量级

| 项目 | 首年 | 续费 |
|---|---|---|
| 阿里云 ECS e 2C2G 3M | 约 99 元 | 约 99 元/年（以活动页为准） |
| 腾讯云轻量 2C2G 3M | 约 459 元 | 约 459 元/年 |
| 阿里云轻量 2C2G（新客） | 38–68 元 | 约 459 元/年 |
| 域名 | 60–100 元 | 60–100 元/年 |
| LLM / VLM API | 几元到几十元/月 | 按用量 |
| 合计 | 约 200–700 元 | 约 200–700 元 + API |

比赛演示阶段，模型调用通常每月几元到几十元。真正花钱的是公开后被刷，所以访问码、限流和预算告警要一起上。

## 七、上线验收清单

- [ ] 域名 A 记录指向大陆服务器；
- [ ] ICP 备案通过，备案号展示在登录页和设置页，并链接工信部；
- [ ] 公安备案已提交或已完成；
- [ ] `https://域名/api/health` 返回 `ok: true`；
- [ ] 证书由可信 CA 签发，浏览器无警告；
- [ ] 手机移动网络、Wi-Fi、电脑浏览器都能打开；
- [ ] 上传图片、生成、下载 STL、扫码音频全流程通过；
- [ ] 作业页“校验产物完整性”全部一致；
- [ ] 关闭 8765，只保留 22/80/443；
- [ ] SSH 密钥登录，禁用密码登录；
- [ ] 备份脚本跑通，备份文件不在 Web 目录；
- [ ] 磁盘和模型账单有告警；
- [ ] 明确标注“尚未完成实体打印和盲人用户测试”，不夸大触觉可读性。

## 八、你现在要做的五件事

1. **注册并实名**阿里云或腾讯云账号；
2. **买域名 + 大陆服务器**：2 核 2G、40GB、3M，上海/杭州/南京/广州/北京/成都；
3. **加 DNS A 记录**，安全组放行 22/80/443 和临时 8765，用 IP:8765 先跑通；
4. **提交 ICP 备案**，保持手机畅通，等待 3–15 个工作日；
5. **备案通过后**填 `deploy/.env`，跑 `bash deploy/deploy.sh`，设置备案号，关闭 8765，用手机蜂窝网络验收。

如果只求比赛现场稳定，先用同一 Wi-Fi 或临时隧道；如果要“任何用户、任意设备、任意大陆网络”，就按这份方案走大陆服务器 + 备案域名，没有捷径。
