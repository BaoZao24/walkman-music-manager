# Walkman LRC Repair

一个用于修复 LRC 歌词文件、提高 Sony Walkman 兼容性的独立脚本。

## 能处理的问题

- 将 UTF-8 或 GB18030 文本统一转换为 UTF-8；
- 删除网易云歌词文件中混入的 JSON 元数据；
- 删除 Walkman 不支持的普通元数据行；
- 将时间戳统一为 `[分:秒.百分之一秒]` 格式；
- 保留原目录结构，并在修改前建立带时间戳的隐藏备份目录；
- 采用原子替换，减少写入中断造成文件损坏的风险；
- 默认跳过 macOS 产生的 `._*.lrc` AppleDouble sidecar 文件。

## 使用方法

建议先做一次预览：

```bash
python3 walkman_lrc_repair.py /Volumes/Biwin
```

确认扫描数量和待修复数量后，正式执行：

```bash
python3 walkman_lrc_repair.py /Volumes/Biwin --apply
```

正式执行时，原文件会备份到类似下面的目录：

```text
/Volumes/Biwin/.walkman-lrc-backup-20260816-120000/
```

也可以手动指定备份目录：

```bash
python3 walkman_lrc_repair.py /Volumes/Biwin \
  --apply \
  --backup-root /Volumes/Biwin/.my-lrc-backup
```

脚本只处理带标准时间戳的 `.lrc` 文件；没有时间戳或无法解码的文件会跳过并报告，不会删除。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 注意

请先安全弹出并重新插入存储卡，再用 Walkman 测试歌词显示。备份目录不要删除，确认一切正常后再自行清理。

## 自动下载音乐与歌词

本项目可以调用 [neteasecli](https://github.com/wangwalk/neteasecli) 下载你有权使用的网易云音乐内容，并把歌词整理成 Walkman 可读的 LRC 文件。先安装并登录：

```bash
npm install --global neteasecli
neteasecli auth login
neteasecli auth check
```

登录时，`neteasecli` 会从浏览器导入网易云登录状态；本项目不会读取或保存 Cookie。先用网易云搜索命令找到歌曲 ID：

```bash
neteasecli --json search track "歌曲名"
```

下载一首歌到存储卡的音乐目录：

```bash
python3 download_music.py 185868 \
  --output-dir /Volumes/Biwin/Music
```

也可以准备一个 ID 清单，每行一个歌曲 ID：

```text
# download-list.txt
185868
186016
```

```bash
python3 download_music.py \
  --input download-list.txt \
  --output-dir /Volumes/Biwin/Music \
  --quality exhigh \
  --lyrics translated
```

默认会按艺术家建立子文件夹，已有文件会跳过；`--lyrics translated` 优先使用网易云提供的中文翻译，没有翻译时回退到原文。也可以使用 `--lyrics original` 或 `--lyrics bilingual`。正式下载前建议先预览：

```bash
python3 download_music.py 185868 \
  --output-dir /Volumes/Biwin/Music \
  --dry-run
```

下载音乐涉及版权和服务条款，请只下载你有权保存或离线使用的内容；脚本不绕过 DRM、VIP 限制或其他访问控制。

### 第一阶段：搜索并下载

按关键词搜索歌曲，结果中的 ID 可以直接交给下载脚本：

```bash
python3 search_music.py "あいみょん マリーゴールド"
```

如果要让其他脚本或 Codex 读取结果，可以输出结构化 JSON：

```bash
python3 search_music.py "あいみょん マリーゴールド" --json
```

确认歌曲 ID 后再下载：

```bash
python3 download_music.py 1352857358 \
  --output-dir /Volumes/Biwin/Music \
  --lyrics translated
```

多个 ID 可以一次下载，也可以放入清单文件。搜索和下载分开，能避免同名歌曲被自动选错；后续完整工作流会在这里继续接入格式转换、歌词处理和移动到存储卡。
