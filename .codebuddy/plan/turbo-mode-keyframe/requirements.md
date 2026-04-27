# 需求文档：Turbo Mode（关键帧模式）

## 引言

Turbo Mode 是 video_face_swap.py 的一个重要运行模式，旨在让用户能够快速预览换脸效果，而无需处理视频的每一帧。当处理长视频（数千甚至数万帧）时，全帧换脸耗时极长。Turbo Mode 通过只提取和换脸关键帧（I-frame），将工作量降低到原来的 1%~5%，让用户可以在几分钟内看到换脸效果的预览，然后根据预览质量决定是否处理全视频。

当前代码存在两个问题：
1. **`--turbo-mode` 参数虽已定义，但 `main()` 中从未实际使用该参数**。现有的 `detect_keyframe_indices()` 和 `collect_keyframes()` 函数只在全帧处理流程结束后以零额外开销方式收集关键帧的符号链接，并不能实现"只处理关键帧"的核心目标。
2. **现有的关键帧检测功能完全失效**，用户反馈在完成视频换脸后查看关键帧目录，发现里面都是空的。根本原因如下：
   - `detect_keyframe_indices()` 使用 `-ss`/`-to` 选项配合 `ffprobe -show_frames` 时，ffprobe 的 seek 行为与 ffmpeg 不同，可能导致返回空结果或帧序号偏移
   - **帧序号不对齐**：ffprobe 返回的帧序号是从整个视频的第1帧开始计数的，但如果用了 `start_time`，ffmpeg 提取帧时只提取时间范围内的帧（从 `frame_00000001.png` 开始），而 ffprobe 的帧序号则从原始视频的第N帧开始。这导致 `keyframe_indices` 中的序号与 `frames_dir` 中的文件名完全对不上
   - `collect_keyframes()` 使用 `enumerate(all_frames, start=1)` 作为1-based帧索引来匹配 `keyframe_indices`，但由于上述序号偏移问题，匹配必然失败
   - 对于某些视频格式，ffprobe 的 `pict_type` 输出格式可能不一致（如包含额外字段），导致 CSV 解析失败

## 需求

### 需求 1：修复关键帧检测功能

**用户故事：** 作为一名视频换脸用户，我希望即使在全帧模式下，关键帧目录也能正确收集到关键帧文件，以便快速浏览换脸前后的关键帧对比。

#### 验收标准

1. WHEN 调用 `detect_keyframe_indices()` THEN 系统 SHALL 返回与 `frames_dir` 中提取帧序号对齐的关键帧索引集合
2. IF 指定了 `start_time` THEN 系统 SHALL 正确处理帧序号偏移，使返回的索引与从 `start_time` 开始提取的帧编号（`frame_00000001.png`）对应
3. WHEN `ffprobe` 命令执行失败或返回空结果 THEN 系统 SHALL 打印明确的错误/警告信息，而非静默返回空集合
4. WHEN `collect_keyframes()` 执行后 THEN `keyframes_original/` 目录 SHALL 包含与检测到的关键帧数量一致的符号链接文件（非0个）
5. WHEN `collect_swapped_keyframes()` 执行后 THEN `keyframes_swapped/` 目录 SHALL 包含与换脸成功的关键帧数量一致的符号链接文件
6. IF 关键帧检测方式不可靠 THEN 系统 SHALL 使用替代方案（如基于帧时间戳匹配或 ffmpeg `select` 滤镜）来确保关键帧提取的可靠性

### 需求 2：Turbo Mode 关键帧提取与换脸

**用户故事：** 作为一名视频换脸用户，我希望开启 turbo mode 后只对关键帧执行换脸操作，以便在几分钟内快速预览换脸效果，而不是等待数小时处理全部帧。

#### 验收标准

1. WHEN 用户指定 `--turbo-mode` THEN 系统 SHALL 仅提取视频的关键帧（I-frame），而非全部帧
2. WHEN turbo mode 开启 THEN 系统 SHALL 只对关键帧执行 FaceFusion 换脸处理，跳过所有非关键帧
3. WHEN turbo mode 换脸完成 THEN 系统 SHALL 在 `keyframes_swapped/` 目录下输出换脸后的关键帧图像
4. IF turbo mode 开启 THEN 系统 SHALL NOT 执行视频重组（Step 5），因为关键帧不足以重建完整视频
5. WHEN turbo mode 开启 THEN 系统 SHALL 打印清晰的提示信息，说明仅处理了关键帧、输出位置及如何基于此预览决定下一步操作

### 需求 3：关键帧数量统计与预览报告

**用户故事：** 作为一名视频换脸用户，我希望在 turbo mode 运行结束后看到关键帧数量、占比等统计信息，以便评估关键帧的覆盖程度和换脸效果。

#### 验收标准

1. WHEN turbo mode 完成关键帧换脸 THEN 系统 SHALL 打印统计报告，包括：关键帧总数、总帧数、关键帧占比百分比、输出目录路径
2. WHEN turbo mode 完成后 THEN 系统 SHALL 提示用户如何查看原始关键帧（`keyframes_original/`）和换脸后关键帧（`keyframes_swapped/`）以便对比
3. WHEN turbo mode 完成后 THEN 系统 SHALL 提示用户如何运行全帧处理（提供完整的非 turbo 命令示例）

### 需求 4：Turbo Mode 与全帧模式的兼容性

**用户故事：** 作为一名视频换脸用户，我希望在不使用 `--turbo-mode` 时，流程与当前完全一致，同时关键帧收集功能仍然可用（且这次是真正可用的），以便在两种模式下都能获得关键帧对比信息。

#### 验收标准

1. IF `--turbo-mode` 未指定 THEN 系统 SHALL 执行与当前完全相同的全帧处理流程（提取所有帧 → 全部换脸 → 重组视频）
2. IF `--turbo-mode` 未指定 THEN 系统 SHALL 仍然执行 Step 1.5 和 Step 4.5，收集 `keyframes_original/` 和 `keyframes_swapped/` 的符号链接（保留现有功能，但修复后的版本应正确工作）
3. WHEN turbo mode 开启 THEN 系统 SHALL NOT 创建全帧的 batch_N/ 分段和 output_N/ 目录（因为只处理关键帧，无需全帧分段）

### 需求 5：Turbo Mode 支持时间范围与多源图片

**用户故事：** 作为一名视频换脸用户，我希望 turbo mode 支持 `--start-time`、`--end-time` 和多个 `--source` 图片，以便对特定片段和多人脸场景也能快速预览。

#### 验收标准

1. WHEN turbo mode 与 `--start-time` / `--end-time` 一起使用 THEN 系统 SHALL 仅在指定时间范围内检测和提取关键帧
2. WHEN turbo mode 与多个 `--source` 图片一起使用 THEN 系统 SHALL 将多个源图片传递给 FaceFusion（与全帧模式行为一致，支持多人脸平均）
3. IF 指定时间范围内没有检测到关键帧 THEN 系统 SHALL 打印警告信息并正常退出（不崩溃）

### 需求 6：关键帧目录结构清晰

**用户故事：** 作为一名视频换脸用户，我希望 turbo mode 的输出目录结构清晰且易于浏览，以便快速找到和对比关键帧。

#### 验收标准

1. WHEN turbo mode 开启 THEN 系统 SHALL 将原始关键帧图像（非符号链接，因为是直接提取）放入 `keyframes_original/` 目录
2. WHEN turbo mode 开启 THEN 系统 SHALL 将换脸后的关键帧图像放入 `keyframes_swapped/` 目录
3. WHEN turbo mode 开启 THEN 系统 SHALL 保持关键帧文件名与帧序号对应（如 `frame_00000042.png`），方便定位到视频时间点
4. IF 全帧模式运行 THEN 系统 SHALL 继续使用符号链接方式收集关键帧（当前行为不变）
