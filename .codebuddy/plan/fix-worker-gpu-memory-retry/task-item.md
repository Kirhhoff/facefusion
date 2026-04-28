# 实施计划

- [ ] 1. 添加重试相关 CLI 参数
   - 在 `video_face_swap.py` 的 `main()` 函数 argparse 中添加 `--max-retries`（默认10）、`--retry-delay`（默认5）、`--max-retry-delay`（默认60）三个参数
   - 将这些参数传递到 `launch_workers()` 和 `process_keyframes()` 函数中
   - _需求：3.1、3.2、3.3、3.4_

- [ ] 2. 默认使用 tolerant 视频内存策略
   - 在 `launch_workers()` 和 `process_keyframes()` 生成的 worker shell 命令中，为 `job-run` 添加 `--video-memory-strategy` 参数
   - 先从 `_read_config_step_args()` 返回的结果中检查用户是否显式配置了 `video_memory_strategy`，如果未配置则默认使用 `tolerant`
   - 在 main() 的启动信息打印中显示当前使用的 video_memory_strategy
   - _需求：1.1、1.2_

- [ ] 3. 为 worker shell 脚本添加 mini-batch 重试循环
   - 在 `launch_workers()` 生成的 worker shell 脚本中，将 `job-run` 失败后的 `exit` 逻辑替换为重试循环
   - 实现指数退避：`delay = min(retry_delay * 2^attempt, max_retry_delay)`
   - 每次重试前打印日志：当前重试次数、最大重试次数、等待时间
   - 超过最大重试次数后才退出 worker
   - _需求：2.1、2.2、2.3、2.4、2.5、2.6_

- [ ] 4. 为 keyframe worker 脚本添加同样的重试循环
   - 在 `process_keyframes()` 生成的 keyframe worker shell 脚本中，应用与任务 3 相同的重试循环逻辑
   - _需求：2.1、2.2、2.3、2.4、2.5、2.6_

- [ ] 5. 移除 verbose traceback 诊断脚本，简化失败处理
   - 删除当前 `job-run` 失败后生成的 `verbose_traceback_*.py` 诊断脚本（该脚本在重试场景下不再必要，日志信息会过于冗长）
   - 保留 health check 脚本，但同样加入重试逻辑
   - _需求：2.1、4.1、4.2_
