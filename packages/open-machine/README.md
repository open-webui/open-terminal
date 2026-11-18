# Open Machine

Alias package for [Open Terminal](https://github.com/open-webui/open-terminal).
Installing it installs `open-terminal`; the `open-machine` command forwards all
arguments to the same CLI, including its existing help and version output.

```bash
uvx open-machine@latest run --host 127.0.0.1 --port 8000

# Or pip
pip install open-machine
open-machine run --host 127.0.0.1 --port 8000
```

To install from this checkout, run from the repository root:

```bash
pip install ./packages/open-machine
```

This package has its own version and is built and published separately from
`open-terminal`. To build it from the repository root:

```bash
uv build packages/open-machine --out-dir packages/open-machine/dist
```
