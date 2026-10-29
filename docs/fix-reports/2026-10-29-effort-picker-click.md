# Fix Report - 思考强度入口可见但无法切换

## Bug 描述

在 8765 实例的模型弹层里，底部可以看到「思考强度 低」入口，但点击后无法进入强度列表，弹层直接消失，底部标签始终停在「思考低」。

复现路径：

1. 打开页面并登录；
2. 点击右下角模型标签，弹出模型列表；
3. 点击列表底部的「思考强度」；
4. 期望出现低 / 中 / 高三个选项，实际弹层关闭，强度没有变化。

## 问题根因

不是 CSS 遮挡，也不是接口拒绝，而是**事件冒泡与弹层重绘的时序冲突**。

`#modelPopover` 上挂了事件委托。点击「思考强度」入口时，处理函数调用 `renderModelPopover()`，用 `innerHTML` 把模型列表整体替换成强度列表：

```js
if (event.target.closest("[data-effort-view]")) {
  state.popoverView = "effort";
  renderModelPopover();
  return;
}
```

被点击的原按钮在这一步已经从文档中移除。事件继续冒泡到 `document` 上的「点击外部关闭」监听时：

```js
if (event.target.closest("#modelPopover") || event.target.closest("#modelChip")) return;
closeModelPopover();
```

`event.target` 是已经脱离文档的旧按钮，`closest("#modelPopover")` 返回 `null`，于是被判定为外部点击，弹层立刻关闭。用户看到的只是入口闪一下，强度列表根本没有机会停留。

用 Edge 最小页面复现，重绘后事件到达 `document` 时：

```text
isConnected=false; closestPop=false
```

同一问题也影响强度页的返回按钮，因为它同样会在点击时重绘弹层。

## 尝试记录

### 尝试 1：检查样式与点击热区

- 结果：排除
- 原因：`.model-popover`、`.effort-entry` 的层级、尺寸、`pointer-events` 均正常，入口可见且可命中；`z-index: 40` 没有被底部输入区遮挡。

### 尝试 2：在全局监听里判断 `event.target.isConnected`

- 结果：可行，但未采用
- 原因：能绕过脱离文档的目标，但只是在外层兜底；弹层内部每次就地重绘都要依赖这个判断，后续新增内页仍然容易踩坑。

### 尝试 3：在弹层根节点阻止冒泡

- 结果：成功
- 原因：弹层内部的点击本来就不应该触发「点击外部关闭」。在 `#modelPopover` 的委托监听开头调用 `stopPropagation()`，事件不再到达 `document`，与是否重绘无关。

## 最终方案

在 `src/touchlearn/agent/static/app.js` 的弹层点击监听开头阻止冒泡：

```js
$("#modelPopover").addEventListener("click", (event) => {
  /* Inner clicks can re-render the popover; stop here so the document-level
     outside-click handler never sees the now-detached original target. */
  event.stopPropagation();
  ...
});
```

外部点击不受影响：点击弹层之外的元素不会进入这个监听，`document` 上的关闭逻辑照常执行。

## 验证

- `node --check src/touchlearn/agent/static/app.js` 通过。
- 用 Edge 无头浏览器 + CDP 对 8765 跑完整用户路径：
  - 登录后标签为 `V4.1 Flash · 思考低`；
  - 打开弹层，点击「思考强度」，弹层保持打开，出现 3 个强度选项，标题为「思考强度」；
  - 选择「中」后标签变为 `V4.1 Flash · 思考中`，`localStorage` 写入 `{"model":"deepseek-v4.1-flash","effort":"medium"}`，提示「思考强度已设为「中」」；
  - 强度页返回按钮可用，返回后仍是 9 个模型；
  - 点击弹层外部仍会正常关闭。
- `python -m unittest discover -t . -s tests`：125 个测试全部通过。
- 静态文件响应带 `Cache-Control: no-store`，8765 无需重启，普通刷新即可加载修复后的 `app.js`。

## 检查清单

- [x] 对比官方文档：MDN `Event.stopPropagation()`、`Event.target`、`Node.isConnected` 与 `innerHTML` 替换导致节点脱离文档的行为。
- [x] 检查模块：`app.js` 弹层事件委托与渲染、`styles.css` 弹层布局与层级、`server.py` 静态文件响应头。
- [x] 性能检查：只增加一次冒泡拦截，没有新增监听器、定时器或额外重绘，不存在循环触发。

## 经验教训

1. **事件委托里重绘自身子树时，必须考虑事件后续的冒泡路径。** 重绘会让 `event.target` 脱离文档，任何依赖 `closest()` 判断「点击是否在容器内」的外层监听都会误判。
2. **「点击外部关闭」应基于点击来源判断，而不是基于重绘后的 DOM 关系。** 在容器根节点拦截冒泡，比在每个子页面里单独处理更稳。
3. **菜单套子菜单的交互要单独做 E2E。** 只验证一级菜单（选模型）发现不了这个问题，因为一级选择本来就会主动关闭弹层，把缺陷掩盖了。
