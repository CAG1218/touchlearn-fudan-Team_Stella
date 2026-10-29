# Fix Report - 三维旋转查看缺块、文字/触点不清晰、缺少放大与全屏

## Bug 描述

在作业完成后的“预览与下载”页签打开“三维旋转查看”，出现三个问题：

1. 几乎所有旋转角度都会有一部分模型消失，像是被切掉或被底板盖住；
2. 触觉字、盲文点和曲线边缘发灰、发糊，放大后也不够清晰；
3. 只有拖动旋转，没有放大/缩小，也没有全屏查看。

## 问题根因

### 1. 缺块来自 Canvas 2D 画师算法，不是 STL 文件损坏

旧查看器把所有三角形投影到 Canvas 2D，再按三角形重心排序后依次绘制。这个做法没有深度缓冲：

- 触觉板的顶面和底面存在大量超大三角形，最大单三角形面积约 14,760 mm²；
- 大三角形的重心往往落在模型中心附近，和凸起字/点的重心顺序交错；
- 一旦底板的超大三角形被排在浮雕之后绘制，就会整块盖住浮雕；
- 不同视角下排序结果不同，于是表现为“换个角度就少一块”。

对 `model.stl` 的检查结果：39,768 个三角形，包围盒 `148 × 210 × 3.8 mm`，无 NaN、无退化三角形。文件本身是完好的，问题在渲染方式。

### 2. 字和点不清晰来自三处叠加

- Canvas 2D 逐三角形填充没有透视/光照，浮雕侧面和顶面颜色几乎一样；
- 画布按 CSS 像素 1:1 渲染（`devicePixelRatio = 1` 时缓冲区仅 627×320），1.5 mm 的盲文点在屏幕上只有约 2 px；
- 取景按整个包围球计算，平板模型在画布里只占约 60% 高度，进一步压缩了细节。

### 3. 全屏失效来自 CSS containing block

`.msg` 的入场动画包含 `transform` 且 `animation-fill-mode: both`。带 transform 的祖先会成为 `position: fixed` 后代的 containing block，所以 `.viewer-wrap.is-fullscreen` 虽然写了 `inset: 0`，实际是相对消息气泡定位，只得到 699×1368 的窄条，而不是视口尺寸。

### 4. Esc 一次关掉整个查看器

页面级 Esc 监听和查看器自己的 Esc 监听同时挂在 `document` 上。全屏时按 Esc，页面级监听先执行并调用 `closeViewer()`，查看器还没来得及退出全屏就被销毁了。

### 5. 异步详情返回会清掉正在看的 viewer

`ensureJobDetails()` 加载完成后会调用 `renderJobPanel(ui)`，直接替换 `panel.innerHTML`，把已经创建的 WebGL canvas 从 DOM 中摘掉，但 `ui.viewer` 仍指向旧上下文。

## 尝试记录

### 尝试 1：提高 Canvas 2D 分辨率、调整三角形排序权重

- 结果：失败
- 原因：排序权重只能缓解部分角度，无法从根本上解决超大三角形与浮雕的相互覆盖；只要没有逐像素深度测试，就一定会存在遮挡错误。

### 尝试 2：改用 WebGL 深度缓冲

- 结果：成功
- 原因：`DEPTH_TEST + LEQUAL` 按像素比较深度，三角形绘制顺序不再影响遮挡；`depthBits = 24`，对 3.8 mm 厚的平板和 0.4–1.6 mm 的浮雕足够。

### 尝试 3：直接给 `.viewer-wrap` 加 `position: fixed`

- 结果：失败
- 原因：`.msg` 的 transform 动画形成 containing block，fixed 相对 `.msg` 定位。
- 最终做法：进入全屏时把 `.viewer-wrap` 临时移动到 `document.body`，原位置用注释节点占位；退出全屏时放回占位节点。同时给关闭按钮加直接监听，避免移出卡片后收不到点击委托。

### 尝试 4：仅靠提高光照强度提升清晰度

- 结果：部分成功
- 原因：顶面变亮了，但顶面和浮雕顶面法线相同，仍然同色；转到背光角度时整块板还会发黑。
- 最终做法：在原有双光源上增加相机方向头灯和环境项，保证任何角度都不会全黑；同时用面积加权的 z 分布找出底板顶面，把高出的浮雕染成更深的绿色；再用 `OES_standard_derivatives` 的 `dFdx/dFdy` 在法线突变处压暗边，形成轮廓线。

## 最终方案

### 渲染

- WebGL1，开启 `DEPTH_TEST`、24 位深度缓冲、`antialias`；
- 逐面法线 + 双光源 + 相机头灯 + 高光，`gl_FrontFacing` 处理双面；
- 面积加权 z 直方图识别底板顶面，浮雕部分叠加颜色差；
- `OES_standard_derivatives` 法线导数轮廓，浏览器不支持时自动降级；
- 取景按默认视角下 8 个包围盒角的真实投影范围计算，不再按包围球，平板可多占约 15% 画面；
- 渲染缓冲区至少 2× 超采样（上限 2.5×、1200 万像素），1x 屏幕也能得到 1254×680 的缓冲区。

### 交互

- 单指/鼠标拖动旋转，俯仰限制在 ±1.5 rad；
- 滚轮以光标为中心缩放，双指捏合缩放并平移；
- 工具栏：缩小、放大、重置、全屏、关闭；
- 右下角实时显示缩放百分比；
- 双击重置；
- 全屏时 Esc 只退出全屏，再按 Esc 才关闭；
- 切换页签、重新渲染作业时自动销毁 viewer，避免 WebGL 上下文泄漏。

### 布局与生命周期

- 全屏时 viewer 移到 `body`，退出/关闭时按占位注释还原；
- 关闭按钮使用直接监听，兼容全屏移出卡片后的场景；
- `renderJobPanel()` 在 viewer 打开时跳过重绘，防止异步详情返回把 canvas 清掉；
- 打开 viewer 后把卡片滚动到 `#chatScroll` 的可视区域内，避免被底部输入框挡住；
- 默认画布高度改为 `clamp(280px, 46vh, 560px)`，小窗口下也能完整看到画布。

## 检查清单

- [x] 已对比 WebGL 1.0 规范：`DEPTH_TEST`、`depthFunc(LEQUAL)`、`OES_standard_derivatives` 扩展用法；
- [x] 已对比 CSS 规范：transform 祖先对 `position: fixed` 的 containing block 影响；
- [x] 已检查模块：`app.js` 的 `openViewer` / `createViewer` / `closeViewer` / `renderJobPanel` / 全局 Esc 监听，`styles.css` 的 viewer 规则，`index.html` 的图标 symbol；
- [x] 已检查性能：40k 三角形、2× 超采样、按需 `requestAnimationFrame` 重绘，静止时不持续渲染；
- [x] 已检查内存：`destroy()` 移除 ResizeObserver、document/window/canvas/按钮监听，取消未执行的动画帧；
- [x] 已检查兼容性：WebGL 不可用时抛出明确提示；不支持导数扩展时自动使用无轮廓着色；
- [x] `node --check src/touchlearn/agent/static/app.js` 通过；
- [x] `python -m unittest discover -s tests -t . -q`：125 个测试全部通过；
- [x] Edge CDP 端到端：默认视角、三个旋转角度、滚轮缩放、全屏、Esc 退出全屏、再次 Esc 关闭、全屏关闭按钮、切换页签均通过。

## 端到端结果

```text
viewer={"css":[627,280],"buffer":[1254,560],"depthBits":24,"error":0,"derivatives":true,"zoom":"100%"}
wheelZoom=160%
fullscreen={"directBodyChild":true,"rect":[746,436],"viewport":[746,436],"zoom":"100%"}
afterEscape={"exists":true,"fullscreen":false,"bodyLock":false,"inSlot":true}
afterSecondEscape=false
afterFullscreenClose={"viewer":false,"bodyLock":false,"bodyViewer":false,"slotHtml":0}
afterTabSwitch={"viewer":false,"tab":"合规报告","canvas":0}
```

## 经验教训

1. 三维网格不能用“按重心排序”的 2D 画师算法兜底，尤其是存在超大底板三角形时；深度缓冲是正确解法。
2. `position: fixed` 在带 transform 动画的祖先里会失效，遇到“全屏只占一小块”先查 containing block，而不是继续加宽高。
3. 多个 `document` 级快捷键监听要有优先级约定，否则一个按键会被多个处理器重复消费。
4. 异步渲染与手动创建的 WebGL 上下文要明确所有权：要么重绘前销毁，要么重绘时跳过，不能让它悬空。
5. 触觉模型的清晰度不只靠分辨率：取景、光照对比、浮雕颜色分层和边缘轮廓缺一不可。
