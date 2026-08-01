# PDF 字体来源与复现记录

## 准入结论

- 字体：Noto Sans SC
- 用途：`docs/competition-proposal.pdf` 的简体中文正文与标题
- 许可证：SIL Open Font License 1.1，完整文本见 `OFL.txt`
- 上游版权：Copyright 2014-2021 Adobe，Reserved Font Name 为 `Source`
- 分发：允许随源码和嵌入字体的 PDF 分发；字体文件不得单独销售
- 获取日期：2026-08-01

## 固定上游

- 官方分发仓库：<https://github.com/google/fonts/tree/main/ofl/notosanssc>
- 固定提交：`2894aab31764f10f29c421bdfd2340d3b382d384`
- 原始文件：`NotoSansSC[wght].ttf`
- 固定下载地址：<https://raw.githubusercontent.com/google/fonts/2894aab31764f10f29c421bdfd2340d3b382d384/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf>
- 原始字节数：17,772,300
- 原始 SHA-256：`A3041811A78C361B1DE50F953C805E0244951C21C5BD412F7232EF0D899AF0DA`

Google Fonts 元数据将该文件登记为 Noto Sans SC、OFL、`wght` 100-900。上游 Noto Sans CJK 对应发布版本为 2.004。

## 静态实例

ReportLab 需要稳定的 TrueType 静态字重。本项目使用 `fonttools==4.59.1` 从固定的官方变量字体生成两个未裁剪实例，并启用 `--update-name-table`：

```powershell
fonttools varLib.instancer "NotoSansSC-wght.ttf" wght=400 --update-name-table --output="NotoSansSC-Regular.ttf"
fonttools varLib.instancer "NotoSansSC-wght.ttf" wght=700 --update-name-table --output="NotoSansSC-Bold.ttf"
```

| 文件 | 内部字重 | 字节数 | SHA-256 |
|---|---:|---:|---|
| `NotoSansSC-Regular.ttf` | 400 / Regular | 10,595,932 | `198D40E70EE7CC1342EA436D2DFDF76E5AC13A9765DCD5F2A3A7655CF07F72F9` |
| `NotoSansSC-Bold.ttf` | 700 / Bold | 10,585,456 | `37BA4C2D255E3491D96988E5702DA52FE2F5665F375E8FEECAFA0102350CBFF2` |

静态实例是 OFL 所定义的 Modified Version。许可证保留的名称只有 `Source`；生成文件没有使用该名称，仍随 `OFL.txt` 按 OFL 1.1 分发。未裁剪字形、未转换格式，也未向 PDF 构建过程引入系统付费字体。
