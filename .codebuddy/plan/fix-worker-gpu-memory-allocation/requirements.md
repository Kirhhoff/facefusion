# 需求文档

## 引言

当并行启动多个 FaceFusion worker（每个 worker 是一个独立的 Python 子进程）时，ONNX Runtime 的 BFC Arena 在初始化阶段尝试分配 GPU 显存会偶发失败，报错：

```
Failed to allocate memory for requested buffer of size 33554432
```

核心问题是：所有 worker 子进程几乎同时启动、同时加载 ONNX 模型到 GPU，导致在初始化瞬间各 GPU 上的显存需求出现尖峰。虽然稳态运行时显存占用仅 25%~60%（脉冲式波动），但初始化时的并发分配峰值会导致 BFC Arena 分配失败，进而使整个 worker 的任务失败。

之前能跑 48 个 worker 不出问题可能是因为 CUDA 驱动或 ONNX Runtime 版本差异、GPU 状态碎片化等因素造成的"巧合"，而非架构上有容错机制。

**关键发现：**
1. `inference_manager.py` 中 `create_inference_session` 使用默认的 ONNX Runtime CUDA EP 选项，没有设置 `gpu_mem_limit`、`arena_extend_strategy` 等关键参数
2. 所有 worker 子进程同时启动（`launch_workers` 中 `Popen` 调用没有交错延迟），造成同一时刻大量进程争抢同一 GPU 的显存
3. `video_memory_strategy` 设为 `strict`/`moderate` 时会在每个 step 后释放模型，但 step 1 的首次加载仍然会并发，无法避免启动时的争抢
4. ONNX Runtime CUDA EP 默认的 `arena_extend_strategy` 是 `kNextPowerOfTwo`，会预分配比实际需求更大的显存块，加剧峰值问题
5. 报错中的 33554432 bytes = 32MB，说明是 BFC Arena 在尝试扩展 arena 时请求了一个较大的块但此时显存不足

## 需求

### 需求 1：Worker 启动交错延迟

**用户故事：** 作为 FaceFusion 管道运维人员，我希望多个 worker 子进程启动时有合理的交错延迟，以便同一 GPU 上的模型初始化不会在同一瞬间并发争抢显存。

#### 验收标准

1. WHEN `launch_workers` 函数启动多个 worker 子进程 THEN 系统 SHALL 在每个 `Popen` 调用之间引入可配置的延迟间隔（默认 2 秒）
2. WHEN 用户指定 `--worker-start-delay` 参数 THEN 系统 SHALL 使用用户指定的延迟值
3. IF 用户未指定延迟参数 THEN 系统 SHALL 使用默认延迟 2 秒
4. WHEN 所有 worker 均已启动 THEN 系统 SHALL 正常进入进度监控流程，无额外等待

### 需求 2：ONNX Runtime CUDA EP 显存分配策略优化

**用户故事：** 作为 FaceFusion 管道运维人员，我希望 ONNX Runtime 创建 CUDA 推理会话时使用更保守的显存分配策略，以便在多进程共享 GPU 场景下减少显存峰值占用和分配失败概率。

#### 验收标准

1. WHEN 创建 ONNX InferenceSession 且使用 CUDA execution provider THEN 系统 SHALL 设置 `arena_extend_strategy` 为 `kSameAsRequested`（仅分配实际所需大小，而非向上取整到 2 的幂）
2. WHEN 创建 ONNX InferenceSession 且使用 CUDA execution provider THEN 系统 SHALL 设置 `gpu_mem_limit` 为一个合理的上限值（默认不限制，但可通过配置设置）
3. IF 系统检测到 `execution_device_ids` 包含多个 GPU THEN 系统 SHALL 正常按设备 ID 分配推理会话，策略优化不影响设备选择逻辑

### 需求 3：推理会话创建失败时的重试机制

**用户故事：** 作为 FaceFusion 管道运维人员，我希望 ONNX 推理会话创建失败时系统能自动重试而非直接 fatal_exit，以便应对瞬态的显存分配失败。

#### 验收标准

1. WHEN `create_inference_session` 因 BFC Arena 分配失败抛出异常 THEN 系统 SHALL 在放弃前自动重试最多 N 次（默认 3 次）
2. WHEN 重试时 THEN 系统 SHALL 在每次重试之间等待递增的时间（指数退避，初始 2 秒，最大 10 秒）
3. IF 所有重试均失败 THEN 系统 SHALL 执行原有的 `fatal_exit(1)` 逻辑
4. WHEN 首次创建即成功 THEN 系统 SHALL 不引入任何额外延迟

### 需求 4：mini-batch 之间模型不释放（非 strict 模式）

**用户故事：** 作为 FaceFusion 管道运维人员，我希望在 `video_memory_strategy` 不是 `strict` 时，同一 worker 的多个 mini-batch 之间保留已加载的模型，以便避免反复加载模型造成的显存脉冲和性能浪费。

#### 验收标准

1. WHEN `video_memory_strategy` 为空或 `tolerant` 且 worker 正在处理同一 mini-batch 内的多个 step THEN 系统 SHALL 在 step 之间保留推理模型在 GPU 上
2. WHEN `video_memory_strategy` 为 `moderate` THEN 系统 SHALL 仅在 mini-batch 边界释放处理器模型（face_swapper 等），保留通用模型（face_detector、face_recognizer 等）
3. WHEN `video_memory_strategy` 为 `strict` THEN 系统 SHALL 在每个 step 后释放所有模型（保持原有行为）
4. IF mini-batch 切换导致模型释放 THEN 下一个 mini-batch 的首次推理 SHALL 重新加载所需模型
