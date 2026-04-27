# 实施计划

- [ ] 1. 重写 `detect_keyframe_indices()` 修复关键帧检测
   - 改用 `ffprobe -show_entries frame=pts_time,pict_type -of json` 替代 CSV 输出，解决格式不一致问题
   - 移除 `-ss`/`-to` 对 ffprobe 的传递，改为对完整视频检测后再按时间范围过滤，解决 seek 行为差异和帧序号偏移问题
   - 基于关键帧的 `pts_time` 与提取帧的 `pts_time` 做时间戳匹配来确定帧序号，而非依赖帧序号直接对齐
   - 添加错误处理：ffprobe 失败或返回空结果时打印明确警告
   - _需求：1.1、1.2、1.3、1.6_

- [ ] 2. 修复 `collect_keyframes()` 使其基于正确对齐的关键帧索引创建符号链接
   - 确保 `keyframe_indices` 中的序号与 `frames_dir` 中 `frame_00000001.png` 起始的文件名对齐
   - 验证 `keyframes_original/` 目录下的符号链接数量与检测到的关键帧数一致
   - _需求：1.4_

- [ ] 3. 修复 `collect_swapped_keyframes()` 使其正确匹配换脸后的关键帧
   - 确保使用修复后的关键帧名称集合，正确从 output_N/ 目录中收集换脸结果
   - 验证 `keyframes_swapped/` 目录下的符号链接数量与换脸成功的关键帧数一致
   - _需求：1.5_

- [ ] 4. 实现 Turbo Mode 的关键帧提取流程
   - 在 `extract_frames()` 中新增 turbo mode 分支：使用 ffmpeg `select='eq(pict_type\,I)'` 滤镜仅提取 I-frame，输出到 `keyframes_original/` 目录（直接提取图像，非符号链接）
   - 返回关键帧总数和总帧数供统计使用
   - _需求：2.1、6.1、6.3_

- [ ] 5. 实现 Turbo Mode 的关键帧换脸流程
   - 新增 `process_keyframes()` 函数：对 `keyframes_original/` 中的关键帧执行 FaceFusion 换脸，结果输出到 `keyframes_swapped/`
   - 复用现有 `launch_workers()` 的逻辑（构建 job JSON + worker 脚本），但将 target 指向关键帧目录
   - turbo mode 下不创建 `batch_N/` 和 `output_N/` 分段目录
   - _需求：2.2、2.3、4.3、6.2_

- [ ] 6. 修改 `main()` 函数实现 turbo mode 分支控制
   - turbo mode 流程：提取关键帧 → 关键帧换脸 → 统计报告 → 结束（跳过 Step 2 分段、Step 4 全帧监控、Step 5 视频重组）
   - 全帧模式流程保持不变，仍执行 Step 1.5 和 Step 4.5 收集关键帧符号链接
   - turbo mode 完成后打印提示信息：仅处理关键帧、输出位置、如何运行全帧处理
   - _需求：2.4、2.5、4.1、4.2_

- [ ] 7. 实现 Turbo Mode 统计报告
   - 统计关键帧总数、总帧数、关键帧占比百分比
   - 打印原始关键帧和换脸后关键帧的目录路径
   - 生成不带 `--turbo-mode` 的完整命令示例，方便用户直接复制执行全帧处理
   - _需求：3.1、3.2、3.3_

- [ ] 8. Turbo Mode 对 `--start-time`/`--end-time` 和多 `--source` 的支持
   - turbo mode 下的关键帧提取和 ffprobe 检测均需正确处理时间范围参数
   - 多 `--source` 图片在关键帧换脸中正常传递给 FaceFusion
   - 时间范围内无关键帧时打印警告并正常退出
   - _需求：5.1、5.2、5.3_
