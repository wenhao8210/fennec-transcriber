# Wennec 转写助手

面向 macOS 的个人录音整理工具：读取 Fennec 的本地录音，完成转写、检索、回听和说话人标注。使用 Python / FastAPI 后端、无需打包的单页界面，以及可选的 Swift 原生窗口。

Wennec 是独立的个人项目，不是 Fennec 或阿里云的官方产品。这里的 Fennec 指作为录音来源的应用。

产品原名为 Fennec 转写助手。为兼容现有链接和配置，仓库地址、FENNEC_* 环境变量、数据目录及旧 fennec:// 链接暂时保留；界面与新构建的 App 使用 Wennec 名称，并支持 wennec:// 链接。

## 功能

- 本地 faster-whisper 转写，默认 small 模型，无需 API Key。
- 可选阿里云百炼 Fun-ASR，支持云端说话人分离和热词。
- 波形定位、播放联动、全文搜索、逐句修正和说话人合并。
- 日期筛选、标签、收藏、TXT 导出和批量操作。
- 本地本人/他人声纹判断，以及云端分离后的已注册声纹匹配。
- 收藏时归档音频副本；按时间标记匹配附近录音并进行本地自动转写。

## 隐私和使用边界

**仓库只包含源码，不包含作者的 API Key、录音、声纹、转写记录、账单缓存、模型、虚拟环境或编译后的 App。**

- 本地转写在电脑上执行；首次准备模型需要联网下载模型文件。
- 云端转写会上传所选音频，云端热词同步会上传词表，可能产生费用。请事先取得所需同意，并核对服务商的数据政策和价格。
- API Key 可通过环境变量提供，也可在设置界面输入。界面保存的凭据以明文存放于本机数据目录的 settings.json，不是系统钥匙串。不要分享此文件或整个 data 目录。
- 余额查询是独立可选功能，需要自己的阿里云 RAM AccessKey。建议只授予只读费用权限，不使用主账号密钥。
- 服务仅供个人本机使用，应只监听 127.0.0.1。没有账户登录、用户隔离或完整公网部署防护，不要将端口暴露到局域网或公网。
- 重命名会修改源录音文件名；删除会把源录音移入 macOS 废纸篓。请先用复制的录音测试，并保留备份。收藏归档不是完整备份方案。

## 视图与原生菜单（1.2.1）

窗口分为「监听」与「录音库」：监听页集中显示采集控制、总时间轴和框选切片；录音库显示历史列表、单条回听和转写正文。切换不会停止后台监听，也不会重建当前播放器；录音库顶栏保留简洁的采集状态。

macOS 顶部「视图」菜单切换监听（`⌘1`）与录音库（`⌘2`），「监听」菜单提供开始/停止、立即录音和监听选项，「转写」菜单提供当前录音转写、搜索与设置。原有 `⌘L`、`⇧⌘R`、`⌘T` 保留；菜单栏右侧波形图标仍可快速控制监听。

## 持续监听与时间轴（1.2）

点击桌面 App「监听」页或菜单中的「开始监听」后，麦克风持续在后台工作：等待人声 → 自动保存一段 → 安静达到设定时长后封口 → 继续等待下一次说话。关闭窗口不会停止监听；菜单栏橙色表示等待声音，红色表示正在录音。`⌘L` 开始/停止监听，`⇧⌘R` 立即录音/结束当前段。

「立即录音」可强制保存一段，不受人声和静音检测限制；「结束这一段」只结束当前文件，监听继续。停止监听或退出 App 会先保存当前片段。重新打开 App 不会自行启用麦克风。

- 静音时间默认 15 秒，可设 5/15/30/60/120 秒或关闭。已有用户设置会保留。连续讲话最长每 10 分钟切一段。
- 人声检测默认开启，使用 macOS 本机 SoundAnalysis；不上传音频、不调用转写。两秒预卷补足检测延迟前的声音，短片段不会自动丢弃。
- 灵敏度按环境底噪的倍数设置，越小越灵敏。关闭人声检测时按音量触发；分类器不可用或跟不上时，会明确显示退化为音量检测。它不判断说话人身份，也不能保证每次声音识别都准确。
- 自录文件位于 `~/Library/Application Support/local.fennec-transcriber/Recordings/`；完成后自动进入原列表。旧 Fennec 的 Sessions、recordings 和已保全归档继续可读，不自动清理历史录音。
- 录音沿用设备输入格式，完成后转为 16 kHz 单声道 AAC，目标码率 24 kbps。后台编码和入库独立运行；自动转写默认关闭，开启后仅用本地引擎。

时间轴默认最近 3 小时，可切换 1/3/6/12/24 小时。拖动框选或填写起止时间，先「试听选区」，满意后「生成片段」。蓝色是已保存录音，橙色虚线是尚未保存的当前片段；涉及当前片段时，先点「保存当前段并生成」，等待真实入库后再切片。选区最长 3 小时，中间没有录音的空隙会保留为静音；空缺首尾不能凭空补成录音。试听缓存有数量和容量上限，正式生成的片段不会按这个规则删除。

后端离线时录音留在本地并重试入库；退出或异常中断遗留的 CAF 与恢复记录会在下次启动继续处理。睡眠期间无法采集，唤醒后仅恢复本次已明确开启的监听意图。当前只采集麦克风，耳机内远端会议声音、系统音频和实时 Voice Guard 尚未接入。

构建：`app/build.sh`。可用 `CODE_SIGN_IDENTITY` 指定开发签名；没有开发签名时使用临时签名。旧 App 备份保留在 `90_过程产物/应用构建备份/`。退出只停止自启且空闲的后端；已有转写或片段生成任务可继续运行。

隔离验收使用 `FENNEC_RECORDINGS_DIR`、`FENNEC_APP_DATA_DIR`、`FENNEC_DATA_DIR`、`FENNEC_PORT`、`FENNEC_LOG_PATH` 和 `FENNEC_DEFAULTS_SUITE`。`FENNEC_TEST_NO_ACTIVATE=1` 可让非正式端口的验收实例不抢前台。不要在生产数据库上运行测试。

```bash
python3 app/Tests/test_recording_controller.py
venv/bin/python -W ignore::ResourceWarning -m unittest discover -s backend -p 'test_recording*.py' -v
python3 app/Tests/test_app_lifecycle.py --app "$PWD/Wennec.app"
```

声音分析接口依据：[Apple SoundAnalysis 流分析文档](https://developer.apple.com/documentation/soundanalysis/classifying-sounds-in-an-audio-stream)。

## 环境要求

- macOS。原生外壳与废纸篓行为按 macOS 实现，其他平台未验证。
- Python 3.13，本次验证使用 3.13.12。
- FFmpeg / ffprobe。
- 构建原生外壳时需要 Xcode Command Line Tools / Swift。
- 足够存放模型、录音缓存和归档的磁盘空间。

## 安装

```bash
git clone https://github.com/wenhao8210/fennec-transcriber.git
cd fennec-transcriber

brew install ffmpeg
python3.13 -m venv venv
./venv/bin/python -m pip install --upgrade pip
./venv/bin/python -m pip install -r requirements.txt
```

依赖版本来自可运行环境，其他 Python 版本和芯片架构需要自行确认兼容性。

PyAV 显式固定为 `av==16.1.0`。当前 `faster-whisper==1.2.1` 会传入 PyAV 19 已移除的 `metadata_errors` 参数；不要单独升级 `av` 到 19。若旧环境转写时报 `open() got an unexpected keyword argument 'metadata_errors'`，重新运行 `./venv/bin/python -m pip install -r requirements.txt`，然后重启服务。

### 下载本地识别模型

需要 faster-whisper 对应的 CTranslate2 模型，不是原版 Whisper 的 .pt 文件：

```bash
./venv/bin/python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Systran/faster-whisper-small', local_dir='models/whisper-small')"
```

改用 medium 时，同时修改上面的仓库名、目录后缀以及 FENNEC_WHISPER_MODEL。模型受上游条款约束，本仓库不再分发模型。

声纹识别首次需要时，会从 [上游模型仓库](https://huggingface.co/csukuangfj/speaker-embedding-models) 下载 3D-Speaker ERes2Net ONNX 文件到本地缓存；不使用声纹时不需要预先下载。

### 配置录音目录

默认读取以下目录：

```text
~/Library/Application Support/com.kkapp.fennec/datas/
├── Sessions/
├── recordings/
└── voice_profile_sample.wav  # 可选的本人声纹参考
```

没有安装 Fennec，也可以自建这套目录结构并放入自己的录音，再设置 FENNEC_DATA_DIR。扫描支持 .m4a、.mp3、.aac、.ogg、.flac，暂不扫描 .wav；声纹上传另行支持 WAV。

```bash
cp .env.example .env
# 按需编辑 .env，路径有空格时保留引号。
./run.sh start
open http://127.0.0.1:8778
```

run.sh 自动加载项目根目录的 .env，内容使用 shell 语法。直接启动 uvicorn 或原生 App 时需自行传入环境变量。环境变量优先于界面保存的云端设置。

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| FENNEC_DATA_DIR | 上述 Fennec datas 目录 | 录音源 |
| FENNEC_STORAGE_DIR | 项目内 data/ | 数据库、设置、缓存、声纹与归档 |
| FENNEC_WHISPER_MODEL | small | 本地模型及目录后缀 |
| FENNEC_WHISPER_LANGUAGE | zh | 识别语言 |
| FENNEC_SPEAKER_THRESHOLD | 0.55 | 本人声纹相似度阈值 |
| DASHSCOPE_API_KEY | 空 | 可选云端转写凭据 |
| DASHSCOPE_BASE_URL | 阿里云默认 API 地址 | 可选可信服务地址 |
| DASHSCOPE_WORKSPACE | 空 | 可选业务空间 |
| ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET | 空 | 可选余额查询凭据 |

只做本地转写时，把云端凭据留空即可。自定义 API 地址会影响凭据和请求发送的目的地，不要照抄陌生人提供的地址。

### 可选 ASR Studio 转写

天润引擎需要通过 `FENNEC_TINET_ASR_SERVER`、`ASR_SERVER` 或本机 ASR Studio 配置指定自己的服务地址，公开版不内置作者的服务地址。选择此引擎会上传音频；默认识别引擎为 `doubao`，可用 `FENNEC_TINET_ASR_ENGINE` 调整。

## 日常使用

```bash
./run.sh           # 打开原生窗口，由 App 托管服务
./run.sh start     # 仅启动后端
./run.sh status    # 查看状态
```

1. 在本机页面选择录音，点击本地转写；使用云端按钮前，配置自己的百炼 API Key。
2. 通过波形和文本回听，修正文字、说话人名称或标签。
3. 重要录音可收藏，程序会尝试归档本地副本。
4. 按需导出 TXT，不要把真实转写内容提交到公开仓库。

`./run.sh stop` 只处理已核验路径的本项目进程，先封存当前录音；后端有转写或片段任务时保留服务。

### 可选原生窗口

```bash
bash app/build.sh
./run.sh app
```

构建出的 Wennec.app 应保留在项目根目录，紧邻 backend/ 和 venv/。它不是独立安装包，也未做 Apple 公证，不要直接拷贝到 Applications 后期待它独立运行。需要时可通过环境变量 FENNEC_HOME 指定项目根目录。

### 当天录音批处理

```bash
./venv/bin/python backend/fennec_transcribe_today.py
# 以下会上传当天选中的录音，并可能产生费用：
./venv/bin/python backend/fennec_transcribe_today.py --engine aliyun
```

默认本地引擎。脚本需要本机服务已启动，按 rec_YYYY-MM-DD_ 文件名识别当天录音。

## 目录

```text
app/                    Swift 原生外壳与构建脚本
backend/
  main.py               API、转写队列、归档与时间标记
  asr.py                本地识别
  aliyun_asr.py          可选云端识别
  speaker.py            声纹提取和匹配
  store.py              SQLite 存储
  hotwords.py           本地热词和可选云同步
  audio_quality.py      人声占比分析
  billing.py            可选余额查询
  config.py             路径和环境配置
frontend/index.html     单页界面
tests/                  发布安全回归测试
.env.example            无凭据的配置示例
```

data/、models/、venv/、.env 与编译产物已加入 .gitignore。复制或压缩整个工作目录不会自动遵守 .gitignore，分享前仍需检查内容。

## 验证与限制

```bash
./venv/bin/python -m pip install -r requirements-dev.txt
./venv/bin/python -m unittest discover -s tests -v
./venv/bin/python -m pip check
bash -n run.sh app/build.sh
```

测试使用临时目录和虚构数据，不读取真实录音，不调用云端 API，不产生转写费用。覆盖本地页面、Origin/Host 校验、导入路径约束、密钥不回显等发布安全行为；还会生成有效的单声道 / 双声道 WAV，经 faster-whisper 和 PyAV 实际解码、重采样，防止依赖兼容性回归。这些解码测试不需要下载识别模型，也不替代准备模型后的完整转写验收。

- 识别速度、准确率和声纹效果取决于模型、音质和设备。
- 本地声纹二分类不是完整多说话人分离；云端也可能把同一人拆成多个说话人。
- 重新转写会覆盖人工修正，请先导出需要保留的内容。
- 余额页可用时长按参考单价估算，不是实时计价承诺。
- 公开版本未使用作者的真实 API Key 进行云端调用验证。
- 此次发布是独立的清理后源码快照，不包含原工作目录和历史提交。

## 许可

仓库暂未附加开源许可证。公开可见不等于授予任意复制、修改或分发的许可，如需相关授权请先联系作者。
