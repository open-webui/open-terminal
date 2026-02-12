# ⚡ Open Terminal

A lightweight API for running shell commands remotely — with real-time streaming and secure access.

## Getting Started

### Docker (recommended)

```bash
docker run -p 8000:8000 -e OPEN_TERMINAL_API_KEY=your-secret-key ghcr.io/open-webui/open-terminal
```

If no API key is provided, one is auto-generated and printed on startup.

### Build from Source

```bash
docker build -t open-terminal .
docker run -p 8000:8000 open-terminal
```

### Bare Metal (if you like to live dangerously)

```bash
pip install open-terminal
open-terminal run --host 0.0.0.0 --port 8000 --api-key your-secret-key
```

| Option | Default | Env Var | Description |
|---|---|---|---|
| `--host` | `0.0.0.0` | — | Bind address |
| `--port` | `8000` | — | Bind port |
| `--api-key` | auto-generated | `OPEN_TERMINAL_API_KEY` | Bearer API key |

## Usage

### Run a Command

```bash
curl -X POST http://localhost:8000/execute \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"command": "echo hello"}'
```

### Stream Output

```bash
curl -X POST "http://localhost:8000/execute?stream=true" \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -d '{"command": "for i in 1 2 3; do echo $i; sleep 1; done"}'
```

Output streams as JSONL:

```jsonl
{"type": "stdout", "data": "1\n"}
{"type": "stdout", "data": "2\n"}
{"type": "stdout", "data": "3\n"}
{"type": "exit", "data": 0}
```

### Upload a File

**From URL:**
```bash
curl -X POST "http://localhost:8000/files/upload?url=https://example.com/data.csv&path=/tmp/data.csv" \
  -H "Authorization: Bearer <api-key>"
```

**Direct upload:**
```bash
curl -X POST "http://localhost:8000/files/upload?path=/tmp/data.csv" \
  -H "Authorization: Bearer <api-key>" \
  -F "file=@local_file.csv"
```

**Via temporary link (no auth needed to upload):**
```bash
# 1. Generate an upload link
curl -X POST "http://localhost:8000/files/upload/link?path=/tmp/data.csv" \
  -H "Authorization: Bearer <api-key>"
# → {"url": "http://localhost:8000/files/upload/a1b2c3d4..."}

# 2. Upload to the link (no auth required)
curl -X POST "http://localhost:8000/files/upload/a1b2c3d4..." \
  -F "file=@local_file.csv"
```

### Download a File

```bash
curl "http://localhost:8000/files/download/link?path=/tmp/output.csv" \
  -H "Authorization: Bearer <api-key>"
```

Returns a temporary download link (valid for 5 minutes, no auth needed):

```json
{"url": "http://localhost:8000/files/download/a1b2c3d4..."}
```

## API Docs

Interactive API documentation is available at [http://localhost:8000/docs](http://localhost:8000/docs).

## License

MIT — see [LICENSE](LICENSE) for details.
