# 需求文档

## 引言

当前 `video_face_swap.py` 多 worker 并行执行 facefusion 时，存在两个关键问题：

1. **GPU 内存波动导致 OOM**：在 `moderate` 策略下，GPU 显存使用率在 75%~100% 之间波动，偶尔触发 ONNX Runtime 的 `BFCArena::AllocateRawInternal` 内存分配失败，导致 worker 进程异常退出，整个 job 中断。
2. **Worker 无重试机制**：当前 worker 的 shell 脚本中，一旦 `job-run` 失败就立即退出（`exit $_job_exit`），没有重试逻辑。由于多 worker 共享 GPU，当某个 worker OOM 时，其他 worker 可能正在释放内存，稍后重试就有可能成功。

本功能旨在：
- 优化 GPU 内存策略，减少 OOM 发生概率
- 为 worker 的 mini-batch 执行添加自动重试机制，使 OOM 等瞬态错误可以通过重试自动恢复

## 需求

### 需求 1：优化视频内存策略选择

**用户故事：** 作为运维人员，我希望系统能够使用更宽松的 GPU 内存策略来减少 OOM 错误，以便多 worker 并行处理时更稳定。

#### 验收标准

1. WHEN `video_face_swap.py` 生成 worker 命令时 THEN 系统 SHALL 在 `facefusion.ini` 配置为空时，默认将 `--video-memory-strategy` 设置为 `tolerant`
2. IF 用户在 `facefusion.ini` 中显式配置了 `video_memory_strategy` THEN 系统 SHALL 使用用户配置的值，不覆盖
3. WHEN 使用 `tolerant` 策略时 THEN facefusion 的 `post_process()` 不会清理 processor 级别的 inference pool（模型常驻内存），避免反复加载模型导致的内存波动
4. WHEN 使用 `moderate` 策略时 THEN facefusion 的 `post_process()` 会在每个 step 完成后清理 processor 级别的 inference pool，导致内存波动（75%~100%），这就是当前 OOM 的根因

**技术分析：** 三种策略的行为差异：
- `strict`：每步完成后清理所有模型（包括 content_analyser、face_classifier 等共享模型）
- `moderate`：每步完成后清理 processor 自身的模型（如 face_swapper 模型），但保留共享模型
- `tolerant`：不清理任何模型，所有模型常驻内存，GPU 利用率稳定

使用 `tolerant` 的权衡：
- **好处**：GPU 内存使用稳定，不会因模型反复加载/释放导致内存波动和 OOM
- **坏处**：GPU 显存占用更高（因为模型始终驻留），在显存较小的机器上可能导致初始加载就 OOM；但在当前 4 卡并行的场景下，每卡只跑 1 个 worker，模型常驻完全可行

### 需求 2：Worker Mini-Batch 自动重试机制

**用户故事：** 作为运维人员，我希望 worker 在遇到 GPU 内存分配失败等瞬态错误时能自动重试，以便整个任务不会因临时的资源竞争而中断。

#### 验收标准

1. WHEN worker 的 `job-run` 命令以非零退出码失败时 THEN 系统 SHALL 自动重试该 mini-batch，而不是直接退出
2. IF 重试次数未超过最大限制 THEN 系统 SHALL 在等待一段退避时间后重新执行 `job-run` 命令
3. WHEN 重试时 THEN 系统 SHALL 使用指数退避策略（初始等待时间可配置），避免多个 worker 同时重试导致 GPU 内存再次竞争
4. IF 重试次数超过最大限制 THEN 系统 SHALL 记录最终失败信息并退出 worker 进程
5. WHEN 重试发生时 THEN 系统 SHALL 在日志中记录当前重试次数、最大重试次数和等待时间
6. WHEN `job-run` 成功时 THEN 系统 SHALL 不进行重试，直接继续下一个 mini-batch

### 需求 3：重试参数可配置

**用户故事：** 作为运维人员，我希望能够通过命令行参数控制重试行为，以便根据不同硬件环境灵活调整。

#### 验收标准

1. WHEN 用户未指定重试参数时 THEN 系统 SHALL 使用默认值：最大重试次数 10 次，初始退避时间 5 秒，最大退避时间 60 秒
2. IF 用户通过 `--max-retries` 参数指定了重试次数 THEN 系统 SHALL 使用用户指定的值
3. IF 用户通过 `--retry-delay` 参数指定了初始退避时间 THEN 系统 SHALL 使用用户指定的值
4. WHEN 退避时间计算超过最大退避时间 THEN 系统 SHALL 使用最大退避时间作为实际等待时间

### 需求 4：重试前的 GPU 内存清理

**用户故事：** 作为运维人员，我希望在重试前系统能主动释放 GPU 内存，以便提高重试成功的概率。

#### 验收标准

1. WHEN `job-run` 失败需要重试时 THEN 系统 SHALL 在等待退避时间期间，通过调用 Python GC 和 CUDA 缓存清理来释放 GPU 内存
2. IF 重试的 `job-run` 使用 `tolerant` 策略 THEN 系统 SHALL 在重试前让 ONNX Runtime 有机会释放之前分配失败的残留内存

**技术分析：** 在 shell 层面重试 `job-run` 时，由于每次 `job-run` 是独立的 Python 进程调用，进程退出时 ONNX Runtime 会自动释放所有 GPU 内存。因此重试天然具备"干净重启"的效果，无需额外的 GPU 内存清理逻辑。此需求在实现上实际已经满足。
