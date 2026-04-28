# 实施计划

- [ ] 1. 新增帧查找辅助函数 `find_output_frame`
   - 创建函数，输入 `output_dir` 和原始帧名（如 `frame_00000001.png`），返回该帧对应的实际输出文件路径
   - 查找优先级：原始名文件 > 临时名文件（匹配模式 `frame_XXXXXXXX-worker-*-mb-*.png`）> None
   - 避免重复匹配同一帧（同一原始帧名只返回一个临时名文件）
   - _需求：1.1、1.2、3.2_

- [ ] 2. 修改 `reassemble_video` 使用新的帧查找逻辑
   - 将当前硬编码的 `os.path.exists(output_frame)` 替换为调用 `find_output_frame`
   - 增加帧匹配统计：总帧数、成功匹配换脸帧数、fallback 原始帧数、完全缺失帧数
   - _需求：1.1、1.2、1.3、1.4、4.2_

- [ ] 3. 修改 `collect_swapped_keyframes` 使用新的帧查找逻辑
   - 将当前硬编码的 `os.path.exists(output_frame)` 替换为调用 `find_output_frame`
   - 增加缺失关键帧的统计报告
   - _需求：2.1、2.2_

- [ ] 4. 增加诊断信息输出
   - 在 `reassemble_video` 开始时，打印每个 worker output 目录中的文件数量和命名模式分布
   - 在帧匹配完成后，打印详细统计信息
   - _需求：4.1、4.2_

- [ ] 5. 端到端验证
   - 使用非 turbo 模式运行完整换脸流程，确认输出视频中换脸帧覆盖完整
   - 检查 output 目录中同时存在原始名和临时名文件时的优先级是否正确
   - 检查 job 部分失败时，已处理帧是否被正确收集
   - _需求：1.1、1.2、1.3、1.4、2.1、3.1、3.2_
