# Optional nathole transfers in the browser

nathole remains an independent project in the optional `third_party/nathole`
submodule. It supplies authenticated, encrypted TCP forwarding over a punched
UDP path. nass3cp owns the file API, NAS password authorization, device enrollment,
and browser upload/download queues.

## Enable the NAS service

Initialize the pinned source on the NAS and each browser client:

```bash
git submodule update --init third_party/nathole
```

Add this block to the existing NAS JSON configuration; keep the existing `tls`,
`auth`, `allowed_roots`, `state_dir`, `search`, and S3 settings:

```json
"nathole": {
  "enabled": true,
  "coordinator": {
    "host": "203.0.113.10",
    "port": 40000
  }
}
```

Replace the example IPv4 address with the existing coordinator's address. For
port P, it needs TCP P and UDP P, P+1, P+2. The coordinator exchanges candidates
and probes addresses; it does not relay file data or provide fallback when UDP
hole punching fails. A NAS without S3 credentials may omit the `s3` block when
nathole is explicitly enabled. Such a NAS offers only nathole in the browser;
S3 copy commands remain unavailable. Existing configured S3 behavior is unchanged.

An absent `nathole` block, an omitted `enabled` field, or `enabled: false` disables
the entire integration: no nathole subprocess, network connection, credential
directory, registration endpoint, or nathole-specific environment expansion.
Disabled installations do not need the submodule or OpenSSL. `enabled` must be a
JSON boolean. The shipped NAS configuration does not enable this feature.

On startup, nass3cp-server starts a supervised nathole daemon and a separate
loopback HTTP listener for the tunnel. It preserves the existing NAS listener
and TLS settings. On shutdown it closes the daemon's control pipe and stops its
workers. The independent nathole supervisor retries pairing timeouts and broken
connections. Its authenticated ready event identifies the current client port.

With an independently installed checkout, set `nathole.program` to its absolute
`nat4_tunnel.py` path. Relative paths resolve against the NAS JSON file. The
checkout must include `nat4_service.py`. A packaged nass3cp client may set
`NASS3CP_NATHOLE_PROGRAM` to the corresponding local script. The program never
downloads dependencies automatically.

## Choose a transfer method

Run the existing `browse` command against the NAS control endpoint. Choose **S3**
or **nathole** in the upload form or next to **Download selected**. The choice is
saved on each queued file and retained when retrying it. There is no transport
command-line switch. If nathole is disabled, the existing S3 UI is retained.

nathole uses nass3cp's whole-file `GET /v1/files?path=...` and
`PUT /v1/files?path=...` APIs through the tunnel. A single shared slot serializes
nathole uploads and downloads in a browser session; the NAS also admits only one
direct file transfer at a time. S3 concurrency settings do not change this limit.
No S3 object, chunk scheduling, pipeline mode, parallel mode, or file resumption
is used for direct transfers. Existing S3 transfers retain their current behavior.

Files use bounded buffers, progress reporting and SHA-256 verification. Uploads
are staged in the destination directory and published atomically without
overwriting existing files. This publication uses a hard link and therefore needs
a NAS filesystem supporting hard links. Downloads stay in the existing local
temporary cache until verified before the browser can save them. Passwords,
root restrictions, ordinary-file checks and maximum file sizes still apply.
Interrupted files fail and can be retried from the beginning. Tunnel reconnection
does not resume an interrupted TCP stream or replay an upload. As with any upload,
cancellation after the NAS has committed a file cannot undo that completed file.
Normal failures remove partial files; force-killing a NAS process can leave
`.nass3cp-direct-*.part` files in its upload destination.

## First device registration

The first nathole transfer automatically generates a dedicated pairing bundle on
the client and submits only its NAS half through the existing authenticated NAS
API. OpenSSL is needed on that client for generation, not on the NAS. The NAS
persists the credentials and reloads its peer registry without interrupting
unchanged devices. Each device has its own room, TLS credentials and UDP secret.
The private CA signing material is discarded after creating this dedicated pair;
runtime peers keep only the four required credential files.

Registration requires a trusted control channel **before** the new tunnel exists:

- Verified HTTPS works with the existing `--ca-file` option when needed.
- HTTP through an existing authenticated, encrypted tunnel uses the existing
  explicit `--no-tls` option. Loopback registration is accepted (for example, an
  SSH local port forward). For an encrypted overlay arriving from a non-loopback
  address, explicitly set `nathole.trusted_http_registration: true` on the NAS.
  This setting is a deployment assertion, not additional encryption; do not use
  it on an unprotected listener or network.
- `--insecure` HTTPS is not accepted for new registration. Neither a NAS password
  nor a private network address alone makes plaintext HTTP confidential.

The public coordinator is not a credential-registration channel. If the NAS
control endpoint is not already reachable through a trusted channel, bootstrap
registration needs such a channel first (or use previously paired credentials).
There is no unauthenticated enrollment listener. Clients never upload their own
TLS private key to the NAS; they transmit the separately generated NAS bundle.

Client credentials live under `%LOCALAPPDATA%\nass3cp\nathole` on Windows, or
`$XDG_STATE_HOME/nass3cp/nathole` (default `~/.local/state/nass3cp/nathole`) elsewhere.
Profiles are bound to the original NAS endpoint and persistent NAS identity,
not a temporary localhost tunnel port. Windows directories get a DACL restricted
to the current user and SYSTEM; Unix directories/files use 0700/0600. No NAS
password is included in these profiles. NAS peers live under
`<state_dir>/nathole/peers/<device-id>` and must be outside `allowed_roots`.
Registration retries reuse pending credentials after a lost response.
Only one browser session can own the same local NAS profile at a time; an OS file
lock prevents concurrent key generation and conflicting connections to its room.

Certificates generated by nathole expire after 365 days. Renewal is not automatic:
revoke the old device, remove its local profile while browse is stopped, and
register again. The authenticated trusted-channel endpoint
`POST /v1/nathole/peers/<device-id>/remove` with `{}` revokes a device and closes its
active worker. Peer directories identify the registered device IDs. Up to 32
device slots are supported, including the optional imported pair.

## Import an existing tunnel-keys pair

Keep NAS and client halves from the same generation. Add the NAS half to the
enabled block:

```json
"keys_dir": "/private/nathole/tunnel-keys/nas",
"room": "my-existing-pair"
```

Set `NASS3CP_NATHOLE_KEYS` in the client's environment (or the source launcher's
ignored `config/client.env`) to the corresponding `tunnel-keys/client` directory.
The client uses the imported room advertised by the NAS and skips new key
generation and registration. The NAS uses peer ID `nas`, and the integrated
client uses ID `client`. Existing key material remains compatible; stop manually
launched tunnels using the same room before using the managed service.

## Validation

`--check-config` validates enabled paths and imported credentials without starting
a child or connecting to a coordinator. Run the normal nass3cp test suite for
disabled behavior, direct file integrity, cancellation, queue selection and local
end-to-end registration plus actual tunnel transfers. The independent submodule
has its own service and transport tests. Local tests do not establish success
rates or throughput across real NATs; the existing transport rate cap defaults
to 512 KiB/s per direction.
