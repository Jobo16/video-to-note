# VideoToNo · 链接转字幕

这个项目现在只做一件事：把视频链接转为本地字幕。服务器 url2audio.jobo.asia 负责解析链接、下载并提取音频；Mac 下载音频后用本地模型转录。服务器失败时，Mac 自动用原有下载器重试。没有大模型笔记、API Key 配置或视频截图流程。

## 在 Mac 上使用

需要 macOS、[uv](https://docs.astral.sh/uv/)、ffmpeg 和 Chrome/Chromium。启动：

~~~bash
./start-macos.sh
~~~

打开 http://127.0.0.1:8000/ ，粘贴抖音、B 站或 YouTube 分享链接，选中文或英语，开始转录。中文使用 Paraformer，英语使用 faster-whisper small，均在本机 CPU 上运行。首次使用会下载模型。

任务完成后，workspace/transcripts/ 下会保存同名的 .md 转录稿和 .srt 字幕。每个任务自己的 workspace/编号/ 目录保留音频、逐段 JSON 和任务记录。SRT 的时间轴来自语音模型，文字可能需要人工校对。

远程服务配置写在仓库根目录的 .env（参考 .env.example）：

~~~text
URL2AUDIO_API_URL=https://url2audio.jobo.asia
URL2AUDIO_TOKEN=<私有令牌>
~~~

两项未配置时直接使用本机下载。远程服务取不到媒体时自动回退本机，不上传本机模型或转录文本。

## 接口

本机 POST /api/transcribe 接受 {"video_url":"https://...","language":"zh"}，英文用 en。返回 task_id 后轮询 GET /api/task/{task_id}；完成后可下载 /api/task/{task_id}/file/srt 或 /api/task/{task_id}/file/md。

服务器只提供带令牌的 POST /jobs、GET /jobs/{id}、GET /jobs/{id}/audio；GET /health 公开用于健康检查。下载任务单 worker 顺序执行，状态保存在 SQLite，完成的音频在服务器留存 24 小时。服务器只接收常见视频平台的 HTTPS 链接，不接收本机文件或 Cookie。

## 部署服务器

服务代码和 Mac 应用在同一仓库。服务器检出放在 /opt/apps/url2audio/repo，运行数据和私有 .env 放在 /opt/stacks/url2audio/。该目录中的 data/ 需可由容器用户 UID 10001 写入。提交并推送代码后，在服务器检出中运行 ./url2audio/deploy.sh；脚本构建 Docker 镜像、启动服务并检查本机健康接口。Caddy 将 url2audio.jobo.asia 转发到 127.0.0.1:14175。

## 本次验证用链接

- 中文：[人类当初为什么会关注水稻](https://v.douyin.com/A4B_GZ7F5Ww/)
- 英语：[用英语谈论健康](https://v.douyin.com/OxW1b-RKAXk/)

这两条链接用于验证服务器取音频、Mac 本地转录和 SRT/Markdown 落盘。平台链接可能过期或触发验证，失败时以任务错误和本机回退结果为准。
