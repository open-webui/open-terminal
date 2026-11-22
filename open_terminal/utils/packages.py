"""System packages for Desktop and Chrome tabs (`open-terminal install-deps`).

These features need programs the Python package cannot bring. This installs the
set the Docker image ships through apt, on Debian and Ubuntu.
"""

import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import click

FEATURES = ("desktop", "browser")
APT = {
    # The default virtual desktop (XFCE over TigerVNC) and its sound.
    "desktop": (
        "tigervnc-standalone-server", "xfce4", "xfce4-terminal", "dbus-x11", "xauth",
        "xdotool", "xclip", "scrot", "x11-xserver-utils",
        "pulseaudio", "pulseaudio-utils", "ffmpeg",
    ),
    "browser": ("chromium", "chromium-sandbox"),
}
FONTS = ("fonts-noto-cjk", "fonts-noto-color-emoji")
# Ubuntu packages Chromium only as a snap, whose confinement cannot use the
# profile directory under ~/.local. Chrome's own package has no such limit.
CHROME_DEB = "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb"
DOCS = "https://github.com/open-webui/open-terminal/blob/main/docs/configuration.md#desktop"


def missing(packages):
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package} ${db:Status-Status}\n", *packages],
        capture_output=True, text=True,
    )
    present = {line.split()[0] for line in result.stdout.splitlines() if line.endswith(" installed")}
    return [name for name in packages if name not in present]


def ready(feature):
    """Whether the running server would offer *feature*, using its own checks."""
    if feature == "desktop":
        from open_terminal.desktop.router import available

        return available()
    from open_terminal.browser.chrome import find_browser

    return find_browser() is not None


def ubuntu():
    try:
        return platform.freedesktop_os_release().get("ID") == "ubuntu"
    except OSError:
        return False


def install(features, extra=(), *, dry_run=False, yes=False):
    """Install what *features* still lack, plus any *extra* apt packages. Returns the process exit code."""
    if sys.platform != "linux" or not shutil.which("apt-get"):
        raise click.ClickException(f"install-deps supports Debian and Ubuntu. For other systems see {DOCS}")
    for name in extra:
        # Names only: anything else could reach apt as an option, run as root.
        if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name):
            raise click.BadParameter(f"{name!r} is not a package name.", param_hint="--package")
    features, extra = list(dict.fromkeys(features)), list(dict.fromkeys(extra))
    packages, deb = missing(extra), None
    for feature in features:
        wanted = APT[feature]
        if feature == "browser":
            if ready("browser"):
                continue
            if ubuntu() and platform.machine() != "x86_64":
                click.echo(
                    "browser: Ubuntu has no Chromium package for this CPU. Install a Chromium "
                    "build and set OPEN_TERMINAL_BROWSER_EXECUTABLE.", err=True,
                )
                continue
            if ubuntu():
                deb, wanted = CHROME_DEB, ()
            wanted = (*wanted, *FONTS)
        packages += missing(wanted)
    if not packages and not deb:
        return report(features, extra)

    root = os.geteuid() == 0
    if not root and not shutil.which("sudo"):
        raise click.ClickException("Installing packages needs root. Run this command as root.")
    # Without a terminal, sudo reads the password from stdin; passwordless sudo reads nothing.
    sudo = [] if root else ["sudo"] if sys.stdin.isatty() else ["sudo", "-S", "-p", ""]

    def command(file=None):
        # One elevated shell, so sudo asks for the password once.
        script = (
            "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "--no-install-recommends --no-upgrade " + shlex.join(packages + ([file] if file else []))
        )
        return [*sudo, "sh", "-c", script]

    if deb:
        click.echo("Will download:\n  " + deb)
    click.echo("Will run:\n  " + shlex.join(command("./" + deb.rsplit("/", 1)[1] if deb else None)))
    if dry_run:
        return 0
    if not yes:
        if not sys.stdin.isatty():
            raise click.ClickException("Pass --yes to install without asking.")
        click.confirm("Continue?", abort=True)

    with tempfile.TemporaryDirectory(prefix="open-terminal-") as directory:
        file = None
        if deb:
            # apt reads the file as its own unprivileged user.
            os.chmod(directory, 0o755)
            file = os.path.join(directory, deb.rsplit("/", 1)[1])
            urllib.request.urlretrieve(deb, file)
        code = subprocess.run(command(file)).returncode
    if code:
        click.echo("Install failed. Run the command above yourself, then restart Open Terminal.", err=True)
        return code
    if features:
        click.echo("Restart Open Terminal to use the new features.")
    return report(features, extra)


def report(features, extra=()):
    ok = not missing(extra)
    for feature in features:
        if ready(feature):
            click.echo(f"{feature}: ready")
        else:
            ok = False
            click.echo(f"{feature}: not available. See {DOCS}", err=True)
    return 0 if ok else 1
