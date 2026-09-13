# nass3cp

`nass3cp` 通过一个短暂的 S3 兼容对象存储中转，把普通文件复制到 NAS，或从 NAS 复制到本机。NAS 服务只承担控制流量；文件数据由发送端上传至对象存储，再由接收端直接下载。当前提供 Cloudflare R2（推荐）和阿里云 OSS 配置示例。

```text
本机 ──HTTPS──► R2/S3 ──HTTPS──► NAS     # 上传到 NAS
NAS  ──HTTPS──► R2/S3 ──HTTPS──► 本机    # 从 NAS 下载
       本机 ◄──HTTPS，或加密覆盖网内的 HTTP──► NAS
```

复制功能支持单个普通文件，也支持在本机目录和 NAS 目录之间递归合并复制；另提供非递归的 NAS 目录列表功能。单文件上传和下载可用 `--mode parallel` 按 64 MiB 分块并发和断点续传。运行时仅需 Python 3.8+ 标准库，没有第三方 Python 依赖。

## 安全模型

- NAS API 使用密码认证。客户端默认在终端安全提示输入密码，不需要把密码保存在客户端配置或命令行历史中。
- Windows 客户端可在验证密码成功后，将其保存到当前登录用户的 Windows 凭据管理器；程序不保存解密密钥，也不会把明文密码写入项目目录。
- 控制通道可选择应用层 TLS 1.2+，或在 ZeroTier/OpenTier、已启用加密的 FRP 等可信隧道内使用 HTTP。后者必须显式传 `--no-tls`。
- 内网 IP 本身不提供加密；使用无 TLS 模式时，保密性和完整性完全由覆盖网络或隧道承担。密码会作为每个控制请求的 Bearer 凭证发送，因此绝不能在未加密网络上使用该模式。
- S3 Endpoint 和客户端拿到的预签名 URL 始终必须是 HTTPS；无 TLS 模式不会降低文件数据通道的要求。
- S3 Secret Access Key 只保存在 NAS 服务端；客户端只得到最长 1 小时、只允许操作单个分块的预签名 URL（URL 中的 Access Key ID 本身不是秘密）。
- 文件默认按 64 MiB 分块，落盘前校验端到端 SHA-256；目标文件通过同目录临时文件原子替换，失败时不会留下半个目标文件。
- 服务端只允许访问 `allowed_roots` 内的路径，默认拒绝覆盖已有文件。
- R2 会自动使用 AES-256 静态加密；OSS 示例则显式要求 AES-256 服务端加密。普通流水线传输完成或失败后立即删除对象；断点续传会保留已完成分块直到传输成功或服务端 TTL 到期。仍建议给中转前缀配置 1 天生命周期规则，处理断电等极端情况。

这里的“传输加密”由 HTTPS/TLS 和可选的加密覆盖网络共同提供；对象存储服务在接收请求时仍可看到明文内容。若威胁模型要求云厂商也无法看到内容，需要增加客户端侧 AEAD 加密；本项目没有用标准库自制密码算法。

## 安装

推荐直接保留完整项目目录，不安装到系统路径。在 NAS 项目根目录执行：

```bash
cp config/server.env.example config/server.env
chmod 700 bin/nass3cp bin/nass3cp-server
chmod 600 config/server.env
```

项目内的 `config/server.json` 默认就是无应用层 TLS 的密码模式，不需要证书。编辑它和 `config/server.env` 后即可启动。启动器会从当前项目的 `src/` 加载代码并自动读取项目内配置，不需要 virtualenv、`pip install` 或 systemctl。仍需 TLS 时可以参考 `examples/server.cloudflare-r2.json`。

如果希望安装成系统命令，原来的 `python3 -m pip install /path/to/nass3cp` 方式仍然可用，此时命令名是 `nass3cp` 和 `nass3cp-server`。

## Cloudflare R2 准备（推荐）

1. 开通 R2 并创建一个专用于 `nass3cp` 的私有 Standard Bucket，位置可选择 Asia-Pacific。
2. 为前缀 `nass3cp/` 增加“1 天后删除”的生命周期规则。
3. 创建仅限这个 Bucket 的 **Object Read & Write** R2 API Token，保存只显示一次的 Access Key ID 和 Secret Access Key。
4. 记录 Account ID；S3 Endpoint 是 `https://ACCOUNT_ID.r2.cloudflarestorage.com`，SigV4 Region 固定填写 `auto`。

Cloudflare R2 支持本程序使用的 SigV4 预签名 PUT、GET 和 DELETE。R2 示例会在签名中加入官方示例使用的 `X-Amz-Content-Sha256=UNSIGNED-PAYLOAD`。它不支持 OSS 示例中的 `x-amz-server-side-encryption` 请求头，因此 R2 配置的 `put_headers` 必须保持为空；R2 会自动加密所有静态对象和元数据。

完整的账号点击步骤、最小权限设置、生命周期规则和上线检查见 [`docs/cloudflare-r2.md`](docs/cloudflare-r2.md)。

官方文档：[R2 API Token](https://developers.cloudflare.com/r2/api/tokens/)、[预签名 URL](https://developers.cloudflare.com/r2/api/s3/presigned-urls/)、[数据安全](https://developers.cloudflare.com/r2/reference/data-security/)。

## 阿里云 OSS 准备（可选）

1. 创建一个私有、标准存储（本地冗余）的 Bucket；地域尽量靠近 NAS 和客户端。
2. 在 Bucket 中启用 OSS 完全托管的 AES-256 默认加密。
3. 给前缀 `nass3cp/` 增加“1 天后删除”的生命周期规则。程序通常会立即删除对象，这条规则只是兜底。
4. 创建独立 RAM 用户，只授予该 Bucket 中转前缀的 `oss:GetObject`、`oss:PutObject`、`oss:DeleteObject` 权限，不要使用主账号 AccessKey。

最小 RAM 策略示例（替换 Bucket 名）：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["oss:GetObject", "oss:PutObject", "oss:DeleteObject"],
      "Resource": ["acs:oss:*:*:YOUR_BUCKET/nass3cp/*"]
    }
  ]
}
```

阿里云提供 S3 兼容 Endpoint，例如杭州为 `https://s3.oss-cn-hangzhou.aliyuncs.com`。本项目自己实现 SigV4，且发送固定 `Content-Length`，不使用 OSS 不支持的 `aws-chunked` 编码。

## 服务端配置

项目内的 [`config/server.json`](config/server.json) 已是“Cloudflare R2 + 无应用层 TLS + 密码认证”模板。修改其中的 Account ID、Bucket、NAS 路径和监听地址，再从 [`config/server.env.example`](config/server.env.example) 创建不会提交到 Git 的 `config/server.env`：

```text
NASS3CP_PASSWORD=用密码管理器生成的高强度随机密码
CLOUDFLARE_R2_ACCESS_KEY_ID=...
CLOUDFLARE_R2_SECRET_ACCESS_KEY=...
```

如果继续使用阿里云，则复制 [`examples/server.aliyun.json`](examples/server.aliyun.json)，并设置：

```bash
export NASS3CP_PASSWORD='用密码管理器生成的高强度随机密码'
export ALIBABA_CLOUD_ACCESS_KEY_ID='LTAI...'
export ALIBABA_CLOUD_ACCESS_KEY_SECRET='...'
```

配置里的 `${NAME}` 只有在整个 JSON 字符串恰好是该占位符时才会展开。项目启动器以数据文件方式读取 `config/server.env`，不会把它作为 shell 脚本执行；外部已有环境变量优先。实际密钥文件已经列入 `.gitignore`。

`auth.password` 既可以直接写字符串，也可以像模板一样引用项目内 `server.env` 的 `${NASS3CP_PASSWORD}`。推荐后者，避免误把密码提交到 Git。旧版的 `auth.token` 和 `NASS3CP_TOKEN` 仍兼容。

`config/server.json` 默认不使用应用层 TLS，并监听 `0.0.0.0:9443`。这会接受所有网卡上的连接；请用 NAS 防火墙把 TCP 9443 限制到覆盖网络网段，或者把 `listen` 改成 NAS 的覆盖网络虚拟 IP。仅供同机 FRP 使用时可以改成 `127.0.0.1`。

启动并先检查配置：

```bash
./bin/nass3cp-server --check-config
./bin/nass3cp-server --check-s3
./bin/nass3cp-server
```

`--check-s3` 会通过配置的中转服务 PUT 一个几十字节的临时对象、GET 校验并 DELETE；它用于确认 Endpoint、Bucket、凭证和权限确实可用。

默认配置监听 `0.0.0.0:9443`。建议通过 NAS 防火墙限制来源，或改为只监听 NAS 的覆盖网络虚拟 IP。

需要退出 SSH 后继续运行时，先执行 `mkdir -p run`，再使用 `nohup ./bin/nass3cp-server >run/nass3cp.log 2>&1 &`。完整的启动、PID、日志和停止命令见 [`docs/cloudflare-r2.md`](docs/cloudflare-r2.md)。SigV4 对时钟敏感，请确保 NAS 时间已自动同步。

## 使用

Windows 不会执行无扩展名文件中的 shebang，请使用项目内的 `bin\nass3cp.cmd`。它会依次寻找 `py -3`、`python` 或 `python3`，并要求 Python 3.8+。例如在 PowerShell 中从 NAS 下载到当前目录：

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --port 9443 `
  nas:/volume1/share/movie.mkv .
```

如果 Python 没有加入 `PATH`，可以只为当前 PowerShell 会话指定解释器：

```powershell
$env:NASS3CP_PYTHON = "C:\Path\To\Python\python.exe"
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 nas:/volume1/share/movie.mkv .
```

Linux 和 macOS 继续使用下面的无扩展名启动器。

覆盖网络模式下，本机上传到 NAS：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --port 9443 \
  ./movie.mkv nas:/volume1/share/movie.mkv
NAS password:
```

从 NAS 下载到本机：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --port 9443 \
  nas:/volume1/share/movie.mkv ./movie.mkv
NAS password:
```

### 单文件传输模式与断点续传

默认的 `--mode pipeline` 严格按块顺序消费，S3 中最多保留 `--inflight` 个尚未确认的块。`--mode parallel` 则把所有未完成块作为独立任务放入滚动任务池，不受 `--inflight` 限制，但任意时刻实际执行的 S3 请求仍最多为 `--jobs` 个；它同时启用本地块清单和断点续传。上传和下载都支持。传输被网络错误、进程退出或 `Ctrl+C` 中断后，在 `transfer_ttl_seconds`（默认 24 小时）内重新运行完全相同的命令：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --mode parallel \
  ./movie.mkv nas:/volume1/share/movie.mkv

./bin/nass3cp --no-tls --host 10.10.10.2 --mode parallel \
  nas:/volume1/share/movie.mkv ./movie.mkv
```

原有的 `--resume` 仍可使用，等价于 `--mode parallel`。

客户端会在本地保存隐藏的 JSON 块清单。上传清单位于源文件旁，名称为 `.<文件名>.nass3cp-upload.json`；下载清单位于目标旁，名称为 `.<文件名>.nass3cp-download.json`，并配套一个 `.<文件名>.<随机值>.nass3cp-part` 临时文件。清单记录传输 ID、文件身份、分块大小，以及每个已完成块的 SHA-256。下载块按索引写入临时文件的固定偏移，因此并发时即使后面的块先完成，也能在下一次只请求缺失或校验失败的块。完整文件通过端到端 SHA-256 校验并原子落盘后，这些本地文件会自动删除。

并行断点续传要求客户端和 NAS 服务端都升级到支持该协议的版本，且只用于非压缩的单文件复制，不能和 `--recursive` 一起使用。为了让下一次仍能取得已完成块，`parallel` 模式不会在中断时主动清理中转对象；S3 峰值占用最多可接近整个文件大小，成功后会立即清理，过期传输则由服务端 TTL 和 Bucket 生命周期规则兜底。续传清单与当前主机、源/目标路径、文件大小和修改时间绑定；源文件发生变化时程序会拒绝误续传，删除清单即可明确地从头开始。

列出 NAS 目录（不经过 S3，也不会产生对象存储请求）：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --port 9443 \
  ls nas:/volume1/share
NAS password:
```

输出依次显示类型（`d` 目录、`-` 普通文件、`l` 符号链接）、文件字节数、修改时间和名称。目录名以 `/` 结尾，符号链接以 `@` 结尾。`ls nas:` 可列出 `allowed_roots` 第一项的根目录；列表不递归，大目录由客户端自动分页获取。

### 递归复制目录

`-r`（或 `--recursive`）会先扫描完整的源目录和目标目录，再把源目录的内容合并到指定的目标根目录。本机上传到 NAS：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 -r \
  ./photos nas:/volume1/share/photos
```

从 NAS 下载到本机：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 -r \
  nas:/volume1/share/photos ./photos
```

这里采用“目标就是合并根”的语义：上例会把 `./photos` 中的相对路径直接放进 NAS 的 `photos/`，不会再自动追加一层源目录名。目标根不存在时会创建，空目录也会保留。递归命令中的 `nas:` 可直接表示 `allowed_roots` 第一项的根目录。

发送前会按相对路径检查目标。目标上已有同名普通文件时直接跳过，即使两边大小或内容不同也不会覆盖；递归模式因此不能与 `--overwrite` 同用。源文件与目标目录同名、源目录与目标非目录同名等结构冲突，会在开始创建目录或传输文件前统一报错。每个文件开始前还会再次检查并跳过扫描后新出现的同名文件。符号链接、设备、socket 等非普通源条目不会跟随，而是计入跳过统计。每个文件继续使用临时文件、SHA-256 校验和原子落盘，但整个目录复制不是事务：如果中途发生网络或磁盘错误，已经完成的文件会保留，重新运行 `-r` 即可跳过它们并继续其余文件。

递归策略默认为 `--rpolicy=auto`。它通过不区分大小写的后缀名识别通常压缩收益较高的文本、源码、日志、CSV/JSON/XML、SQL/SQLite 等文件，优先处理这些候选文件，并用 gzip 临时压缩；只有压缩结果确实小于原文件才通过 S3 发送压缩数据。接收端校验压缩数据和还原内容的两份 SHA-256，最终文件名、内容和修改时间保持原样。图片、视频、音频和已有压缩包等默认原样发送。使用 `--rpolicy=raw` 可关闭这项判断，让所有普通文件原样中转：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 -r --rpolicy=raw \
  ./photos nas:/volume1/share/photos
```

`auto` 会先在发送端生成一个完整的临时 gzip 文件，因此本机上传需要本机临时目录有足够空间，NAS 下载则需要 NAS 的 `state_dir` 有足够空间。可正常处理的完成、失败和取消路径会删除临时文件；如果本机进程被直接强杀，操作系统临时目录中可能留下一个 `nass3cp-*.gz` 文件。递归复制需要客户端和 NAS 服务端都升级到支持这些接口的版本。

增加 `--dry` 可以只生成计划，不创建目录、不上传或下载文件，也不创建 S3 对象；它仍会认证并通过控制 API 读取 NAS 目录。输出包括源文件数、全部普通文件的原始总字节数、已有目标的跳过字节数、预计传输的原始字节数、自动压缩候选、待创建目录、非普通条目和路径冲突：

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 -r --dry \
  ./photos nas:/volume1/share/photos
```

`--dry` 只用于递归复制，必须和 `-r` 一起使用。统计中的“原始大小”不是预估压缩量，也不是 S3 计费存储量；它是扫描时所有普通源文件大小的精确求和。

这里的 `10.10.10.2` 要换成 NAS 的 ZeroTier/OpenTier IP；如果使用 FRP，则换成加密隧道提供的本地入口地址。密码输入不会回显。

### 在 Windows 中记住密码

第一次连接时增加 `--remember-password`。客户端会先向 NAS 验证密码，只有认证成功才写入当前用户的 Windows 凭据管理器：

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --remember-password ls nas:
```

以后对相同主机、端口和连接安全模式执行复制或 `ls` 时会自动读取，不再提示输入。显式传入的 `--password-file`、`NASS3CP_PASSWORD` 或兼容的旧参数优先级更高。

密码改变时，可以忽略旧值并验证、覆盖保存新密码：

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 `
  --no-saved-password --remember-password ls nas:
```

删除保存的密码（安全模式参数应与保存时一致）：

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --forget-password
```

验证过的 HTTPS、`--insecure` HTTPS 和 `--no-tls` 会使用彼此独立的凭据项，防止较弱连接自动复用较强连接下保存的密码。Windows 凭据保护可以防止其他系统用户直接读取，但不能防御已经以当前 Windows 用户身份运行的恶意程序。非 Windows 平台或没有交互式用户凭据会话的定时任务仍应使用 `--password-file`，或在项目内创建 `config/client.env` 并设置 `NASS3CP_PASSWORD`。

TLS 模式仍然可用：

```bash
./bin/nass3cp --host nas.example --port 9443 --ca-file nas-ca.crt \
  ./movie.mkv nas:/volume1/share/movie.mkv
```

复制时恰好一个路径参数必须以 `nas:` 开头。NAS 相对路径以 `allowed_roots` 的第一项为基准；绝对路径也必须位于某个允许根目录中。单文件复制遇到已有目标时默认报错，明确传 `--overwrite` 才会替换；递归复制始终跳过已有同名普通文件。两个传输模式的客户端 S3 请求并发数都由 `--jobs` 控制，默认 2，范围 1–16。`ls` 同样只能访问这些允许根目录。

默认的 `--mode pipeline` 使用有界分块流水线。`--inflight` 控制允许同时存在于 S3 中、尚未被接收方确认的最大分块数，默认 3，范围 1–128；接收方严格按索引顺序校验、写入并确认分块。默认 64 MiB 分块配合 `--inflight 3` 时，单个正常传输的云端峰值约为 192 MiB，而不再等于整个文件大小；同时运行多个复制进程时，各自的窗口会叠加。`--mode parallel` 会改为独立调度全部未完成块并保留已完成块，此时 `--jobs` 是实际并发上限，`--inflight` 不参与控制。普通单文件传输与旧服务端混用时会自动回退到原来的整文件中转协议；并行断点续传、递归目录与透明压缩要求两端均为新版。

复制时默认显示实时进度条、已传输大小、速度和预计剩余时间。流水线阶段会显示经 S3 复制的整体进度；使用 `--quiet` 可以关闭进度输出。终端内会原地刷新，重定向到日志时则按行记录进度。

`--insecure` 仍会加密控制流量，但不验证 NAS 身份，可能遭受中间人攻击，仅用于临时排障。

## 约 1 GiB 的 Cloudflare R2 费用

R2 Standard 当前每月免费额度包括 10 GB-month 存储、100 万次 Class A 操作和 1000 万次 Class B 操作，并且从 R2 直接流出公网免费。默认 64 MiB 分块的一次 1 GiB 复制约产生 16 次 PUT、16 次 GET 和 16 次免费 DELETE；对象在接收方确认后立即删除，所以存储用量取决于流水线窗口而不是当月累计传输量。如果账号当月仍在免费额度内，总成本通常为 **$0**。

免费额度只适用于 Standard。不要为临时中转使用 Infrequent Access，因为它没有这份免费额度，还存在数据读取费和 30 天最短存储期。价格会调整，最终以 [Cloudflare R2 定价](https://developers.cloudflare.com/r2/pricing/) 为准。

## 约 1 GB 的阿里云费用

以下按 2026-09-12 中国内地公共云、标准本地冗余、按量付费、普通公网 Endpoint、没有资源包或免费额度估算：

| 项目 | 1 GB 单次复制 |
|---|---:|
| 上传到 OSS | ¥0（流入免费） |
| 从 OSS 公网下载，00:00–08:00 | 约 ¥0.25 |
| 从 OSS 公网下载，08:00–24:00 | 约 ¥0.50 |
| 临时存储 1 小时 | 约 ¥0.000167 |
| 16 PUT + 16 GET + 约 16 DELETE | 免费额度外也仅约 ¥0.000048 |
| 合计 | 闲时约 ¥0.2502；忙时约 ¥0.5002 |

上传到 NAS 和从 NAS 下载的价格基本相同：两者都是一次免费流入 OSS，再有一次 OSS 公网流出。重试会重复产生流出流量；使用传输加速 Endpoint 还会叠加传输加速费，所以示例没有开启。标准存储当前每月每地域前 500 万次 PUT、前 2000 万次 GET 免费，实际小规模使用的请求费通常为零。

价格会调整，最终以阿里云账单为准：[OSS 价格详情](https://cn.aliyun.com/price/detail/oss)、[流量费用说明](https://help.aliyun.com/zh/oss/traffic-fees)、[存储费用说明](https://help.aliyun.com/zh/oss/storage-fees)、[使用 AWS SDK/S3 API 访问 OSS](https://help.aliyun.com/zh/oss/developer-reference/use-aws-sdks-to-access-oss)。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```
