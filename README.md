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
