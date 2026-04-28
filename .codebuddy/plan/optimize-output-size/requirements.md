# 需求文档：优化视频换脸生成图片与视频体积

## 引言

在当前的 `video_face_swap.py` 管线中，视频帧提取硬编码使用 **PNG 无损格式**，视频合成使用 **mpeg4 编码器**，导致：
- 中间帧图片体积巨大（单张 PNG 可达数 MB）
- 合成视频体积大且压缩效率低（mpeg4 编码器老旧）
- 磁盘 I/O 成为处理速度瓶颈

本需求旨在通过**配置文件调优**和**代码层面优化**两方面，在不显著影响画面质量的前提下，大幅缩减中间帧图片和输出视频的体积，并间接加速换脸处理速度。

## 需求

### 需求 1：将中间帧格式从 PNG 改为 JPEG，降低帧图片体积

**用户故事：** 作为视频换脸使用者，我希望中间帧图片使用 JPEG 格式保存而非 PNG，以便大幅减少磁盘占用和 I/O 时间，同时画面质量基本不受影响。

#### 验收标准

1. WHEN `video_face_swap.py` 提取视频帧（普通模式） THEN 系统 SHALL 使用 JPEG 格式（`.jpg`）保存帧图片，而非 PNG
2. WHEN `video_face_swap.py` 提取关键帧（turbo 模式） THEN 系统 SHALL 使用 JPEG 格式保存关键帧
3. WHEN 帧提取使用 JPEG 格式 THEN 系统 SHALL 使用可配置的 JPEG 质量参数（默认值 95，范围 80-100）
4. IF 用户通过命令行参数 `--frame-quality` 指定了帧质量 THEN 系统 SHALL 使用用户指定的值
5. WHEN 帧图片格式从 PNG 改为 JPEG THEN 系统 SHALL 更新所有帧文件名的 glob 模式匹配（`frame_*.png` → `frame_*.jpg`）
6. WHEN 帧图片格式从 PNG 改为 JPEG THEN 系统 SHALL 更新帧排序、查找、链接等所有相关逻辑中的文件扩展名引用

### 需求 2：优化视频合成编码器与参数，降低输出视频体积

**用户故事：** 作为视频换脸使用者，我希望输出视频使用现代高效的视频编码器和合理的编码参数，以便在保持画质的同时大幅减小视频体积。

#### 验收标准

1. WHEN `reassemble_video` 合成视频 THEN 系统 SHALL 默认使用 `libx264` 编码器替代 `mpeg4`
2. WHEN 使用 `libx264` 编码器 THEN 系统 SHALL 使用 CRF 模式（默认 CRF 23，范围 18-28）控制质量
3. WHEN 使用 `libx264` 编码器 THEN 系统 SHALL 使用可配置的 preset（默认 `medium`）
4. IF 用户通过命令行参数 `--video-encoder` 指定了编码器 THEN 系统 SHALL 使用用户指定的编码器
5. IF 用户通过命令行参数 `--video-crf` 指定了 CRF 值 THEN 系统 SHALL 使用用户指定的 CRF 值
6. WHEN 使用 `libx264` 编码器 THEN 系统 SHALL 设置像素格式为 `yuv420p` 以确保兼容性
7. WHEN 系统检测到可用的 NVIDIA GPU THEN 系统 SHALL 可选使用 `h264_nvenc` 硬件编码器

### 需求 3：在 `facefusion.ini` 中提供合理的默认配置值

**用户故事：** 作为系统运维人员，我希望 `facefusion.ini` 中提供合理的默认值，以便无需每次手动指定参数即可获得体积优化的效果。

#### 验收标准

1. WHEN `facefusion.ini` 的 `[frame_extraction]` 段 THEN 系统 SHALL 包含 `temp_frame_format = jpeg` 默认值
2. WHEN `facefusion.ini` 的 `[output_creation]` 段 THEN 系统 SHALL 包含 `output_video_encoder = libx264` 默认值
3. WHEN `facefusion.ini` 的 `[output_creation]` 段 THEN 系统 SHALL 包含 `output_video_quality = 80` 默认值（映射 CRF ≈ 23）
4. WHEN `facefusion.ini` 的 `[output_creation]` 段 THEN 系统 SHALL 包含 `output_video_preset = medium` 默认值

### 需求 4：`face_swapper_pixel_boost` 参数优化指导

**用户故事：** 作为视频换脸使用者，我希望了解 `face_swapper_pixel_boost` 参数对处理速度和体积的影响，以便在质量和速度之间做出合理选择。

#### 验收标准

1. WHEN `face_swapper_pixel_boost` 设置为模型基础分辨率（如 `256x256`） THEN 系统 SHALL 以最快速度处理，但人脸区域分辨率最低
2. WHEN `face_swapper_pixel_boost` 设置为更高分辨率（如 `512x512` 或 `1024x1024`） THEN 系统 SHALL 处理时间随 pixel_boost_total² 增长（例如 1024x1024 时处理 16 个 patch）
3. WHEN 用户选择 `pixel_boost` 分辨率 THEN 系统 SHALL 在 `VIDEO_FACE_SWAP_GUIDE.md` 中提供清晰的选择建议和性能影响说明

### 需求 5：磁盘 I/O 优化间接加速换脸速度

**用户故事：** 作为视频换脸使用者，我希望通过减小帧图片体积来降低磁盘 I/O 瓶颈，以便在磁盘速度受限的环境中加速换脸处理。

#### 验收标准

1. WHEN 帧格式从 PNG 改为 JPEG（质量 95） THEN 单帧体积 SHALL 减少约 70-90%（典型 1080p 帧从 ~5MB 降至 ~0.5MB）
2. WHEN 帧体积减少 THEN 磁盘读写时间 SHALL 相应减少，在 HDD 或网络存储环境中效果尤为明显
3. WHEN 视频编码从 mpeg4 改为 libx264 THEN 输出视频体积 SHALL 减少约 50-70%
4. WHEN 使用 `h264_nvenc` 硬件编码器 THEN 编码速度 SHALL 显著快于 CPU 编码的 libx264
