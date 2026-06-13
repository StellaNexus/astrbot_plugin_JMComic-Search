# AstrBot JMComic 禁漫搜索插件

从禁漫天堂搜索本子，支持分页结果（合并转发封面+名字）、查看详情、下载章节为 PDF。

基于 [JMComic-Crawler-Python](https://github.com/hect0x7/JMComic-Crawler-Python) 库实现。

## 命令

| 命令 | 说明 |
|------|------|
| `/jm 关键词` | 分页搜索（8条/页，最多3页），合并转发封面+名字 |
| `+/下一页` | 翻到下一页 |
| `-/上一页` | 翻到上一页 |
| `回复数字` | 选择当页结果查看详情 |
| `/jm 本子ID` | 直接查看指定ID的本子详情 |
| `/jm看图 当页序号` | 取当页第N本 → 下载第1话 → 生成PDF发送 |
| `/jm看图 章节ID` | 直接下载指定章节ID的图片并拼接 PDF |

## 安装

1. 将本插件文件夹放入 AstrBot 的 `data/plugins/` 目录
2. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```
3. 重启 AstrBot

## 配置

编辑 `config.yaml`：

```yaml
client_impl: "api"   # "api"(推荐,不限IP) 或 "html"(需特定地区IP)
proxy: ""            # 代理，如 http://127.0.0.1:7890
domains: []          # 自定义域名，留空使用默认
```

## 使用示例

```
用户: /jm 少女
Bot:  [合并转发] 8本封面+名字，末尾 「少女」第1/3页
用户: +
Bot:  [合并转发] 下一页结果
用户: 3
Bot:  [合并转发] 第3本详情（封面+文字+章节列表）
用户: /jm看图 3
Bot:  共 20 张，第3本 第1话.pdf → [PDF文件]
```

## 依赖

- jmcomic >= 2.6.0
- img2pdf >= 0.4.4（推荐，流式图片转PDF）
- Pillow >= 9.0.0（img2pdf 不可用时的备选）
- PyPDF2 >= 3.0.0（Pillow 备选模式下合并 PDF 分块）