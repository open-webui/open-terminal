import os

API_KEY = os.environ.get("OPEN_TERMINAL_API_KEY", "")
LOG_DIR = os.environ.get(
    "OPEN_TERMINAL_LOG_DIR",
    os.path.join(os.path.expanduser("~"), ".open-terminal", "logs"),
)

# Comma-separated mime type prefixes for binary files that read_file will return
# as raw binary responses (e.g. "image,audio" or "image/png,image/jpeg").
BINARY_FILE_MIME_PREFIXES = [
    p.strip()
    for p in os.environ.get("OPEN_TERMINAL_BINARY_MIME_PREFIXES", "image").split(",")
    if p.strip()
]
