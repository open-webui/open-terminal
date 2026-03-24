import re

class CommandGuard:
    """
    Security guard for terminal commands executed by agents.
    Prevents execution of high-risk or destructive commands.
    """
    def __init__(self, blocked_patterns=None):
        self.blocked_patterns = blocked_patterns or [r"rm -rf /", r"mkfs", r"shutdown"]

    def is_safe(self, command: str) -> bool:
        for pattern in self.blocked_patterns:
            if re.search(pattern, command):
                print(f"Blocked unsafe command: {command}")
                return False
        return True
