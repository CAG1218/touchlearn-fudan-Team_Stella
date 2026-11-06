# 把 v0.3.5 更新到阿里云 ECS

目标服务器：`47.103.61.115`（上海，2 vCPU / 4 GiB + 2 GiB swap，IP 模式，端口 8765）

这次更新包含：

- 服务端语音合成：Linux 容器用 edge-tts 生成 `narration.mp3`（中文 `zh-CN-XiaoxiaoNeural`），不再退回浏览器朗读
- Windows 本机仍优先走系统 SAPI 生成 `narration.wav`；`TOUCHLEARN_TTS_ENGINE=auto|sapi|edge` 可强制引擎
- 音频文件统一由 `manifest.audio.audio_file` / `audio_format` / `engine` 描述，前端按扩展名播放和下载
- 静态文件支持 HTTP Range（206 / 416），修复 iOS Safari 上 `<audio>` 无法拖动进度、偶发不播放
- 讲解脚本与音频同时生成才算合规 PASS；音频失败时保留脚本并明确提示原因

上一版（v0.3.4）包含：盲文标题不截断、水密/体积单趟计算、TokenHub 文字生成 3D 与照片级重建、三模式并行。

下面命令都在服务器上执行，只有第 1 步在本机执行。

## 1. 本机上传发布包

发布包已经生成：`touchlearn-0.3.5.tar.gz`。

```bash
scp touchlearn-0.3.5.tar.gz root@47.103.61.115:/root/
```

如果本机没有配置 SSH 密钥，`scp` 会提示输入服务器密码。

## 2. 登录服务器并找到现有项目目录

```bash
ssh root@47.103.61.115
find / -maxdepth 5 -name "docker-compose.ip.yml" 2>/dev/null
```

记下输出中的目录，例如 `/root/touchlearn-fudan-Team_Stella`。下文的 `APP_DIR` 都指这个目录。

```bash
APP_DIR=/root/touchlearn-fudan-Team_Stella   # 按上一步结果修改
cd "$APP_DIR"
ls deploy/.env deploy/docker-compose.ip.yml
```

## 3. 备份配置和账号库

```bash
cp deploy/.env "/root/touchlearn-env-$(date +%F-%H%M).bak"

docker run --rm \
  -v touchlearn-outputs:/data \
  -v /root:/backup \
  alpine tar czf /backup/touchlearn-outputs-$(date +%F-%H%M).tar.gz -C /data .
```

第一条备份环境变量，第二条备份账号库和所有历史作业。确认 `/root` 下出现两个备份文件后再继续。

## 4. 解压新版本代码

发布包不包含 `.env`，不会覆盖你的密钥。

```bash
cd "$APP_DIR"
tar -xzf /root/touchlearn-0.3.5.tar.gz -C "$APP_DIR"
```

解压后确认版本：

```bash
grep '__version__' src/touchlearn/__init__.py
# 应输出 __version__ = "0.3.5"
```

## 5. 修改 `deploy/.env`

```bash
nano deploy/.env
```

确认或补充以下几项（不要改已有密码和验证码配置）：

```dotenv
DEPLOY_MODE=ip
TOUCHLEARN_PUBLIC_URL=http://47.103.61.115:8765
TOUCHLEARN_AUTH_DEV_MODE=false

# 文本与视觉模型：TokenHub 与 DeepSeek 二选一
TOUCHLEARN_LLM_PROVIDER=tokenhub
TOUCHLEARN_LLM_KEY=你的TokenHub密钥
TOUCHLEARN_LLM_MODEL=deepseek-v4.1-flash
TOUCHLEARN_VLM_PROVIDER=tokenhub
TOUCHLEARN_VLM_MODEL=deepseek/deepseek-flash
TOUCHLEARN_LLM_TIMEOUT=120

# 照片级 3D 重建与文字生成 3D：复用同一个 TokenHub Key
TOUCHLEARN_3D_PROVIDER=tokenhub
TOUCHLEARN_3D_KEY=
TOUCHLEARN_3D_MODEL=tripo-3d-p1
TOUCHLEARN_3D_TEXT_MODEL=tripo-3d-3.1
TOUCHLEARN_3D_QUALITY=high
TOUCHLEARN_3D_GENERATIVE=1
TOUCHLEARN_3D_FACES=300000
TOUCHLEARN_3D_TIMEOUT=600
TOUCHLEARN_3D_POLL=4

# 讲解语音：auto 在 Linux 上用 edge-tts；也可强制 sapi / edge
TOUCHLEARN_TTS_ENGINE=auto
# 留空使用 zh-CN-XiaoxiaoNeural；也可换成 zh-CN-YunxiNeural 等
TOUCHLEARN_TTS_VOICE=
```

重点：

- `TOUCHLEARN_3D_KEY` 留空即可，TokenHub 会自动复用 `TOUCHLEARN_LLM_KEY`。
- 文字生成 3D 默认走 `tripo-3d-3.1`（最高 150 万面，detailed 几何与纹理）；也可以改成 `hy-3d-3.1`、`hy-3d-3.0`、`tripo-3d-p1`、`tripo-3d-p2` 或 `hy-3d-express`。
- `TOUCHLEARN_3D_QUALITY` 控制面数与纹理档位：`draft`、`standard`、`high`（默认）。`TOUCHLEARN_3D_FACES` 只影响图生 3D。
- 本次升级验收请先把 `.env` 设为 `TOUCHLEARN_3D_QUALITY=high`，新建一个任务确认 66% 能走到 100%；验证完如果担心并发内存，可以改回 `standard` 并重新 `up -d`（不需要重新构建镜像）。
- 设 `TOUCHLEARN_3D_GENERATIVE=0` 可强制回退到本地结构化编译器（太阳系与离线演示仍会自动走这条）。
- 图生 3D 默认 `tripo-3d-p1`：1 张照片走单图；2–4 张且文件名含 `front/left/back/right`（或 `前/左/后/右`）时自动走多视图 `inputs`。`hy-3d-3.1` 走 `image_base64` 单图。
- Tripo 示例用 HTTPS 图片 URL。服务器已设置 `TOUCHLEARN_PUBLIC_URL=http://47.103.61.115:8765`，构建时会把照片临时发布到 `/files/<job>/input-N.<ext>`，重建结束立即删除；不要把这个地址改成 `127.0.0.1`，否则会退回 data URI。
- `hy-3d-motion` 输出动画，不是可打印静态网格，不要配置为 3D 模型。
- 宿主机 shell 里 `export` 的变量不会自动进入容器。密钥必须写进 `deploy/.env`，或者写进 compose 的 `environment`，然后重建容器。
- 未登录访问 `/api/health` 时，`llm` / `vision` / `reconstruction` 会统一显示 `locked`，这是隐私保护，不代表 Key 没配好。部署后用第 7 步的容器内命令确认真实状态。

## 6. 重建并启动

```bash
cd "$APP_DIR/deploy"
docker compose --env-file .env -f docker-compose.ip.yml up -d --build --remove-orphans
docker compose --env-file .env -f docker-compose.ip.yml ps
```

首次构建约 3–8 分钟。看到 `touchlearn` 状态为 `healthy` 即成功。

## 7. 验证

```bash
curl -s http://127.0.0.1:8765/api/health | python3 -m json.tool
```

应看到：

```json
{
  "ok": true,
  "version": "0.3.5",
  "public_base_url": "http://47.103.61.115:8765"
}
```

再检查容器内的三维配置（不会输出密钥）：

```bash
docker compose --env-file .env -f docker-compose.ip.yml exec touchlearn \
  python -c "import json; from touchlearn.agent.reconstruct import reconstruction_status as s; print(json.dumps(s(), ensure_ascii=False, indent=2))"
```

应看到：

```json
{
  "configured": true,
  "provider": "tokenhub",
  "label": "腾讯云 TokenHub",
  "model": "tripo-3d-p1",
  "text_model": "tripo-3d-3.1",
  "text_to_3d": true,
  "quality": "high",
  "face_budget": 1500000
}
```

确认容器内能装到 edge-tts（不依赖系统 SAPI）：

```bash
docker compose --env-file .env -f docker-compose.ip.yml exec touchlearn \
  python -c "import edge_tts, importlib.metadata as m; print('edge-tts', m.version('edge-tts'))"
```

浏览器访问 `http://47.103.61.115:8765`，登录后：

1. 先跑一次“触觉教材生成”，确认原有功能正常；
2. 再勾选“触觉3D模型”，输入“做一个六轴机械臂 3D 模型”，确认卡里填标题、高度、底座，提交；
3. 页面会依次显示提交、排队、生成、下载、解析、组装；TokenHub 通常 2–6 分钟返回；
4. 下载 `model.stl`（可打印）、`model_textured.glb`（带纹理）和 `generation.json`（提示词、模型、面数、后处理参数）；
5. 打开作业里的 `audio.html`，确认播放的是 `narration.mp3` 而不是浏览器朗读音；手机扫码再验证一次，确认 iOS / Android 都能播放并拖动进度条；
6. 再选“照片级 3D 重建”，上传一张主体清晰、背景干净的照片，确认同样的高度、底座和盲文名称流程；
7. 用 Bambu Studio / PrusaSlicer / Cura 打开 STL 检查底座、标题牌和支撑方向。注意 AI 生成网格可能有开放边，切片前先修复。

## 8. 回滚

如果新版本启动失败：

```bash
cd "$APP_DIR"
# 恢复旧代码：用更新前的备份或 git 记录
git status
git stash list
# 恢复环境变量
cp /root/touchlearn-env-YYYY-MM-DD-HHMM.bak deploy/.env
cd deploy
docker compose --env-file .env -f docker-compose.ip.yml up -d --build --remove-orphans
```

账号库在 Docker 卷 `touchlearn-outputs` 里，重建容器不会删除。只有执行 `docker volume rm touchlearn-outputs` 才会删，不要执行。

## 9. 常见问题

**构建时 pip 很慢**：在 `deploy/.env` 加一行

```dotenv
PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
```

**TokenHub 返回 `InvalidResultFormat`**：`result_format` 必须是大写 `GLB`，当前代码已固定，不需要手动改。

**TokenHub 返回 401 / 403**：`deploy/.env` 里的 `TOUCHLEARN_LLM_KEY` 不是有效的 TokenHub Key，或 Key 没有开通 3D 模型权限。

**照片超过 6 MB**：TokenHub 会拒绝。先在本地压缩到 6 MB 以内，或换一张更小的照片。

**任务超时**：默认 600 秒。排队高峰可能超过，任务 ID 24 小时内有效；把 `TOUCHLEARN_3D_TIMEOUT` 调到 900 再重试。

**内存不足**：本机 98 万面索引 GLB 端到端峰值约 942 MB，非索引最坏情况约 1.38 GB。4 GiB + 2 GiB swap 足够跑一个 `high` 任务，但不要并发两个；多人同时生成时把 `TOUCHLEARN_3D_QUALITY` 降到 `standard` 或 `draft`，或把容器内存上限调高。

**`edge-tts 合成失败` / `TimeoutError`**：服务器到微软朗读端点的出站连接被限速或阻断。先确认容器能访问外网，再重试；持续失败说明该出口不可用，此时讲解稿和二维码仍然生成，前端会退回浏览器朗读。也可以换成 `TOUCHLEARN_TTS_VOICE=zh-CN-YunxiNeural` 再试。

**`未安装 edge-tts`**：镜像没有带 `tts` extra。确认 `Dockerfile` 里是 `pip install ... ".[docs-reader,tts]"`，然后 `docker compose ... build --no-cache` 重建。

**音频能下载但手机不播放**：确认服务端已返回 `Accept-Ranges: bytes`，并用 `curl -I -H "Range: bytes=0-1023" <音频地址>` 检查是否 206。旧镜像不支持 Range，需要升级到 0.3.5。
