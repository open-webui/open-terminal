import os

# open_terminal.main refuses to import without an API key.
os.environ.setdefault("OPEN_TERMINAL_API_KEY", "test-key")
