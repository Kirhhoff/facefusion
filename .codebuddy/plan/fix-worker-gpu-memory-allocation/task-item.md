# 实施计划

- [ ] 1. Worker 启动交错延迟
   - 修改 `video_face_swap.py` 中 `launch_workers` 函数，在每个 `Popen` 调用之间引入 `time.sleep(worker_start_delay)` 延迟
   - 在 `facefusion/args.py` 中添加 `--worker-start-delay` 命令行参数，类型 float，默认值 2.0
   - 在 `facefusion.ini` 中添加 `worker_start_delay = 2.0` 配置项
   - _需求：1.1、1.2、1.3、1.4_

- [ ] 2. ONNX Runtime CUDA EP 显存分配策略优化
   - 修改 `facefusion/execution.py` 中 `create_inference_providers` 函数，为 CUDA EP provider_options 添加 `arena_extend_strategy` 字段，值为 `'kSameAsRequested'`
   - 在 `facefusion/args.py` 中添加 `--gpu-mem-limit` 命令行参数（可选，单位 GB，默认 0 表示不限制），并将其传入 CUDA EP 的 `gpu_mem_limit` 字段（转换为字节）
   - 确保多 GPU 场景下 `device_id` 分配逻辑不受影响
   - _需求：2.1、2.2、2.3_

- [ ] 3. 推理会话创建重试机制
   - 修改 `facefusion/inference_manager.py` 中 `create_inference_session` 函数，包裹 `InferenceSession` 创建逻辑为重试循环
   - 默认重试 3 次，使用指数退避（初始 2 秒，最大 10 秒），仅捕获 ONNX Runtime 相关异常
   - 所有重试失败后仍调用 `fatal_exit(1)`，首次成功则不引入延迟
   - _需求：3.1、3.2、3.3、3.4_

- [ ] 4. mini-batch 间模型保留（非 strict 模式）
   - 修改 `facefusion/workflows/image_to_image.py` 和相关处理器调用逻辑，在 `video_memory_strategy` 非 strict 时跳过 step 间的模型释放
   - moderate 模式：仅释放处理器模型（face_swapper 等），保留通用模型（face_detector、face_recognizer）；tolerant/默认模式：保留所有模型
   - strict 模式保持原有行为不变（每个 step 后释放所有模型）
   - _需求：4.1、4.2、4.3、4.4_

- [ ] 5. 集成测试与验证
   - 使用 32 个 worker 运行完整的视频换脸管道，验证不再出现 BFC Arena 分配失败
   - 验证 `--worker-start-delay 0` 可回退到原始无延迟行为
   - 验证 `--gpu-mem-limit` 参数生效（设一个较小值确认 session 创建时确实受限）
   - 验证重试机制日志输出（模拟显存不足场景）
   - _需求：1.1~1.4、2.1~2.3、3.1~3.4、4.1~4.4_
