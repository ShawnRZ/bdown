# bdown

下载 bilibili 视频的命令行工具：给一个 BV 号就能下载。

## 特性

- 输入 BV 号、av 号或视频链接均可
- 自动挑选最高可用画质，也可以指定 qn / 编码（avc、hevc、av1）
- DASH 音视频流分别下载后用 ffmpeg 无损封装成 MP4（不重新编码）
- 分块并发下载，**支持断点续传**：中断后重跑会接着下，不会从零开始
- 主地址失败时自动切备用 CDN，单块失败自动重试
- 多分 P 稿件可按 `1,3-5` 的形式挑选，自动按标题建目录
- 支持只下音频（`--audio-only`）、无损 / 杜比音轨（`--hires`）

## 依赖

- Python ≥ 3.12
- [ffmpeg](https://ffmpeg.org/)（用于合并音视频，Debian/Ubuntu：`sudo apt install ffmpeg`）

## 安装

```bash
uv sync                  # 安装到项目虚拟环境
uv run bdown --help
```

也可以装成全局命令：

```bash
uv tool install .
bdown --help
```

## 用法

```bash
bdown BV1kktD69EaX                      # 下载最高可用画质
bdown BV1kktD69EaX -q 80                # 指定 1080P
bdown BV1kktD69EaX -o ~/视频            # 指定输出目录
bdown BV1kktD69EaX --codec hevc         # 用 HEVC，同画质体积更小
bdown BV1kktD69EaX -p 1,3-5             # 只下第 1、3~5 个分 P
bdown BV1kktD69EaX --list               # 只看分 P 和可用画质，不下载
bdown BV1kktD69EaX --audio-only         # 只要音频，输出 m4a
bdown BV1kktD69EaX -j 16                # 提高并发分块数
bdown BV1aaa BV1bbb                     # 一次下多个
```

`--list` 会列出该稿件的分 P 和当前账号能拿到的全部流：

```
剧版哈利就是越长越好看啊  (BV1JmTE6zEqM)
UP 主：小鱼怪   分 P：2
分 P 列表
P  标题                    时长
1  哈利剪辑history         0:19
2  多米尼克《麦克白》谢幕  0:30
可用视频流（第一个分 P）
qn  清晰度     分辨率          编码  估算体积
32  480P 清晰  480x640@30.000  avc   836.9KiB
...
```

## 关于清晰度与登录

**不登录最高只能拿到 480P。** 更高画质需要提供浏览器里的 Cookie：

1. 浏览器登录 bilibili，开发者工具 → Network，复制任一 api 请求的 `Cookie` 请求头
   （至少要包含 `SESSDATA`）
2. 按任一方式提供给 bdown：

```bash
# 方式一：写入配置文件（推荐）
mkdir -p ~/.config/bdown && vim ~/.config/bdown/cookie.txt

# 方式二：环境变量
export BILI_COOKIE='SESSDATA=xxxx; bili_jct=yyyy'

# 方式三：命令行参数
bdown BV1kktD69EaX --cookie 'SESSDATA=xxxx'
```

各档位能力大致如下：未登录 480P；登录后 1080P；大会员可用 1080P+、4K、HDR、
杜比视界及无损音轨。`-q` 请求的档位拿不到时会自动退到不超过它的最高档，并给出提示。

清晰度 qn 取值：

| qn | 画质 | qn | 画质 |
|----|------|----|------|
| 16 | 360P | 112 | 1080P+ |
| 32 | 480P | 116 | 1080P60 |
| 64 | 720P | 120 | 4K |
| 74 | 720P60 | 125 | HDR |
| 80 | 1080P | 127 | 8K |

## 实现说明

`playurl` 等接口要求 WBI 签名（`wts` + `w_rid`）：签名密钥由 `nav` 接口下发的两张
图片文件名经固定重排表推导，参数需按键排序并以浏览器 `URLSearchParams` 的规则编码后
取 MD5。`src/bdown/wbi.py` 实现了这套算法，`tests/wbi_vectors.json` 存了几组从浏览器
抓包提取的真实签名样本（不含任何凭据）用于锁定行为。

下载器把文件切成 4MiB 的块并发抓取，各线程用 `os.pwrite` 写入同一文件的不同偏移，
进度记录在 `<文件名>.bdown` 里；因为进度总在数据落盘之后才更新，中断后最多重下一小段，
不会出现空洞。服务端不支持 Range 时自动退回单连接下载。

## 开发

```bash
uv run pytest              # 全部测试，不需要联网
```

## 说明

请仅下载你有权获取的内容，遵守 bilibili 的用户协议，尊重创作者的权益。
