# Walkman Music Manager

一套面向 Sony Walkman（可移动存储卡）的音乐自动下载与整理工作流：支持从**网易云音乐**和**哔哩哔哩**自动下载歌曲与翻唱，自动完成**歌词处理**（修复为 Walkman 兼容的 LRC、可选日语翻译）和**封面嵌入**（网易云专辑封面 / B 站视频封面），并按日期、歌手或专辑归档到音乐库。

**An automated music download & library workflow for Sony Walkman storage cards**: fetch tracks and covers from NetEase Cloud Music and Bilibili, repair lyrics into Walkman-compatible LRC, embed album / video covers, and archive your library by date, artist, or album.

所有写入类操作默认提供预览（`--dry-run`）与备份，避免误操作。

## 功能特性

### 歌词修复（`walkman_lrc_repair.py` / `walkman.py repair`）

- 将 UTF-8 或 GB18030 文本统一转换为 UTF-8；
- 删除网易云歌词文件中混入的 JSON 元数据；
- 删除 Walkman 不支持的普通元数据行；
- 将时间戳统一为 `[分:秒.百分之一秒]` 格式；
- 保留原目录结构，并在修改前建立带时间戳的隐藏备份目录；
- 采用原子替换，减少写入中断造成文件损坏的风险；
- 默认跳过 macOS 产生的 `._*.lrc` AppleDouble sidecar 文件。

### 网易云音乐下载（`download_music.py`、`search_music.py`）

- 集成 [neteasecli](https://github.com/wangwalk/neteasecli) 搜索与下载，支持歌曲 ID 清单批量下载；
- 自动嵌入专辑封面（FLAC picture / MP3 APIC）；
- 歌词三种模式：原文 / 网易云官方中文翻译 / 双语（无翻译时自动回退）；
- 可选的 GPT 日语歌词翻译：只发送含日文假名的带时间戳歌词行，时间戳与其它行在本地保留，返回校验失败时不写入；
- 默认按艺术家建子目录、已有文件自动跳过，`--dry-run` 可先预览。

### `.ncm` 转换（`process_music.py`）

- 批量调用 [ncmdump](https://github.com/taurusxin/ncmdump) 转换 `.ncm` 文件；
- 仅对真实 `.ncm` 调用转换器，保留艺术家目录结构，输出时自动嵌入封面并修复歌词。

### 哔哩哔哩翻唱整理（`walkman.py bilibili`）

为整理翻唱投稿设计（示例场景：虚拟歌手 / 唱见的高频翻唱更新）：

- `list`：WBI 签名 API 列出 UP 主全部投稿（含发布时间，便于筛选最新作品）；
- `fetch`：批量获取 BV 号的标题 / 时长；
- `download`：yt-dlp 批量下载 mp3（最高音质），**自动嵌入视频封面**，已有同 BV 文件自动跳过，失败清单单独输出；
- `rename`：删除 `[BV]` 后缀，按 JSON 译名表应用中文译名，重名自动加序号；
- `copy`：把整理好的音频复制到最终音乐目录（同名跳过）。

### 封面与歌词补齐（`tools/`）

- `tools/embed_cover.py`：为缺封面的音频按文件名搜索网易云封面并批量嵌入（ffprobe 检测 → 下载 → ffmpeg 内嵌）；
- `tools/fill_lyrics.py`：为缺 LRC 的音频批量补歌词（搜索 → 修复 → 按同名写入）。

### 统一 CLI（`walkman.py`）

```
walkman.py search    搜索网易云音乐
walkman.py download  下载歌曲 + Walkman 歌词（自动嵌封面）
walkman.py convert   批量转换 .ncm（自动嵌封面 + 修复歌词）
walkman.py repair    修复存储卡上的 LRC
walkman.py album     整理为单张专辑目录（只移动源目录顶层文件）
walkman.py full      一键：搜索 → 下载 → 整理为专辑目录
walkman.py bilibili  list / fetch / download / rename / copy
```

其中 `album` 带安全防呆：递归移动整个目录树（`--recursive`，属于破坏性操作）必须显式加 `--confirm`。

### 通用安全设计

- 所有子命令默认只读，`--dry-run` 只预览；下载 / 重命名 / 复制等落地命令与旧脚本行为一致；
- 目标位于 `/Volumes/<卷名>` 且该卷未挂载时拒绝写入，防止 macOS 在系统盘上静默建目录导致文件写错位置；
- 交互式登录（neteasecli / bilibili cookie）均在本机完成，本项目不读取或保存你的登录凭据；
- 不绕过任何 DRM、VIP 限制或访问控制。

## 环境要求

- Python 3.10+（仅标准库，无第三方依赖）
- [ffmpeg](https://ffmpeg.org/)（封面嵌入、音频转码）
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)（哔哩哔哩下载）
- [ncmdump](https://github.com/taurusxin/ncmdump)（`.ncm` 转换，可选）
- [neteasecli](https://github.com/wangwalk/neteasecli)（网易云下载与封面/歌词搜索，可选）：

```bash
npm install --global neteasecli
neteasecli auth login
neteasecli auth check
```

- （可选）OpenAI 兼容 API（日语歌词翻译），通过环境变量提供密钥。

## 快速开始

### 本机 Web 前端

启动本机管理界面（Python 3.10+，无额外 Python 依赖）：

```bash
python3 web_app.py
```

打开终端提示的 `http://127.0.0.1:8765`，在 **AI 与 API** 页面填写 OpenAI 兼容服务的 API 地址、模型名称和 API Key。密钥保存在用户目录下的 `~/.config/walkman-music-manager/settings.json`，仅当前用户可读；页面不会接收已保存的密钥。也可配置无 Key 的本机兼容服务。

前端可用 AI 规划网易云与 Bilibili 工作流、搜索并下载歌曲、管理翻唱投稿、转换 `.ncm`、修复歌词和归档专辑。写入或下载操作都需要先查看预览，再点击确认执行。默认音乐目录为 `~/Music/Walkman`，可在设置页修改。

把 `/Volumes/WALKMAN` 替换为你的存储卡挂载路径：

```bash
# 1. 下载（自动嵌入专辑封面 + 修复歌词）
python3 walkman.py download <歌曲ID> --output-dir /Volumes/WALKMAN/Music --by-album

# 2. 转换 .ncm（自动嵌入封面 + 修复歌词）
python3 walkman.py convert ~/Music/网易云音乐 \
  --output-dir "/Volumes/WALKMAN/Music/按日期/$(date +%Y-%m-%d)" --flat

# 3. 复制已下载的 mp3 到日期目录
cp ~/Music/网易云音乐/*.mp3 "/Volumes/WALKMAN/Music/按日期/$(date +%Y-%m-%d)/"

# 4. 修复歌词（Walkman 兼容格式）
python3 walkman.py repair "/Volumes/WALKMAN/Music/按日期/$(date +%Y-%m-%d)" --apply

# 5. 清理源目录
rm -f ~/Music/网易云音乐/*
```

## 使用详解

### 歌词修复

建议先做一次预览，确认扫描数量和待修复数量：

```bash
python3 walkman_lrc_repair.py /Volumes/WALKMAN
```

正式执行（原文件会备份到带时间戳的隐藏目录）：

```bash
python3 walkman_lrc_repair.py /Volumes/WALKMAN --apply
# 备份示例：/Volumes/WALKMAN/.walkman-lrc-backup-20260816-120000/

# 也可以手动指定备份目录：
python3 walkman_lrc_repair.py /Volumes/WALKMAN --apply --backup-root /Volumes/WALKMAN/.my-lrc-backup
```

脚本只处理带标准时间戳的 `.lrc` 文件；没有时间戳或无法解码的文件会跳过并报告，不会删除。

### 网易云音乐搜索与下载

先用搜索找到歌曲 ID：

```bash
python3 search_music.py "あいみょん マリーゴールド"
python3 search_music.py "あいみょん マリーゴールド" --json   # 结构化输出，便于脚本处理
```

下载单曲或批量（每行一个 ID 的清单文件）：

```bash
python3 download_music.py 185868 --output-dir /Volumes/WALKMAN/Music
python3 download_music.py --input download-list.txt \
  --output-dir /Volumes/WALKMAN/Music \
  --quality exhigh \
  --lyrics translated
```

质量可选 `standard / higher / exhigh / lossless / hires`；歌词可选 `original / translated / bilingual`。正式下载前建议先 `--dry-run` 预览。

需要 GPT 翻译日语歌词时：

```bash
export OPENAI_API_KEY='在本机环境中设置，不要提交到版本库'
python3 download_music.py 歌曲ID \
  --output-dir /Volumes/WALKMAN/Music \
  --lyrics original \
  --translate-japanese
```

`process_music.py`（`.ncm` 转换）与 `walkman.py download / convert` 支持同样的 `--translate-japanese` 选项。

### 哔哩哔哩翻唱整理

准备：安装 yt-dlp，并准备浏览器导出的 Netscape 格式 cookie（默认文件 `www.bilibili.com_cookies.txt`，已被 `.gitignore` 排除，请勿提交）。

```bash
# 列出 UP 主全部视频（WBI 签名 API）
python3 walkman.py bilibili list 488970166 --output all_videos.json

# 批量获取 BV 号标题/时长
python3 walkman.py bilibili fetch BV1hD421p7Vy BV17tyHYgELa --output titles.json

# 批量下载 mp3（自动嵌入视频封面，已有同 BV 文件自动跳过）
python3 walkman.py bilibili download --input covers.txt \
  --output-dir Cover_Staging --no-proxy

# 重命名：去掉 [BV] 后缀；配合 JSON 译名表应用译名/规范名
python3 walkman.py bilibili rename Cover_Staging --translations trans.json --dry-run
python3 walkman.py bilibili rename Cover_Staging --translations trans.json

# 复制到最终音乐目录（同名跳过）
python3 walkman.py bilibili copy Cover_Staging "/Volumes/WALKMAN/Music/专辑名"
```

译名表是 `{BV号: 新文件名}` 的 JSON。不提供译名表时仅去掉 `[BV]` 后缀；重名文件自动加 `(2)` 序号。

### 封面 / 歌词补齐

```bash
# 为缺封面的音频批量嵌入网易云封面
python3 tools/embed_cover.py /Volumes/WALKMAN/Music/某目录 --dry-run
python3 tools/embed_cover.py /Volumes/WALKMAN/Music/某目录

# 为缺 LRC 的音频批量补歌词
python3 tools/fill_lyrics.py /Volumes/WALKMAN/Music/某目录
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 注意事项

- 请先安全弹出并重新插入存储卡，再用 Walkman 测试歌词显示与封面；备份目录不要急着删除，确认一切正常后再自行清理；
- 音频文件、歌词文件、下载 cookie 均不会提交到本仓库（见 `.gitignore`）。

## 免责声明

下载音乐涉及版权与服务条款，请只下载你有权保存或离线使用的内容；本项目不绕过 DRM、VIP 限制或其他访问控制。
