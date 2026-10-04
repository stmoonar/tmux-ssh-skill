# Base64 Chunked Transfer Protocol

Contents: design constraints / upload sequence / download sequence / parameters and performance / failure semantics

## Design constraints

Transfers must use the authenticated shell already present in a tmux session. They cannot use `scp`, `rsync`, or another channel that requires a new login. Data therefore travels as text through a PTY (pseudoterminal), subject to these constraints:

- Canonical PTY input has a line limit of about 4 KiB. Wrap payload lines and keep control commands short.
- `capture-pane` exposes only limited scrollback. Large files would scroll out of view, so downloads cannot rely on it.
- PTYs echo input. On slow links, echo and paste can compete; complete a handshake before sending data.
- Terminals may insert `\r` and escape sequences. Accept only strictly valid Base64 lines when parsing.

## Upload sequence

Each chunk completes a full independent round trip:

1. Read a local chunk, Base64-encode it, wrap it at 76 columns, and append a unique end marker.
2. Load it into a tmux buffer with `load-buffer`.
3. Send a remote read loop that disables echo with `stty -echo`, prints a `READY` marker, and reads lines until the end marker.
4. Call `paste-buffer` locally **only after observing `READY`**. Pasting earlier can lose the start of the payload.
5. Decode remotely, append to the staging file, restore `stty echo`, and print a receipt containing `rc`.

After all chunks arrive, perform final verification remotely:

1. Compare the wire payload's SHA256 and byte count. Decompress if compression was used.
2. Compare the original content's SHA256 and byte count after decompression.
3. Use `replace` to publish the final path only after both checks pass.

The transfer therefore publishes only a verified complete file at the final path.

## Download sequence

1. Check for an existing output pipe. If one exists, refuse the download and preserve the existing log. Acquire a dedicated pipe with `pipe-pane -o` and a local handshake before triggering remote output.
2. Slice the remote file by byte range, compute the range's SHA256 in Python, and emit a start marker, Base64 payload, and end marker.
3. Parse the local capture file, accepting only Base64 lines with valid lengths and characters.
4. Verify each range independently before appending it to a local staging file.
5. After all ranges arrive, verify the whole-file SHA256 and byte count, then rename to the destination path.

Slicing, hashing, and encoding all run in remote Python. Avoid `dd`, `stat -c`, and `sha256sum` because their options differ between BSD and GNU environments.

## Parameters and performance

| Parameter | Default | Meaning |
|---|---|---|
| `--chunk-bytes` | 3 MiB | Raw bytes per chunk |
| `--compress` | Off | gzip before upload; automatic remote decompression |
| `--timeout` | 600 s | Maximum wait for an individual remote operation |
| `--max-parallel` | 4 | Sessions handled concurrently |

Base64 adds about 33% overhead, and PTY line-by-line processing reduces throughput compared with native file channels. Choose accordingly:

- Package directories and many small files into a single archive to reduce round trips.
- Compress text content to reduce the bytes sent over the channel.
- Do not recompress already compressed archives, images, or model weights.
- Each result includes `throughput_mib_s` and `elapsed_seconds`; tune using measurements.
- Run different sessions in parallel and operations within a session sequentially. One PTY cannot safely carry interleaved operations.

## Failure semantics

| Symptom | Meaning | Action |
|---|---|---|
| `remote reader not ready` | The remote shell did not enter its read loop | Check for an interactive program or an exited shell |
| Nonzero chunk `rc` | Remote decoding failed | The protocol sends the end marker and restores echo automatically; then retransmit the file |
| `wire mismatch` | Wire bytes differ from the local source | The link is corrupted; the staging file is deleted; retransmit |
| `payload mismatch` | Decompressed content differs | The staging file is deleted and the final path remains untouched |
| `range verification failed` | A download range is corrupted | Local staging is cleaned up; download again |
| `pane already has an output pipe` | The pane is already logging terminal output | Preserve the pipe and use a pane without an output pipe |
| `TIMEOUT` | No receipt appeared; the operation may still be running | Inspect actual remote state before deciding what to do; do not retry immediately |

Failures never publish a partial file at the final path. Uploads use `.part-<nonce>` staging followed by an atomic rename; downloads stage locally in `.part-` files before renaming.

After a timeout or terminated driver, the session's unfinished-operation marker blocks further writes. Inspect with read-only `capture-pane`, confirm the shell is idle, and follow the `--recover-session` procedure in `SKILL.md`. Download temporary files and handles are released on both success and exception paths.
