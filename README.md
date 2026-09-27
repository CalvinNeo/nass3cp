# nass3cp

English | [简体中文](README.zh-CN.md)

`nass3cp` copies regular files to or from a NAS through temporary S3-compatible object storage. The NAS service handles control traffic; the sender uploads file data to object storage, and the receiver downloads it directly. Configuration examples are available for Cloudflare R2 (recommended) and Alibaba Cloud OSS.

```text
Client ──HTTPS──► R2/S3 ──HTTPS──► NAS     # Upload to NAS
NAS    ──HTTPS──► R2/S3 ──HTTPS──► Client  # Download from NAS
       Client ◄──HTTPS, or HTTP over an encrypted overlay──► NAS
```

The copy commands support individual regular files and recursive merges between local and NAS directories. Non-recursive NAS directory listing is also available. Single-file uploads and downloads can use `--mode parallel` for concurrent transfers and resumption with 64 MiB chunks. The runtime requires only Python 3.8+ and its standard library, with no third-party Python dependencies.

## Security model

- The NAS API uses password authentication. By default, the client prompts securely in the terminal, so the password does not need to be saved in client configuration or shell history.
- After successful authentication, Windows clients can save the password in Windows Credential Manager for the signed-in user. The program does not store a decryption key or write the plaintext password to the project directory.
- The control channel can use application-layer TLS 1.2+, or HTTP inside a trusted encrypted tunnel such as ZeroTier/OpenTier or FRP with encryption enabled. HTTP requires the explicit `--no-tls` option.
- A private IP address does not provide encryption. In no-TLS mode, confidentiality and integrity depend entirely on the overlay network or tunnel. The password is sent as a Bearer credential with every control request, so never use this mode over an unencrypted network.
- S3 endpoints and presigned URLs received by the client must always use HTTPS. No-TLS mode does not relax the requirements for the file data channel.
- The S3 Secret Access Key stays on the NAS server. The client receives only presigned URLs that authorize an operation on one chunk and expire within one hour. The Access Key ID in a URL is not itself a secret.
- Files use 64 MiB chunks by default, with end-to-end SHA-256 verification before the destination is finalized. A temporary file in the destination directory is atomically moved into place, so a failure does not leave a partial destination file.
- The server restricts access to paths within `allowed_roots` and rejects overwriting existing files by default.
- R2 automatically uses AES-256 encryption at rest; the OSS example explicitly requests AES-256 server-side encryption. Normal pipeline transfers delete objects immediately after completion or failure. Resumable transfers retain completed chunks until success or the server TTL expires. A one-day lifecycle rule for the relay prefix is still recommended to handle events such as power loss.

Encryption in transit is provided by HTTPS/TLS and, optionally, an encrypted overlay network. The object storage provider can still see plaintext when processing requests. If your threat model requires hiding content from the cloud provider, add client-side AEAD encryption; this project does not implement custom cryptography with the standard library.

## Installation

The recommended setup keeps the complete project directory without installing into system paths. From the project root on the NAS:

```bash
cp config/server.env.example config/server.env
chmod 700 bin/nass3cp bin/nass3cp-server
chmod 600 config/server.env
```

The bundled `config/server.json` uses password authentication without application-layer TLS, so no certificate is required. Edit it and `config/server.env`, then start the service. The launchers load code from the project's `src/` directory and automatically read local configuration; no virtual environment, `pip install`, or systemctl is required. See `examples/server.cloudflare-r2.json` if you need TLS.

To install system commands instead, use `python3 -m pip install /path/to/nass3cp`. The installed command names are `nass3cp` and `nass3cp-server`.

## Prepare Cloudflare R2 (recommended)

1. Enable R2 and create a dedicated private Standard bucket for `nass3cp`. Asia-Pacific is one location option.
2. Add a lifecycle rule that deletes objects under the `nass3cp/` prefix after one day.
3. Create an **Object Read & Write** R2 API token restricted to this bucket. Save the Access Key ID and Secret Access Key when they are displayed.
4. Record the Account ID. The S3 endpoint is `https://ACCOUNT_ID.r2.cloudflarestorage.com`, and the SigV4 region must be `auto`.

Cloudflare R2 supports the SigV4 presigned PUT, GET, and DELETE operations used by this program. The R2 example includes `X-Amz-Content-Sha256=UNSIGNED-PAYLOAD` in the signature, as in the official examples. R2 does not support the `x-amz-server-side-encryption` header used by the OSS example, so keep `put_headers` empty for R2. R2 automatically encrypts all objects and metadata at rest.

See [`docs/cloudflare-r2.md`](docs/cloudflare-r2.md) for detailed account setup, minimum permissions, lifecycle rules, and deployment checks.

Official documentation: [R2 API tokens](https://developers.cloudflare.com/r2/api/tokens/), [presigned URLs](https://developers.cloudflare.com/r2/api/s3/presigned-urls/), and [data security](https://developers.cloudflare.com/r2/reference/data-security/).

## Prepare Alibaba Cloud OSS (optional)

1. Create a private Standard bucket with local redundancy, preferably in a region close to the NAS and client.
2. Enable OSS-managed AES-256 encryption as the bucket default.
3. Add a lifecycle rule that deletes objects under `nass3cp/` after one day. The program normally deletes objects immediately; this rule is a fallback.
4. Create a dedicated RAM user with only `oss:GetObject`, `oss:PutObject`, and `oss:DeleteObject` permissions for the bucket's relay prefix. Do not use the primary account's AccessKey.

Example minimum RAM policy; replace the bucket name:

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

Alibaba Cloud provides S3-compatible endpoints, such as `https://s3.oss-cn-hangzhou.aliyuncs.com` for Hangzhou. This project implements SigV4 and sends a fixed `Content-Length`; it does not use the `aws-chunked` encoding that OSS does not support.

## Server configuration

The bundled [`config/server.json`](config/server.json) is a template for Cloudflare R2 with password authentication and no application-layer TLS. Update the Account ID, bucket, NAS paths, and listen address. Then copy [`config/server.env.example`](config/server.env.example) to the Git-ignored `config/server.env`:

```text
NASS3CP_PASSWORD=strong-random-password-generated-by-a-password-manager
CLOUDFLARE_R2_ACCESS_KEY_ID=...
CLOUDFLARE_R2_SECRET_ACCESS_KEY=...
```

For Alibaba Cloud, copy [`examples/server.aliyun.json`](examples/server.aliyun.json) and set:

```bash
export NASS3CP_PASSWORD='strong-random-password-generated-by-a-password-manager'
export ALIBABA_CLOUD_ACCESS_KEY_ID='LTAI...'
export ALIBABA_CLOUD_ACCESS_KEY_SECRET='...'
```

`${NAME}` is expanded only when it is the entire JSON string value. The project launcher reads `config/server.env` as a data file, never as a shell script. Existing process environment variables take precedence. Files containing actual secrets are already listed in `.gitignore`.

`auth.password` accepts a literal string or a reference to `${NASS3CP_PASSWORD}` from the project's `server.env`, as shown in the template. The latter reduces the risk of committing a password to Git. The legacy `auth.token` and `NASS3CP_TOKEN` settings remain supported.

By default, `config/server.json` disables application-layer TLS and listens on `0.0.0.0:9443`, accepting connections on all network interfaces. Restrict TCP port 9443 to the overlay network subnet with the NAS firewall, or change `listen` to the NAS overlay IP. Use `127.0.0.1` when only a local FRP process needs access.

Validate the configuration before starting:

```bash
./bin/nass3cp-server --check-config
./bin/nass3cp-server --check-s3
./bin/nass3cp-server
```

`--check-s3` uploads a temporary object of a few dozen bytes to the configured relay with PUT, verifies it with GET, and deletes it with DELETE. This checks that the endpoint, bucket, credentials, and permissions work together.

The default configuration listens on `0.0.0.0:9443`. Restrict incoming connections with the NAS firewall or bind only to the NAS overlay IP.

To keep the service running after disconnecting SSH, run `mkdir -p run`, then `nohup ./bin/nass3cp-server >run/nass3cp.log 2>&1 &`. See [`docs/cloudflare-r2.md`](docs/cloudflare-r2.md) for startup, PID, logging, and shutdown commands. SigV4 is sensitive to clock skew, so keep the NAS clock synchronized.

## Usage

Windows does not execute shebangs in extensionless files. Use the bundled `bin\nass3cp.cmd`, which tries `py -3`, `python`, and then `python3`, and requires Python 3.8+. For example, download from the NAS into the current directory in PowerShell:

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --port 9443 `
  nas:/volume1/share/movie.mkv .
```

If Python is not on `PATH`, select the interpreter for the current PowerShell session:

```powershell
$env:NASS3CP_PYTHON = "C:\Path\To\Python\python.exe"
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 nas:/volume1/share/movie.mkv .
```

On Linux and macOS, use the extensionless launcher shown below.

Upload a local file to the NAS over an encrypted overlay network:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --port 9443 \
  ./movie.mkv nas:/volume1/share/movie.mkv
NAS password:
```

Download a file from the NAS:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --port 9443 \
  nas:/volume1/share/movie.mkv ./movie.mkv
NAS password:
```

### Single-file transfer modes and resumption

The default `--mode pipeline` consumes chunks strictly in order and keeps at most `--inflight` unacknowledged chunks in S3. With `--mode parallel`, all unfinished chunks become independent tasks in a rolling worker pool. This mode ignores `--inflight`, but at most `--jobs` client S3 requests run at once. It also enables local chunk manifests and resumption for both uploads and downloads. After a network error, process exit, or `Ctrl+C`, rerun the exact same command within `transfer_ttl_seconds` (24 hours by default):

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --mode parallel \
  ./movie.mkv nas:/volume1/share/movie.mkv

./bin/nass3cp --no-tls --host 10.10.10.2 --mode parallel \
  nas:/volume1/share/movie.mkv ./movie.mkv
```

The legacy `--resume` option remains available and is equivalent to `--mode parallel`.

The client stores a hidden JSON chunk manifest locally. Upload manifests sit beside the source file as `.<filename>.nass3cp-upload.json`. Download manifests sit beside the destination as `.<filename>.nass3cp-download.json`, alongside a `.<filename>.<random>.nass3cp-part` temporary file. A manifest records the transfer ID, file identity, chunk size, and SHA-256 of each completed chunk. Download chunks are written at fixed offsets based on their index. Even when later chunks finish first, the next run requests only missing chunks or chunks that fail verification. These local files are removed after the complete file passes end-to-end SHA-256 verification and is atomically moved into place.

Parallel resumption requires compatible client and NAS server versions. It supports only uncompressed single-file copies and cannot be combined with `--recursive`. To make completed chunks available on the next run, `parallel` mode retains relay objects after interruption. Peak S3 usage can approach the full file size. Objects are cleaned up immediately after success; expired transfers are covered by the server TTL and bucket lifecycle rules. A resume manifest is tied to the host, source and destination paths, file size, and modification time. The program refuses to resume against a changed source; delete the manifest to explicitly start over.

List a NAS directory without using S3 or making object storage requests:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 --port 9443 \
  ls nas:/volume1/share
NAS password:
```

The output shows type (`d` for directories, `-` for regular files, and `l` for symlinks), size in bytes, modification time, and name. Directory names end in `/`; symlink names end in `@`. `ls nas:` lists the first entry in `allowed_roots`. Listings are non-recursive, and the client automatically fetches additional pages for large directories.

### Browse NAS files in a local web page

Use `browse` to connect to the NAS. After authentication and a readability check for the starting directory, the client starts a local web service and opens the browser:

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 browse nas:/volume1/share
```

Linux / macOS:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 browse nas:/volume1/share
```

Omitting the directory (`browse`) or using `browse nas:` starts at the first entry in `allowed_roots`. Password prompts, `--password-file`, TLS options, and Windows `--remember-password` behave as they do for `ls`.

The English-language UI shows name, type, size, creation time, and modification time. You can open folders, go to the parent directory, switch shared roots, enter a NAS path, refresh, and navigate pages of up to 100 entries. Sizes use B / KiB / MiB units; hover to see exact byte counts. Directory sizes are not calculated recursively. Symlinks are listed but cannot be opened as folders or selected for download. Images and videos appear as ordinary files, without thumbnails or previews.

Dates use the client machine's local time zone. Creation time is shown only when the filesystem and Python provide a true creation timestamp. NAS systems without that field, including many Linux environments, show "N/A"; Unix `ctime` is not treated as creation time. Older NAS servers still support basic listings. Update the server to expose creation times, shared roots, and accurate parent-directory information.

The default web address is `http://localhost:8765/`. Use the **complete session link** printed by the command for your first visit. Set a different port with `--web-port`, or use `0` to select an available port. `--no-browser` prints the link without opening a browser:

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --web-port 0 --no-browser browse nas:
```

Keep the command running and press `Ctrl+C` to stop the web service. It listens only on `127.0.0.1` and creates a separate local session credential at each startup. The NAS password stays in the client process and is never sent to the web page. Browsing reads directories through the control API only: it does not use S3 or upload or download file contents. Access remains restricted by the NAS `allowed_roots`.

### Download selected files in the browser

Select regular files in directory listings or search results, then click **Download selected**. Selection is kept across folders and pages in the same tab. Each file downloads separately through the existing S3 pipeline, with chunk and end-to-end SHA-256 verification. This UI does not support downloading folders; recursive CLI copies remain available.

The compact **Downloads** table has one row per file, showing its status, progress, byte counts, average transfer speed, and cancel or retry action. The stages distinguish downloading from the NAS through S3, verification, and sending to the browser. Refreshing or navigating the local page restores the queue. The final **Sent to browser** status means the local service finished sending the file; check the browser's download list for its final save status. Saving follows your browser's download-directory and prompt settings. If it blocks automatic or multiple downloads, allow them for this local page or click **Save file**.

File concurrency defaults to **1**, configurable from 1 to 8 in the local `config/client.env`:

```dotenv
NASS3CP_DOWNLOAD_CONCURRENCY=1
```

The command-line option overrides this setting:

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --download-concurrency 2 browse nas:/volume1/share
```

`--jobs` and `--inflight` still control chunk concurrency and the S3 window **per file**. Increasing file concurrency multiplies those resource demands. A file holds its slot until the browser receives it, it is canceled, or its ready-to-save link expires after 10 minutes. This bounds the number of locally staged files. The queue holds at most 1,000 file records; active duplicates are ignored. Queue state survives page refreshes, but not a restart of the `browse` command.

The client stages each file in a private directory under the local operating system's temporary directory, using the **`.nass3cp-part`** suffix throughout preparation. Only verified files can be handed to the browser with their original filename (characters unsupported on Windows are sanitized). The browser manages its own incomplete downloads. Staged files are deleted after sending, cancellation, failure, expiry, or normal service shutdown. The local disk needs room for staging and the browser's saved copy; force-killing the process may leave a `nass3cp-browser-download-*` temporary directory. No file contents are buffered in a browser JavaScript Blob. Downloading uses S3; listing and searching remain metadata-only operations.

### Search for files

The web search box recursively searches regular filenames in the current directory and its descendants, including Unicode characters such as Chinese. It does not read file contents or follow symlinks or Windows junctions. The default match is a case-insensitive plain-text substring. Select **Regex** for Python regular expressions or **Case sensitive** for case-sensitive matching. Examples:

- Plain text `report`: matches filenames containing "report".
- Regex `report.*\.pdf$`: matches filenames containing "report" and ending in `.pdf`.
- Regex `\.(jpg|png|mp4)$`: finds files with the listed extensions.

Regular expressions match individual filenames, not full paths. Use `^` and `$` to anchor the entire name. Results arrive in batches with relative path, size, and dates. **Open folder** opens the containing directory; **Cancel** stops the job. Refreshing the same page restores search progress without rescanning. Both the local client and NAS server must support search.

Add or adjust the following top-level configuration in the NAS `config/server.json`, then restart the service. Older configurations that omit it use these defaults:

```json
"search": {
  "entries_per_second": 50,
  "max_results": 1000,
  "regex_timeout_ms": 100,
  "batch_size": 200,
  "max_pending_dirs": 2048,
  "exclude_dirs": []
}
```

| Setting | Default | Meaning |
|---|---:|---|
| `entries_per_second` | 50 | Maximum directory entries processed per second, from 1 to 10000. Opening a directory and finishing iteration also consume a pacing interval; unused capacity does not accumulate into bursts. |
| `max_results` | 1000 | Stop at this result count and display **Result limit reached**, from 1 to 10000. Narrow the directory or query to continue searching. |
| `regex_timeout_ms` | 100 | Wall-clock limit for one regex compilation or match, from 1 to 5000 milliseconds. A timeout stops the search and preserves results already found. |
| `batch_size` | 200 | Maximum entries processed in one directory per turn, from 1 to 10000, before other active directories get a turn. At the default scan rate, a batch takes about four seconds, or longer with slow I/O. |
| `max_pending_dirs` | 2048 | Maximum queued, unopened directories, from 1 to 16384. When full, traversal locally switches to depth-first scanning to bound memory without discarding directories. |
| `exclude_dirs` | `[]` | Optional list of subdirectory names, such as `["node_modules", ".git"]`. Names are case-sensitive and support Unicode; they are not paths or regexes. Up to 256 names are allowed. Nothing is excluded by default, and the explicitly selected starting directory is never excluded. |

Traversal favors shallow directories from the queue and rotates batches among at most 16 open directories. Paused directories retain their iterator position and resume without rescanning. This covers different directories early instead of finishing a deep subtree first. It is not strict breadth-first traversal: parent and child scans can interleave, with local depth-first traversal under queue pressure. Local depth-first traversal still has a 128-level depth limit, and the total number of open directory handles stays at or below 144. No on-disk job queue or additional scanning threads are created. Larger batches reduce directory switching; smaller batches improve responsiveness. The global scan rate limit remains active during rotation and depth-first traversal.

Only one search can run across the NAS at a time; additional requests receive a busy response. Scanning and regex matching run in a separate process, with a best-effort reduction in CPU scheduling priority on Unix. The scanner does not count the entire tree in advance and reads file metadata only after a filename matches. Open directory handles are bounded. Unreadable entries, excessive depth, symlinks, and other non-regular entries are skipped and counted. A search is canceled after 60 seconds without a progress request. At most four finished jobs are retained for up to ten minutes, and jobs do not survive a NAS restart. Stopping the local `browse` command or NAS service also cancels the associated running jobs.

The compact search display shows scanned entries, completed/discovered directories, matches, elapsed time, scan rate limit, and an estimated percentage. Expand **Search details** for the current directory, queue status, and estimation notes. Configured exclusion names and counts remain visible. The percentage estimates remaining work from the average entries in completed directories and entries already scanned in unfinished ones. It can decrease when new directories are discovered and is only a rough guide because no total is counted in advance. With too few samples, the UI shows an ongoing scan instead of a percentage. It reaches 100% only after traversal finishes; a result limit, cancellation, or failure never indicates that the whole tree was scanned.

The default of 50 entries per second is a conservative starting point, not a benchmark for a particular NAS or a hard limit on disk IOPS or CPU usage. Reduce it to 10–20 when mechanical disks are busy, then increase it gradually after confirming spare capacity. Useful tuning information includes CPU model, memory, disk type, and system load during a search; NAS login details are not needed.

### Copy directories recursively

`-r` (or `--recursive`) scans the complete source and destination directories, then merges the source contents into the specified destination root. Upload from the local machine to the NAS:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 -r \
  ./photos nas:/volume1/share/photos
```

Download from the NAS:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 -r \
  nas:/volume1/share/photos ./photos
```

The destination is the merge root. These examples place paths relative to `./photos` directly inside the NAS `photos/` directory, without appending another source-directory name. A missing destination root is created, and empty directories are preserved. In recursive commands, `nas:` refers to the first entry in `allowed_roots`.

The destination is checked by relative path before transfer. Existing regular files with the same name are skipped even if their sizes or contents differ, so recursive mode cannot be combined with `--overwrite`. Structural conflicts, such as a source file matching a destination directory or a source directory matching a non-directory, are reported before any directories are created or files transferred. Each file is checked again before transfer, so same-name files created after the initial scan are also skipped. Source symlinks, devices, sockets, and other non-regular entries are not followed and count as skipped. Each file still uses a temporary file, SHA-256 verification, and atomic placement, but a directory copy is not a transaction. Files completed before a network or disk error remain in place; rerun `-r` to skip them and continue.

The default recursive policy is `--rpolicy=auto`. It uses case-insensitive extensions to identify files that often compress well, including text, source code, logs, CSV/JSON/XML, and SQL/SQLite files. These candidates are handled first and compressed into temporary gzip files. Compressed data is sent through S3 only when it is smaller than the original. The receiver verifies separate SHA-256 hashes for the compressed payload and restored content; the final filename, contents, and modification time are preserved. Images, video, audio, and existing archives are sent unchanged by default. Use `--rpolicy=raw` to disable this selection and relay all regular files unchanged:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 -r --rpolicy=raw \
  ./photos nas:/volume1/share/photos
```

`auto` creates a complete temporary gzip file on the sender first. Uploads therefore need sufficient space in the local temporary directory; downloads need sufficient space in the NAS `state_dir`. Normal completion, failure, and cancellation paths remove temporary files. Forcibly terminating the local process may leave a `nass3cp-*.gz` file in the operating system's temporary directory. Recursive copies require compatible client and NAS server versions.

Add `--dry` to generate a plan without creating directories, uploading or downloading files, or creating S3 objects. It still authenticates and reads NAS directories through the control API. The output includes the source file count, total original bytes of all regular files, skipped bytes at existing destinations, original bytes scheduled for transfer, automatic compression candidates, directories to create, non-regular entries, and path conflicts:

```bash
./bin/nass3cp --no-tls --host 10.10.10.2 -r --dry \
  ./photos nas:/volume1/share/photos
```

`--dry` is available only for recursive copies and requires `-r`. "Original size" is the exact sum of regular source-file sizes at scan time, not an estimate of compressed size or billable S3 storage.

Replace `10.10.10.2` with the NAS ZeroTier/OpenTier IP. For FRP, use the local endpoint provided by the encrypted tunnel. Password input is not echoed.

### Remember passwords on Windows

Add `--remember-password` on the first connection. The client verifies the password with the NAS before saving it in Windows Credential Manager for the current user:

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --remember-password ls nas:
```

Subsequent copies or `ls` commands for the same host, port, and connection security mode read the saved password automatically. An explicit `--password-file`, `NASS3CP_PASSWORD`, or legacy credential option takes precedence.

After changing the password, ignore the saved value and verify and save the replacement:

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 `
  --no-saved-password --remember-password ls nas:
```

Delete a saved password using the same security mode it was saved under:

```powershell
.\bin\nass3cp.cmd --no-tls --host 10.10.10.2 --forget-password
```

Verified HTTPS, `--insecure` HTTPS, and `--no-tls` use separate credential entries, preventing weaker connections from automatically reusing passwords saved under stronger modes. Windows credential protection helps prevent direct access by other system users, but cannot defend against malicious software already running as the current Windows user. On other platforms, or in scheduled tasks without an interactive user credential session, use `--password-file` or create `config/client.env` with `NASS3CP_PASSWORD`.

TLS mode remains available:

```bash
./bin/nass3cp --host nas.example --port 9443 --ca-file nas-ca.crt \
  ./movie.mkv nas:/volume1/share/movie.mkv
```

Exactly one path in a copy command must start with `nas:`. Relative NAS paths are resolved against the first entry in `allowed_roots`; absolute paths must also fall inside an allowed root. Single-file copies reject existing destinations unless `--overwrite` is explicitly supplied. Recursive copies always skip existing regular files with the same name. Both transfer modes limit concurrent client S3 requests with `--jobs`, which defaults to 2 and accepts 1–16. `ls` is restricted to the same allowed roots.

The default `--mode pipeline` uses a bounded chunk pipeline. `--inflight` limits the number of chunks present in S3 but not yet acknowledged by the receiver, with a default of 3 and a range of 1–128. The receiver verifies, writes, and acknowledges chunks strictly in index order. With 64 MiB chunks and `--inflight 3`, one normal transfer peaks at about 192 MiB of cloud storage rather than the full file size. Multiple copy processes each have their own window, so their usage adds up. `--mode parallel` independently schedules all unfinished chunks and retains completed chunks; `--jobs` is the actual concurrency limit and `--inflight` does not apply. Ordinary single-file transfers automatically fall back to the original whole-file relay protocol with older servers. Parallel resumption, recursive directories, and transparent compression require updated versions at both ends.

Copies display a live progress bar, transferred size, speed, and estimated remaining time by default. Pipeline stages show overall progress through S3. Use `--quiet` to disable progress output. Interactive terminals update in place; redirected output records progress on separate lines.

`--insecure` still encrypts control traffic but does not verify the NAS identity, leaving it vulnerable to a man-in-the-middle attack. Use it only for temporary troubleshooting.

## Cloudflare R2 cost for about 1 GiB

R2 Standard's monthly free tier includes 10 GB-month of storage, one million Class A operations, and ten million Class B operations, with free direct internet egress from R2. A 1 GiB copy with the default 64 MiB chunks uses about 16 PUT, 16 GET, and 16 free DELETE operations. Objects are deleted immediately after receiver acknowledgment, so storage usage depends on the pipeline window rather than total monthly transfer volume. If the account remains within the monthly free tier, the total cost is usually **$0**.

The free tier applies only to Standard. Do not use Infrequent Access for a temporary relay: it lacks this free tier and adds retrieval fees and a 30-day minimum storage duration. Prices can change; consult [Cloudflare R2 pricing](https://developers.cloudflare.com/r2/pricing/) for current rates.

## Alibaba Cloud cost for about 1 GB

The following estimate is dated 2026-09-12 and assumes mainland China public cloud, Standard locally redundant storage, pay-as-you-go billing, a normal public endpoint, and no resource packages or free allowances:

| Item | One 1 GB copy |
|---|---:|
| Upload to OSS | CNY 0 (free ingress) |
| Public download from OSS, 00:00–08:00 | About CNY 0.25 |
| Public download from OSS, 08:00–24:00 | About CNY 0.50 |
| Temporary storage for one hour | About CNY 0.000167 |
| 16 PUT + 16 GET + about 16 DELETE | About CNY 0.000048 even outside the free allowance |
| Total | About CNY 0.2502 off-peak; CNY 0.5002 during peak hours |

Uploads to and downloads from the NAS have similar costs: both involve free ingress into OSS followed by one public egress transfer. Retries incur additional egress traffic. Transfer-acceleration endpoints add acceleration fees, so they are not enabled in the example. Standard storage includes a monthly regional allowance of five million PUT and twenty million GET operations, so request fees are usually zero for small-scale use.

Prices can change; the Alibaba Cloud bill is authoritative. References: [OSS pricing](https://cn.aliyun.com/price/detail/oss), [traffic fees](https://help.aliyun.com/zh/oss/traffic-fees), [storage fees](https://help.aliyun.com/zh/oss/storage-fees), and [accessing OSS with AWS SDKs / S3 APIs](https://help.aliyun.com/zh/oss/developer-reference/use-aws-sdks-to-access-oss).

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Contributing

See [AGENT.md](AGENT.md) for the project layout, development commands, and maintenance guidelines. Keep the English and Simplified Chinese READMEs aligned when changing usage. Write all other documentation, code comments, and docstrings in English. Test data used to verify Unicode support may retain its original characters.
