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

## 日常使用

```bash
./run.sh start     # 后台启动；./run.sh 默认行为相同
./run.sh status    # 查看状态
```

1. 在本机页面选择录音，点击本地转写；使用云端按钮前，配置自己的百炼 API Key。
2. 通过波形和文本回听，修正文字、说话人名称或标签。
3. 重要录音可收藏，程序会尝试归档本地副本。
4. 按需导出 TXT，不要把真实转写内容提交到公开仓库。

注意：现有 run.sh stop 按进程名匹配，可能停止其他同名 uvicorn 服务。运行多个项目时，优先结束你明确启动的进程。

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
bash -n run.sh app/build.sh
```

测试使用临时目录和虚构数据，不读取真实录音，不调用云端 API，不产生转写费用。覆盖本地页面、Origin/Host 校验、导入路径约束、密钥不回显等发布安全行为。

- 识别速度、准确率和声纹效果取决于模型、音质和设备。
- 本地声纹二分类不是完整多说话人分离；云端也可能把同一人拆成多个说话人。
- 重新转写会覆盖人工修正，请先导出需要保留的内容。
- 余额页可用时长按参考单价估算，不是实时计价承诺。
- 公开版本未使用作者的真实 API Key 进行云端调用验证。
- 此次发布是独立的清理后源码快照，不包含原工作目录和历史提交。

## 许可

仓库暂未附加开源许可证。公开可见不等于授予任意复制、修改或分发的许可，如需相关授权请先联系作者。
