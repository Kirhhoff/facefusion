# 实施计划：优化视频换脸生成图片与视频体积

- [ ] 1. 将帧提取格式从 PNG 改为 JPEG（普通模式 + turbo 模式）
   - 修改 `extract_frames` 函数中两处 ffmpeg 命令的输出路径：`frame_%08d.png` → `frame_%08d.jpg`
   - 将 ffmpeg 参数从 `-qmin 1 -q:v 1`（PNG 无损）改为 JPEG 质量控制：`-qmin 1 -q:v 2`（高质量 JPEG）
   - 添加 `--frame-quality` 命令行参数（默认 95，范围 80-100），映射到 ffmpeg 的 `-q:v` 参数
   - 将质量参数传递到 `extract_frames` 函数签名中
   - _需求：1.1、1.2、1.3、1.4_

- [ ] 2. 更新所有帧文件 glob 匹配与扩展名引用（`frame_*.png` → `frame_*.jpg`）
   - 将 `video_face_swap.py` 中全部 29 处 `frame_*.png` glob 模式改为 `frame_*.jpg`
   - 将 `reassemble_video` 中正则 `^frame_\d{8}\.png$` 和 `^frame_\d{8}-worker-\d+-mb-\d+-\d+\.png$` 更新为 `.jpg`
   - 将帧链接命名 `frame_{idx:08d}.png` 改为 `frame_{idx:08d}.jpg`
   - 将 ffmpeg 输入模板 `frame_%08d.png` 改为 `frame_%08d.jpg`
   - 更新文档字符串中的 `.png` 引用
   - _需求：1.5、1.6_

- [ ] 3. 优化 `reassemble_video` 视频编码：默认使用 libx264 + CRF 模式
   - 将默认编码器从 `mpeg4` 改为 `libx264`
   - 将 `-qscale:v 2` 替换为 `-crf 23`（默认值）
   - 添加 `-preset medium` 参数
   - 保留 `-pix_fmt yuv420p`
   - _需求：2.1、2.2、2.3、2.6_

- [ ] 4. 添加视频编码相关的命令行参数
   - 添加 `--video-encoder` 参数（默认 `libx264`，可选 `mpeg4`、`h264_nvenc`）
   - 添加 `--video-crf` 参数（默认 23，范围 18-28）
   - 添加 `--video-preset` 参数（默认 `medium`）
   - 将这些参数传递到 `reassemble_video` 函数签名中
   - 当选择 `h264_nvenc` 时使用 `-preset p4 -cq 23` 替代 CRF 模式
   - _需求：2.4、2.5、2.7_

- [ ] 5. 更新 `facefusion.ini` 默认配置值
   - 在 `[frame_extraction]` 段设置 `temp_frame_format = jpeg`
   - 在 `[output_creation]` 段设置 `output_video_encoder = libx264`
   - 在 `[output_creation]` 段设置 `output_video_quality = 80`
   - 在 `[output_creation]` 段设置 `output_video_preset = medium`
   - _需求：3.1、3.2、3.3、3.4_

- [ ] 6. 更新 `VIDEO_FACE_SWAP_GUIDE.md` 文档，添加 pixel_boost 参数说明和体积优化指南
   - 添加 `face_swapper_pixel_boost` 参数的性能影响说明（256x256 vs 512x512 vs 1024x1024 的处理时间对比）
   - 添加新增命令行参数（`--frame-quality`、`--video-encoder`、`--video-crf`、`--video-preset`）的用法说明
   - 添加体积优化建议（JPEG vs PNG 体积对比、编码器选择建议）
   - _需求：4.3、5.1、5.3_
