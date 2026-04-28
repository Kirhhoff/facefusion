# 需求文档：修复视频合成丢失换脸帧问题

## 引言

在非 turbo 模式的完整换脸流程中，发现换脸后的结果图片大部分都成功换脸了，但合成出来的视频中只有很少的片段被换脸。问题根因在于 `reassemble_video` 函数只能按原始文件名（如 `frame_00000001.png`）查找 FaceFusion 的输出文件，而当 FaceFusion job 部分失败或 job 内部使用临时文件名（如 `frame_00000001-worker-0-mb-0-0.png`）时，`reassemble_video` 找不到这些文件，导致 fallback 到原始未换脸的帧。

### 问题根因分析

FaceFusion 的 `job-run` 执行流程如下：

1. **`run_step`**: 每个 step 处理一帧，先输出到 `output_path`（如 `output_0/frame_00000001.png`），完成后将文件**重命名**为临时名 `step_output_path`（如 `output_0/frame_00000001-worker-0-mb-0-0.png`）
2. **`finalize_steps`**: 所有 steps 成功完成后，将临时文件**移回**原始 `output_path`
3. 如果某个 step 失败（如未检测到人脸），整个 job 中止，`finalize_steps` 不会被调用
4. 已完成但未被 finalize 的帧以临时名 `frame_XXXXXXXX-worker-X-mb-X-X.png` 留在 output 目录中

`reassemble_video` 当前只查找 `output_dir/frame_XXXXXXXX.png`（原始名），当文件以临时名存在时，fallback 到原始帧，导致大部分帧未使用换脸结果。

## 需求

### 需求 1：reassemble_video 能正确匹配 FaceFusion 输出文件

**用户故事：** 作为用户，我希望视频合成步骤能正确使用所有已成功换脸的帧，以便最终输出视频中包含完整的换脸效果。

#### 验收标准

1. WHEN `reassemble_video` 查找某帧的输出文件 AND 该帧的输出文件以 FaceFusion 临时名格式存在（`frame_XXXXXXXX-worker-X-mb-X-X.png`） THEN 系统 SHALL 能正确识别并使用该文件
2. WHEN `reassemble_video` 查找某帧 AND 该帧同时存在原始名文件和临时名文件 THEN 系统 SHALL 优先使用原始名文件
3. IF 某帧在 output 目录中既不存在原始名也不存在临时名文件 THEN 系统 SHALL fallback 到原始帧
4. WHEN 视频合成完成 THEN 系统 SHALL 报告使用了多少帧换脸结果、多少帧 fallback 到原始帧

### 需求 2：在 collect_swapped_keyframes 中也正确匹配临时名文件

**用户故事：** 作为用户，我希望关键帧收集步骤能正确找到所有已换脸的关键帧，以便 turbo 模式和非 turbo 模式的 keyframes_swapped 目录都包含完整结果。

#### 验收标准

1. WHEN `collect_swapped_keyframes` 查找某关键帧的输出 AND 该帧以 FaceFusion 临时名格式存在 THEN 系统 SHALL 能正确识别并使用该文件
2. WHEN 关键帧收集完成 THEN 系统 SHALL 报告找到和缺失的关键帧数量

### 需求 3：考虑 job 完全成功但 finalize 未执行的边界情况

**用户故事：** 作为用户，我希望即使 FaceFusion 内部流程出现异常（如 job 失败、进程崩溃等），已处理的帧也不会丢失，以便最大化利用已完成的工作。

#### 验收标准

1. WHEN FaceFusion job 部分失败 AND 部分 step 的输出文件以临时名留在 output 目录 THEN 系统 SHALL 仍能识别并使用这些成功处理的帧
2. WHEN FaceFusion 进程崩溃 AND output 目录中同时存在原始名和临时名文件 THEN 系统 SHALL 不重复使用同一帧

### 需求 4：增加诊断信息帮助排查问题

**用户故事：** 作为用户，我希望在视频合成时能看到详细的帧匹配统计，以便判断换脸效果是否完整。

#### 验收标准

1. WHEN 视频合成开始 THEN 系统 SHALL 打印每个 worker output 目录中的文件数量和命名模式
2. WHEN 帧匹配完成 THEN 系统 SHALL 打印统计信息：总帧数、成功匹配换脸帧数、fallback 原始帧数、完全缺失帧数
