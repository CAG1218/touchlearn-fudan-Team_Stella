# Fix Report - 中文文件名上传被判为“未知文件类型”

## Bug 描述

线上环境（`http://47.103.61.115:8765`）上传中文名图片后，每一次分析都返回同一条错误：

> 这次没有理解成功：不支持的文件类型：未知（支持 PNG/JPG/PDF/STL/OBJ/TXT/JSON）

复现材料：

- `路线图.jpg`（连续三次重试，全部失败）
- `尾翼图.png`
- `拍照图.png`

同样内容、改成 ASCII 文件名（如 `route.jpg`）即可通过。说明问题与图片内容、大小、格式无关，只与文件名有关。

## 问题根因

`src/touchlearn/agent/agent.py` 的 `_save_uploads()` 在判断类型前，先对整个文件名（含扩展名）调用 `_slug()`：

```python
name = _slug(str(item.get("name") or f"upload-{index + 1}"), f"upload-{index + 1}")
extension = Path(name).suffix.lower()
```

而 `_slug()` 是为 ASCII 标识符设计的：

```python
clean = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
```

对 `路线图.jpg` 的实际执行过程：

1. `路线图` 三个字符都不在 `A-Za-z0-9._-` 内，整段被替换成一个 `-` → `-.jpg`；
2. `.strip("-._")` 去掉首尾的 `-`、`.`、`_` → `jpg`；
3. `Path("jpg").suffix` 为空 → 进入 `extension not in ALLOWED_UPLOADS` 分支，报“未知”。

也就是说，**扩展名前面的那个点在清洗时被当成无意义前缀剥掉了**。所有以非 ASCII 字符开头、且后续没有 ASCII 字符的文件名都会命中，不只是中文：日文、俄文、emoji 开头的文件名同样会失败。

这是纯本地逻辑错误，与模型、网络、部署无关。线上和本地跑的是同一份代码，所以本地用同样的文件名可以稳定复现。

## 尝试记录

### 尝试 1：怀疑前端没有把 MIME 或文件名传给后端

- 结果：排除
- 原因：前端用 `FormData`/JSON 发送 `file.name`，`server.py` 的 `/api/analyze` 原样把 payload 交给 `agent.analyze()`，中间没有解析或重写文件名的逻辑；错误文案出现在 `agent.py:694` 之前，属于本地校验。

### 尝试 2：直接复现清洗结果

- 结果：成功定位
- 原因：用解释器跑 `_slug("路线图.jpg")`，得到 `"jpg"`，`Path("jpg").suffix == ""`，与线上“未知”完全对应；对照组 `IMG_20261030.jpg`、`route map.png` 均正常。

### 尝试 3：把 `_slug()` 改成允许 Unicode

- 结果：否决
- 原因：`_slug()` 同时用于草稿 id、作业 id、场景块 id 等地方，允许 Unicode 会改变这些 id 的生成结果，影响已有作业目录、测试断言和外部约定。修一个上传问题不应该动全局标识符规则。

### 尝试 4：新增 `_upload_name()`，先拆扩展名再清洗主干

- 结果：采用
- 原因：改动只落在上传路径；扩展名被独立保留并统一小写；主干允许 Unicode 单词字符，中文名不再被抹成 `upload-1.jpg`，用户和审计记录里看到的仍是自己的文件名。

## 最终方案

`agent.py` 新增：

```python
def _upload_name(value: str, fallback: str = "upload") -> str:
    base = str(value).replace("\\", "/").split("/")[-1]
    path = Path(base)
    suffix = path.suffix.lower()
    stem = re.sub(r"[^\w.-]+", "-", path.stem, flags=re.UNICODE).strip("-._")
    return (stem[:80] or fallback) + suffix
```

`_save_uploads()` 改为：

```python
name = _upload_name(str(item.get("name") or f"upload-{index + 1}"), f"upload-{index + 1}")
extension = Path(name).suffix.lower()
```

行为对照：

| 输入 | 修复前 | 修复后 |
|---|---|---|
| `路线图.jpg` | `jpg` → 未知类型 | `路线图.jpg` |
| `尾翼图.png` | `png` → 未知类型 | `尾翼图.png` |
| `route map.png` | `route-map.png` | `route-map.png` |
| `C:\fakepath\图.pdf` | `C-fakepath--pdf` → 未知 | `图.pdf` |
| `../../etc/passwd.txt` | `etc-passwd.txt` | `passwd.txt` |

安全边界没有放宽：仍然只取最后一段文件名（去掉 `/` 与 `\` 路径），控制字符和符号仍被替换，长度仍限制 80 字符，扩展名仍必须命中 `ALLOWED_UPLOADS`。

## 检查清单

- [x] 对比官方文档：`pathlib.Path.stem/suffix` 对多点文件名与无扩展名文件的定义；`re` 的 `\w` 在 `str` 模式下按 Unicode 匹配；`os.fsencode` 在 Linux/Windows 下对 UTF-8 文件名的处理
- [x] 检查模块：`agent._save_uploads`、`agent.analyze`、`server._handle_analyze`、前端上传构造、`documents` 读取（按扩展名分派）
- [x] 性能检查：单个文件一次正则替换，O(n)，n 为文件名长度（≤80 截断），无额外 IO；不改变上传并发行为
- [x] 回归测试：新增 `test_uploaded_file_with_chinese_name_keeps_extension`、`test_uploaded_image_with_chinese_name_is_accepted`
- [x] 全量测试：257 项通过（含 2 项新增回归），41.1 s，未调用付费接口
- [x] 端到端验证：本地起服务后走真实 HTTP 链路 `POST /api/analyze`，上传 `路线图.jpg`、`尾翼图.png`，草稿记录中文件名分别保留为 `路线图.jpg`（U+8DEF U+7EBF U+56FE）与 `尾翼图.png`（U+5C3E U+7FFC U+56FE），`extension` 为 `.jpg` / `.png`，文件落盘成功
- [x] 兼容性：ASCII 文件名的清洗结果与修复前一致，不改变已有作业目录结构

## 上线注意

这次改的是镜像内的 Python 代码，服务器上必须**重新构建镜像**，只 `up -d` 不会生效。线上是按发布包部署的，更新流程：

```bash
# 本机：重新打包并上传（包内不含 deploy/.env，不会覆盖服务器密钥）
scp release/touchlearn-0.3.0.tar.gz root@47.103.61.115:/root/

# 服务器：解压 + 重建
cd /root/touchlearn-fudan-Team_Stella      # 以实际目录为准
cp src/touchlearn/agent/agent.py /root/agent.py.bak-$(date +%F-%H%M)
tar -xzf /root/touchlearn-0.3.0.tar.gz -C .
cd deploy
docker compose --env-file .env -f docker-compose.ip.yml up -d --build --remove-orphans
```

验证：登录后上传 `路线图.jpg`，草稿卡应显示原文件名且不再报“未知”；容器内可跑

```bash
docker compose --env-file .env -f docker-compose.ip.yml exec -T touchlearn \
  python -c "from touchlearn.agent.agent import _upload_name; print(_upload_name('路线图.jpg'))"
```

应输出 `路线图.jpg`。

## 经验教训

1. **清洗函数要区分“标识符”和“文件名”。** 标识符可以丢弃一切非 ASCII 字符；文件名必须保住扩展名，否则类型判断、媒体类型和用户认知同时失效。
2. **`strip("-._")` 是这次的关键。** 去掉首尾分隔符看似无害，实际会把“仅由非 ASCII 字符组成的主干 + 扩展名”压成裸扩展名。以后写清洗逻辑，先拿中文名、纯符号名、多点文件名各跑一遍。
3. **报错文案要带原始文件名。** 当时只显示“未知”，用户无法判断是格式问题还是文件名问题。修复后仍建议在 `ValueError` 里带上原始名，便于线上排查。
4. **可复现的最小输入优先。** 从截图到根因只用了三行解释器输出，比在浏览器的 Network 面板里绕一圈快得多。
