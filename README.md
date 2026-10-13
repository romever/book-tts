# Book TTS

面向家庭使用的有声书制作准备台：上传 EPUB/TXT 后先在本地建立稳定的章节和分段索引，再由用户选择一个执行窗口，按范围完成读音检查、TTS 和章节音频生成。TTS 可按书籍选择 MiMo 或本地 mlx-audio；MiMo 使用克隆音色时，被内容审核拒绝的分段会自动改由本地模型用同一音色生成。读音检查默认通过 OpenRouter 调用 DeepSeek。

## 本地启动

需要 Python 3.12、`ffmpeg` 和 `ffprobe`。

```bash
cp .env.example .env
# 编辑 .env，至少填写 APP_PASSWORD 和 OpenRouter 的 LLM_API_KEY
# 只有选择 MiMo 时才需要 MIMO_API_KEY
UV_CACHE_DIR=.cache/uv uv venv .venv
UV_CACHE_DIR=.cache/uv uv pip install --python .venv/bin/python -r requirements.txt
```

启动 Web 服务：

```bash
APP_ROLE=web .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开一个终端启动 Worker：

```bash
APP_ROLE=worker .venv/bin/python -m app.worker
```

访问 `http://127.0.0.1:8000`。会话密钥会自动保存在 `DATA_DIR/.session-secret`，不需要额外配置 `APP_SESSION_SECRET`。

MiMo 正式生成、试听和音色设计按模型共享请求频率与冷却时间，Web 和 Worker 通过同一份数据库协调。`MIMO_TTS_RPM` 默认 60，允许设置为 1～100；同账号其他应用的请求也占官方配额，需要预留余量。收到 429 后按约 10、20、40、60 秒逐步延长等待，并加入少量随机延迟；响应带有效 `Retry-After` 时至少等待其指定时间。`MIMO_TTS_RATE_LIMIT_WAIT_SECONDS` 默认 180 秒，时间窗口耗尽后明确报错，不再由外层重试。单次 HTTP 请求超时仍为 300 秒，其他可重试错误继续由 `TTS_MAX_ATTEMPTS` 控制。

MiMo 每次请求的时间、耗时、状态码及限流相关响应头保存在音频旁的 `*.tts-requests.jsonl`，运行日志中也会显示冷却和 Token 用量。诊断记录不包含 API Key、朗读正文或参考音频。手动验证时，重启 Web 和 Worker，在页面试听 MiMo 或生成一个小范围；遇到 429 后查看日志，确认等待后继续生成。若单并发、低频率下仍持续超限，可把发生时间和服务商请求 ID 提交给 MiMo 支持。

## 启动本地 mlx-audio

本地 TTS 运行在 macOS 宿主机，不安装到 Book TTS 的 Docker 容器。当前已安装的服务可以手工启动为：

```bash
cd /Users/liguoyang/Documents/code/tools/qwen3-tts
.venv/bin/pip install 'mlx-audio[server]==0.5.5'
.venv/bin/python -c "import sys, mlx.core as mx; mx.set_memory_limit(10*1024**3); mx.set_cache_limit(1*1024**3); from mlx_audio.server import main; sys.argv=['mlx_audio.server','--host','127.0.0.1','--port','8100','--tts-max-batch-size','1']; sys.exit(main())"
```

服务启动后可用 `curl http://127.0.0.1:8100/` 检查连接。本地模型使用 `mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit`，只支持声音克隆，没有预置音色；首次试听会下载并加载模型，耗时会明显长于后续请求。

可以在进入 Book TTS 前直接验证模型和克隆效果（`ref_audio` 是 mlx-audio 所在机器上的文件路径，`ref_text` 是这段音频里说的原话）：

```bash
curl -X POST http://127.0.0.1:8100/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"mlx-community/Qwen3-TTS-12Hz-1.7B-Base-8bit","input":"你好，这是本地有声书模型测试。","ref_audio":"/path/to/reference.wav","ref_text":"参考音频里说的原话","lang_code":"chinese","response_format":"wav","stream":false,"max_tokens":4096}' \
  --output /tmp/book-tts-mlx-test.wav
ffprobe -v error -show_entries stream=codec_name,sample_rate,channels,duration \
  -of json /tmp/book-tts-mlx-test.wav
```

Book TTS 把参考音频保存在 `DATA_DIR/voices/<音色ID>/reference.wav`，请求时直接把这个绝对路径发给 mlx-audio，所以两者需要能访问同一个路径：本机直接运行 Book TTS 时天然满足；Book TTS 跑在 Docker 或 NAS 上时，本地克隆音色不可用。

如果 Book TTS 通过 Docker Desktop 运行，需要将 mlx-audio 改为 `--host 0.0.0.0`，并将 `.env` 中的 `MLX_AUDIO_BASE_URL` 设为 `http://host.docker.internal:8100`。该方式会将 8100 端口暴露给局域网，只应在可信网络或已配置防火墙的环境中使用。

## 使用方式

1. 首页只负责上传和查看书库。
2. 上传时可以试听并选择 MiMo 或 mlx-audio 及其音色。克隆音色是两者共用的音色库，在音色列表里「新建克隆音色」有两种方式：
   - 用 MiMo 设计：写一段声音描述，生成样音，满意后保存；样音文本会自动作为参考原话。
   - 上传录音：一段 3～20 秒、单人无背景音的清晰人声，并逐字填写其中说的原话。

   克隆音色未被书籍使用时可以删除。推荐用 MiMo + 克隆音色：MiMo 负责主要生成，遇到内容审核拒绝的分段由本地模型接管，需要此时 mlx-audio 已启动。两个模型复刻的是同一段参考音频，音色接近但不会完全一致。
3. 上传后自动进入“制作准备台”，Worker 先完成一次本地解析。
4. 选择按章节、按字数或高级范围；页面会先展示实际字数、章节数、预检请求数、TTS 分段数和预计时长。
5. 选择“只做读音检查”“检查 + 生成语音”“检查 + 样例音频”或章节成品阶段。
6. 每个执行窗口独立记录。失败会停止当前窗口，但已经成功的批次、分段和章节文件会保留，可重试或继续下一窗口。

在「章节内容与成品」中勾选需要的章节，点击「下载所选章节（ZIP）」即可批量下载；「全选成品」可选中本书全部可下载章节。ZIP 内是按章节编号排列的独立 M4B 文件，尚未生成的章节不能勾选。

章节详情里可以对整章「重新检查本章」（只更新朗读稿和发音提示）、「重新生成本章语音」（替换全部分段语音并自动重新合并章节文件）或「重新合并本章」（用现有分段语音重新生成章节文件），也可以对单个分段重新生成语音。

首页的「TTS 配置」可以保存新书默认供应商和音色，也可以试听、创建或管理克隆音色；上传表单会使用保存的默认配音，已有书籍继续使用各自的配音设置。

制作台可以随时更换 TTS 供应商或音色（执行窗口运行中除外）。已生成的分段音频和章节成品会保留，新设置只用于之后生成的分段；分段详情里会标出每段实际使用的音色，想统一声音时可以对个别分段「重新生成」。

发音预检严格校验 LLM 返回的 JSON、分段覆盖，以及每段朗读稿的内容确实来自本段原文（防止模型把分段错位或合并）。原文与朗读稿分开保存；朗读稿以听感为准，会删掉注释、出处、页码等不适合朗读的内容，把数字和符号改成读法，并按意思分成自然段，TTS 使用朗读稿而不是原文。每本书可以选择朗读稿风格：「贴近原文」只做上述清理；「讲述改写」会在清理后逐段改写成主讲人的讲述口吻（短句、口语衔接、适当设问），再在最终讲述稿上标注发音，改写幅度较大但不改变事实和原意。风格可在上传时选择，之后在制作台修改，已检查的章节需要「重新检查本章」才会换成新风格；整段都是注释的分段朗读稿为空，生成为短暂静音。生成语音时按自然段逐段合成，段与段之间、分段与分段之间留出停顿换气；MiMo 还会收到主讲人风格的朗读指令，本地克隆模型不接受指令，语气取决于参考音频。模型偶发输出退化（思考死循环、输出截断、分段错位等）时，同一批会自动重新请求，最多 `LLM_MAX_ATTEMPTS` 次（默认 3），仍失败才报错。每次请求的原始响应都会保存到该书的 `executions/<execution_id>/llm/`（第二次起文件名带 `.attempt-N`），页面会显示朗读稿和原文对照。

LLM 默认配置为 OpenRouter 的 `deepseek/deepseek-v4.1-flash`。`LLM_API_KEY` 必须填写 OpenRouter API Key；`OPENROUTER_ALLOW_FALLBACKS=true` 时，同一模型的服务商出现异常会由 OpenRouter 切换到其他健康服务商。同一模型在不同服务商的量化精度和稳定性差别很大，可以用 `OPENROUTER_PROVIDER_ORDER` 指定优先服务商、用 `OPENROUTER_QUANTIZATIONS` 排除 fp4/nvfp4 等低精度版本；需要严格只用指定服务商时，把 `OPENROUTER_ALLOW_FALLBACKS` 设为 `false`。`LLM_REASONING_EFFORT` 留空时使用模型默认策略；需要强制推理的模型可填写 `low`、`medium`、`high` 或 `max`。

## 在 Codex 中整理朗读稿

除了由 Worker 调用 OpenRouter 整理朗读稿，也可以在 Codex 中用 ChatGPT 订阅完成整本书的整理，再导入 Book TTS。项目自带 Codex skill `book-speech-rewrite`（`.agents/skills/book-speech-rewrite/`，已链接到 `~/.agents/skills/`），在 Codex 中说“用 book-speech-rewrite 按讲述改写整理某本书的朗读稿”即可。

skill 使用的命令也可以手动执行：

```bash
.venv/bin/python -m app.speech_io list
.venv/bin/python -m app.speech_io export --book <书籍ID> --style narrated   # 或 faithful
.venv/bin/python -m app.speech_io check --dir <导出目录>
.venv/bin/python -m app.speech_io import --dir <导出目录>
```

导出目录默认在 `DATA_DIR/speech-exports/<书籍ID>-<风格>/`，包含每章一个 JSON（在 `speech_text` 中填写整理结果）和与内置流程同一份的规则文件 `rules.md`。导入时逐章校验分段是否完整、原文是否变化、朗读稿是否与本段原文对应（防止错位或合并），只写入通过校验的章节，并把这些章节的读音检查标记为完成；已生成的语音不会自动替换，需要时对章节「重新生成本章语音」。

## 数据目录

默认情况下，数据库、原始书稿、规范化章节文本、分段 WAV、预检响应和日志都在项目的 `data/` 目录；最终章节 M4B 写入 `AUDIOBOOKS_DIR`。

最终书库结构为：

```text
/audiobooks/
  作者/
    书名/
      cover.jpg
      0001-章节一.m4b
      0002-章节二.m4b
```

每个章节文件是一个独立音频 track，适合 Audiobookshelf 的书籍目录结构。

## Docker / 群晖

```bash
cp .env.example .env
# 编辑 .env 和 docker-compose.yml 中的卷映射
docker compose up -d --build
```

Compose 默认把项目的 `./data` 挂载到容器 `/data`，把项目的 `./audiobooks` 挂载到容器 `/audiobooks`。如需直接写入现有 Audiobookshelf 书库，只修改 `docker-compose.yml` 中 `./audiobooks` 左侧的宿主机路径。

## 处理边界

- 支持 EPUB 和 TXT；TXT 默认作为一章，也可在上传时填写章节标题正则。
- 本地文本清理和分段是确定性的；LLM 生成朗读稿时可以删除注释等非正文内容，但原文始终保留，且朗读稿不得编造事实、删减正文叙述或改变原意。
- `uncertain` 发音提示会保存，但默认不传给 TTS。
- 发音提示只对 MiMo 生效（包括 MiMo 克隆音色）；本地克隆模式不接受朗读指令，会只按朗读稿生成。
- mlx-audio 的长朗读段会在请求前按句子确定性切分，生成后再合并为原分段对应的单个 WAV。
- 不包含 PDF、目录监控、播放器、多用户、音频可疑片段复核。除内容审核拒绝外，其他 MiMo 错误不会切换到本地模型。
