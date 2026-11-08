# Fix Report - 交付 STL 在 float32 量化后出现非流形边

日期：2026-11-08　范围：确定性几何内核、两个基准案例、初赛测试样例

## Bug 描述

提交前用项目自己的 `read_stl_mesh()` 读回已生成的 STL，`report.json` 写的是
`watertight: true`，但读回结果却是：

- 机械臂：`open_edges=0`，`nonmanifold_edges=6`
- 函数：`open_edges=0`，`nonmanifold_edges=10`

也就是说，内存中的网格通过检查，落盘后的文件却通不过同一套检查。这会让
“STL 水密”的声明无法由交付文件复现。

## 问题根因

`build_mesh()` 给每个触觉图元单独生成闭合壳，并用 `shell_index * 1e-7` 的
z 偏移避免相邻壳共用边。但二进制 STL 的坐标是 float32：

- z ≈ 3.8 mm 处 float32 的最小间隔约 `4.77e-7`；
- `1e-7` 的步长在写入时被四舍五入掉，多个壳落到同一个 z；
- 相邻笔画/图元原本只差一个微小偏移，量化后在文件里完全重合；
- 于是同一条边被 4 个三角形共用，读回即报非流形。

内存网格用 float64 判断，所以构建时的检查仍然是 `true`，问题只在序列化后暴露。

## 尝试记录

### 尝试 1：只在读回时容许非流形边

- 结果：不可行
- 原因：这会改掉项目自己写的“每条边恰好两个三角形”判定，等于为了通过检查而放宽标准。

### 尝试 2：把 z 偏移从 1e-7 放大到 1e-5

- 结果：可分离，但副作用明显
- 原因：图元数量多时累计高度会改变最高点，`3.8 mm` 的尺寸和报告数字不再稳定。

### 尝试 3：在 x/y 方向给每个壳一个唯一的微小偏移

- 结果：成功
- 原因：x/y 偏移不改变板厚和浮雕高度；`1e-5 mm` 步长在 float32 下可表示，
  且远小于 0.1 mm 的触觉特征尺度。两个方向组合可覆盖 66,049 个壳，当前两个
  基准案例约 1,500 个壳，不会重复。

## 最终方案

`src/touchlearn/geometry.py` 的 `build_mesh()` 改为：

```python
dx = ((shell_index % 257) - 128) * 1e-5
dy = (((shell_index // 257) % 257) - 128) * 1e-5
shifted = [(x + dx, y + dy) for x, y in polygon]
triangles.extend(prism_triangles(shifted, z0, z1, layout.height))
```

z 坐标保持精确的 `base_thickness - embed` 到 `base_thickness + height`，
不再使用会被 float32 吞掉的 z 抖动。

新增回归测试
`tests/test_audio_trace_stl.py::test_written_stl_is_watertight_after_float32_round_trip`：
两个基准案例写盘后用 `read_stl_mesh()` 读回，要求 `open_edges=0` 且
`nonmanifold_edges=0`。

## 验证结果

| 案例 | 三角面 | 尺寸 (mm) | 体积 (mm³) | 读回 open | 读回 nonmanifold |
|---|---:|---|---:|---:|---:|
| 机械臂 | 18,756 | 148 × 210 × 3.8 | 96,938.755 | 0 | 0 |
| 函数 | 20,652 | 148 × 210 × 3.8 | 95,555.098 | 0 | 0 |

全量测试：`Ran 382 tests`，`OK`。两个案例重新构建后 `PASS`，三角面数和体积与修复前一致。

## 检查清单

- [x] 对比 STL 二进制格式：坐标字段为 32 位浮点，量化误差不可避免；
- [x] 检查 `build_mesh()` 的壳偏移逻辑与 `Mesh.topology()` 的精确边计数；
- [x] 用项目自己的 `read_stl_mesh()` 读回文件复现问题，而不是只看内存结果；
- [x] 修复后重新构建两个案例，比较三角面数、尺寸、体积；
- [x] 增加读回回归测试，防止以后再把偏移改回 float32 不可表示的量级。

## 经验教训

“构建时检查通过”不等于“交付文件通过”。只要产物经过有损序列化，就必须
**读回产物再检查一次**；尤其是水密性、尺寸和哈希这类会被写进报告的数字。
微小偏移不能凭直觉取 `1e-7`，要先确认目标格式能表示这个量级。
