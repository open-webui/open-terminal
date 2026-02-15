import os

API_KEY = os.environ.get("OPEN_TERMINAL_API_KEY", "")
LOG_DIR = os.environ.get(
    "OPEN_TERMINAL_LOG_DIR",
    os.path.join(os.path.expanduser("~"), ".open-terminal", "logs"),
)
