# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.17.3] - 2026-11-07

### Added

- Added support for Tether to run Open Terminal without opening a directly accessible HTTP address. Your connected Open WebUI can still use the terminal through Tether. Direct HTTP access can be enabled for just your computer or for other computers on your network, and still requires an API key. Requires an updated Tether.
- Other desktop apps can use the same connection mode through `--ipc`. See the [local connection setup](README.md#private-local-ipc).

### Changed

- The Linux Desktop now uses the Open WebUI homepage image as its default wallpaper in the full and agents Docker images. The image is bundled with Open Terminal. Update or rebuild your Docker image to get it.

### Fixed

- Fixed Docker's allowed-site restrictions being bypassed through IPv6 connections. When allowed-site restrictions or the block-all setting are enabled, new outgoing IPv6 connections are now blocked, including connections to allowed sites. Allowed sites remain reachable over IPv4.

## [0.17.2] - 2026-11-06

### Fixed

- Fixed Discard in the Git panel leaving changes that were already marked for the next commit. Discard now returns selected existing files to their last committed version, including restoring deleted files and undoing renames. Changes to other files are kept. An updated Open WebUI is required to show Discard for every changed file.
- Discard can now delete selected new files, whether or not they were marked for the next commit, including in projects with no commits yet. This permanently removes those files. Previously, Discard could not remove new files.
- Fixed Discard leaving a file missing when its deletion or rename was marked for commit and another file was created with the original name. The last committed version is now restored.
- Discard now reports a clear error when a selected folder contains its own Git repository, before changing any selected files. Previously, it reported success while leaving that folder on disk.

## [0.17.1] - 2026-11-06

### Added

- Added support for showing Codex's reasoning effort and service tier options in Open WebUI's model controls, so you can choose how much effort it spends thinking and which service tier to use. Available choices and defaults come from your installed Codex version; options it does not report stay hidden. Requires an updated Open WebUI.

### Fixed

- Fixed repeated pauses before agent replies when checking which models are available. Previously, sending a Codex message could wait for another agent's slow startup or login check after the model list expired. Known models now remain available while the list refreshes in the background. With an updated Open WebUI, sending a message checks only the selected agent, so other agents cannot hold it up even on the first check.

## [0.17.0] - 2026-11-06

### Added

- Added support for chatting with Codex, Claude Code, Cursor, Grok, OpenCode, Cline, Gemini CLI, and Pi through Open WebUI. Select your terminal in chat to see the models offered by its installed agents in the model selector. Requires an Open WebUI version with agent support. Install and sign in to each agent from the terminal first; the `agents` Docker image already includes Codex and Claude Code.
- Agent chats work in the terminal's current folder. You can attach files, watch replies and tool activity as they happen, answer the agent's approval requests, stop a running reply, and continue the conversation with a follow-up message. Available approval prompts depend on the agent. Branching or regenerating an agent conversation requires a new chat.
- Agent connections can recover from a brief interruption, catch up on unread output, and avoid sending the same input twice. Disconnecting does not immediately stop the agent; sessions left disconnected for more than an hour are closed during cleanup.
- Added support for running local agents through Tether's saved Open WebUI connections. Your hosted Open WebUI can reach the agents on your computer through Tether without a public address for your computer. Requires Tether and Open WebUI versions with agent support; agent logins stay in the terminal environment.

### Changed

- Shortened the README around installation and connecting to Open WebUI. Moved detailed instructions for settings, Docker images, agent login, storage, multi-user setup, and temporary public access into linked guides, keeping the existing command examples.

## [0.16.7] - 2026-11-06

### Added

- Added an `agents` Docker image with the latest Codex and Claude Code releases installed at build time alongside the full Open Terminal toolkit, with authentication configured per user at runtime. Includes AMD64 and ARM64 publishing and versioned `-agents` tags.

## [0.16.6] - 2026-11-05

### Fixed

- Fixed the Browser tab stopping its live updates after opening a page or following a link in Chrome. The page view now resumes automatically once the new page loads, including when it finishes loading before the Browser panel is ready.

## [0.16.5] - 2026-11-05

### Changed

- Reduced pauses between screen updates in the Browser tab and when sharing a Mac or Windows desktop over connections with noticeable delay, helping scrolling and animations stay smoother. Requires an updated Open WebUI. Browser views using the built-in Linux desktop keep their existing streaming behavior.

### Fixed

- Commands that print large amounts of text now slow down when the connected terminal cannot display it fast enough, preventing unread output from being discarded while the viewer catches up. Requires an updated Open WebUI.
- Fixed terminal output getting stuck when a large paste was waiting for the running program to accept it. Output can now continue while the paste waits.

## [0.16.4] - 2026-11-05

### Fixed

- Fixed pages using your running Chrome reverting to the Chrome window's size instead of the size selected in the Browser tab. Opening or resizing the tab, switching mobile preview, and changing display quality now keep the requested page size. Update Open WebUI as well for the related fix for pages appearing too small with empty space around them.
- Reconnecting to a Browser tab now waits for its current size before showing the page and discards images left over from the previous connection, preventing a brief view of the page at the old size.

## [0.16.3] - 2026-11-05

### Added

- The Browser tab can now open pages in your running Chrome using your existing website logins. Set `OPEN_TERMINAL_BROWSER_MODE=personal`, enable debugging in Chrome at `chrome://inspect/#remote-debugging`, restart Open Terminal, and approve the connection when Chrome asks. This requires single-user mode and works with Open WebUI's existing Chrome controls, without a WebUI update. See [setup instructions](docs/configuration.md#use-your-running-chrome).
- When using your Chrome, Open Terminal opens its own tabs. Closing a Browser session or stopping Open Terminal closes those tabs while leaving Chrome and your other tabs open. If it cannot connect to the Chrome instance you selected, it reports the problem instead of opening a different browser.

### Fixed

- After Chrome closes, a tab closes, or the browser connection drops, attempts to control that tab now report that it disconnected and ask you to reopen it.

## [0.16.2] - 2026-11-05

### Added

- Added `open-terminal run --isolated` and `OPEN_TERMINAL_ISOLATED=true` to start without loading saved system or user settings. Environment settings and a file explicitly selected with `--config` still apply.

### Changed

- Notebook support is now an optional install. Use `pip install 'open-terminal[notebooks]'` to include it. Open Terminal can start without the notebook packages, with notebook features disabled. The default Docker image still includes them.

### Fixed

- Terminal connections can now drop and reconnect without immediately stopping the shell or its running programs. Output continues to be collected while disconnected, with up to 1 MiB kept for reconnection. Connected apps can resume from where they left off; terminals left disconnected for more than five minutes are closed during the next cleanup.
- Failed uploads no longer overwrite an existing file with incomplete content. The original file is replaced only after the upload has been fully written.

## [0.16.1] - 2026-11-04

### Fixed

- Fixed Open Terminal becoming unresponsive when opening a large folder outside a Git repository, such as `/`. Watching that folder for changes no longer scans every subfolder.
- Added the missing font for emoji and country flags to display correctly in the Browser tab when using the default Docker image.

## [0.16.0] - 2026-11-02

### Added

- You can now hear videos, music, and other app sounds from the Linux Desktop tab. Audio is compressed to keep data use low, and the default Docker image includes the required sound tools. Requires an updated Open WebUI. Sound sharing is not yet available for native Mac or Windows desktops.

### Changed

- Shortened the Browser and Desktop assistant instructions to favor completing known steps together, such as typing and pressing Enter, and checking the result before deciding what to do next.

### Fixed

- Fixed assistants missing a terminal opened before the first chat message. The existing terminal now joins the chat when you send that message, keeping its running programs, output, and settings without restarting the shell. Requires an updated Open WebUI.

## [0.15.5] - 2026-11-02

### Changed

- Docker images now default to `OPEN_TERMINAL_BROWSER_NO_SANDBOX=true` so Chromium can start under Docker's default security restrictions. Chromium's sandbox remains enabled by default outside Docker. Container isolation is unchanged, but Chromium's protection for files and processes inside the container is disabled. Deployments that support Chromium's sandbox can restore it with `OPEN_TERMINAL_BROWSER_NO_SANDBOX=false`.

## [0.15.4] - 2026-11-02

### Changed

- Desktop and Browser now require an actions list for every request, even for a single step. Their tool instructions tell assistants to combine steps such as typing a command and pressing Enter in one call, with one result after the last step. Clients using the old single-action format must update, and cached tool definitions must be refreshed.

## [0.15.3] - 2026-11-02

### Added

- Assistants can now click a desktop field, enter text, and press Enter in one sequence, receiving a screenshot after the last step instead of after each action.
- Assistants can now fill several fields in the same Browser tab in one sequence, receiving the updated page contents after the last step. Connected apps must support sending actions together to use these Browser and Desktop improvements.
- Browser and Desktop sequences stop when a step fails and report how many earlier steps completed. Completed steps are not undone, and assistants can still perform one action at a time.

## [0.15.2] - 2026-11-02

### Added

- Added `OPEN_TERMINAL_BROWSER_NO_SANDBOX=true` for deployments where Chromium cannot start with its sandbox enabled. The same setting applies to Browser tabs and Chromium opened from the Linux Desktop in the Docker image. Restart or redeploy Open Terminal after changing it. This setting turns off Chromium's built-in sandbox protection.

### Fixed

- Chrome startup failures on Linux and Mac now show the actual error, such as a locked browser profile or an unavailable sandbox, instead of the generic message asking you to check the installation, user, and sandbox support. The error is also saved in the server logs.

### Security

- Chromium's sandbox now stays enabled by default in Docker. Previously, Open Terminal automatically disabled it when it detected Docker. Existing deployments that cannot run sandboxed Chromium must now explicitly set `OPEN_TERMINAL_BROWSER_NO_SANDBOX=true` to keep launching the browser.

## [0.15.1] - 2026-11-02

### Added

- The Desktop tab can now show and control your Mac or Windows computer when Open Terminal runs directly on it. You and the assistant can use the mouse, type, paste text, and interact with your open applications. It shares the logged-in user's primary screen across workspaces. On Mac, grant Screen Recording and Accessibility permissions to the app running Open Terminal. Requires an updated Open WebUI.
- Chrome mode in the Browser tab now works on Windows, with automatic detection of installed Chrome, Edge, and Chromium browsers.
- Browser and Desktop can now be turned off separately with `OPEN_TERMINAL_ENABLE_BROWSER=false` and `OPEN_TERMINAL_ENABLE_DESKTOP=false`. Both remain on by default. Restart Open Terminal after changing either setting.

### Changed

- Browser views on Mac, Windows, and Linux installations without the desktop packages now use less data by skipping unchanged parts of the page and dropping outdated screen updates. Requires an updated Open WebUI.

### Fixed

- Assistants now have controls for your open Browser tabs, addressing requests that previously sent them to Desktop instead. They can see the page, click, scroll, type, and navigate in the existing tab with its current login. They use the selected browser tab, or the last-used tab when the pane is hidden. The tab must be in Chrome mode. Requires an updated Open WebUI.
- Fixed browser lag after rapid mouse movement and scrolling on Mac, Windows, and Linux installations without the desktop packages. These actions no longer build up a queue that delays typing or navigation.

## [0.15.0] - 2026-11-02

### Added

- Added support for Open WebUI's Browser tab to open websites and apps running inside Open Terminal. Choose Chrome for a full browser or Proxy for a lightweight page preview, with back, forward, reload, and page titles. Requires an updated Open WebUI.
- Chrome browser tabs support typing, clicking, scrolling, pasting, and mobile page previews. Switching tabs keeps the other pages open. Each user's saved browser profile is reused across tabs and restarts, with automatic recovery from locks left by an interrupted browser. Chrome mode requires Chrome or Chromium on Linux or macOS; Chromium is now included in the default Docker image.
- Local web app previews now support automatic page updates after you edit their files, along with loading their styles, images, and scripts through Open Terminal. This works with both direct terminal connections and connections through Open WebUI.
- Added a Desktop tab for opening a Linux desktop in the terminal workspace. You can use graphical apps, including LibreOffice, and reopen the tab without closing running applications. The desktop and its required tools are included in the default Docker image. Requires an updated Open WebUI.
- Assistants can see screenshots of that same desktop and click, drag, scroll, type text, and press keyboard shortcuts. You and the assistant work with the same open applications and workspace files.
- Connected apps can now receive file and Git changes as they happen, including edits made from a terminal or another app. They can also update the list of available local web apps when a server starts or stops.
- Connected apps can show background command output as it arrives and update the command list when commands start or finish. Closing the output viewer leaves the command running.
- Added support for opening a repository's website from Open WebUI's Git panel and hiding local branch rename and delete actions for remote branches. Requires an updated Open WebUI.

### Fixed

- Added Chinese, Japanese, and Korean fonts to the default Docker image to address blank characters when previewing PDF and Word documents.
- Git lookup failures now report the error instead of making an existing repository appear to be an ordinary folder.
- Background commands now publish buffered output even when they stop producing text, and report completion only after their final output has been saved.

## [0.14.8] - 2026-11-01

### Added

- Added support for Open WebUI's Git panel when browsing a Git repository, including its subfolders. The panel can show the current branch, changed files, added and removed lines, and commit history. Requires an updated Open WebUI.
- From the Git panel, you can select files to commit, review or discard edits, undo the last commit while keeping your files, switch or manage branches, and create separate working folders for other branches. You can also set changes aside and restore them later, download remote changes, and upload your commits.
- Added GitHub pull request controls for viewing changes, comments, reviews, and checks; creating and editing pull requests; leaving comments and reviews; and merging, closing, or reopening them. These controls require GitHub CLI to be installed and signed in on the machine running Open Terminal.

### Fixed

- When several terminal tabs are open in the same chat, the assistant now reads output and sends input to the active terminal. When no terminal is visible, it uses the last-used terminal that is still connected. Previously, it always used the newest terminal, even after you switched to another tab. Requires an updated Open WebUI.

## [0.14.7] - 2026-10-31

### Added

- File listings now include file and folder creation dates, enabling "Date Added" sorting with an updated Open WebUI. Editing a file does not change its creation date. This includes support in the Alpine Docker image; dates remain unavailable on filesystems that do not provide them.

### Fixed

- Fixed the missing OpenShift Docker image in GitHub Container Registry. Automatic builds and publishing now include `ghcr.io/open-webui/open-terminal:openshift` and versioned OpenShift images for both Intel/AMD and ARM systems. Previously, the OpenShift image was documented but left out of automatic publishing.

## [0.14.6] - 2026-10-29

### Fixed

- Updated the default assistant instructions to present created or edited files through Open WebUI's file viewer before finishing a reply. This addresses replies that showed labels such as "Download PDF" and "Editable Word version" without working download links. Custom assistant instructions must be updated separately to include this guidance.

## [0.14.5] - 2026-10-25

### Added

- `open-terminal run --cftunnel` publishes a temporary Cloudflare HTTPS endpoint with a required configured API key. Reuses installed cloudflared or offers a pinned, checksum-verified download; `--yes` approves unattended downloads. The API and connector shut down together.

### Fixed

- CLI launches using `OPEN_TERMINAL_API_KEY_FILE` no longer leave conflicting API-key environment settings. Explicit `--api-key` overrides environment sources.
- Malformed REST and terminal WebSocket credentials are rejected without authentication-handler exceptions.

## [0.14.4] - 2026-10-24

### Added

- Optional HTTP keep-alive timeout configuration through `OPEN_TERMINAL_UVICORN_TIMEOUT_KEEP_ALIVE` or `uvicorn_timeout_keep_alive` in TOML. Unset by default, preserving Uvicorn's default behavior.

## [0.14.3] - 2026-10-24

### Fixed

- Fixed Docker startup when outbound network restrictions are enabled. Allowed websites can now be reached, and startup preserves the configured API key, settings, and command arguments. Applies to the default, slim, and Alpine images.
- Blocked domain names can no longer be looked up by contacting Docker's name lookup service directly, including when all outbound access is disabled.
- Docker startup now stops if the requested network restrictions cannot be applied, instead of continuing without those restrictions.
- Commands, interactive terminals, and notebooks no longer automatically receive Open Terminal's API key and server settings. This reduces accidental exposure when programs print their environment settings. Other environment settings and values explicitly supplied for a command are preserved.
- In multi-user mode, changing folders no longer changes another user's current folder when both users share the same session ID. File operations and commands keep using each user's own selected folder.
- Changing to a missing folder or a file in multi-user mode now returns an error and keeps the previous folder selected.
- Connected apps must now identify the user when accessing a workspace in multi-user mode. Requests without a user ID are rejected instead of using the server account's workspace. Single-user mode still works without a user ID.
- Notebooks now run under the requesting user's account in multi-user mode, with that user's home folder and file permissions. Opening and saving notebooks also follows the same workspace checks as other file operations.
- Notebook sessions now belong to the user who started them. Other users cannot run cells, view session details, or stop those sessions through the notebook controls.

## [0.14.2] - 2026-10-19

### Fixed

- Reduced memory use when reading and editing large text files. Reading just a few lines no longer loads every line into memory, which could previously crash the server even for a small response.
- Reduced memory use when reading text from documents, uploading and viewing files, downloading folders as ZIP files, and previewing Word and PowerPoint files as PDFs. Temporary files are removed after sending or cancellation.
- Reduced memory use when searching large folders or files with many matches, including when loading later pages of results. Matching lines are still returned in full.
- Reading command output and removing old output from oversized logs no longer loads the entire log into memory. If saving output fails, such as when the disk is full, commands can still finish and release their resources instead of remaining marked as running and retaining memory.
- Apps opened through forwarded ports now deliver live responses as they arrive. Previously, Open Terminal kept collecting the response in memory until it ended, so continuous updates could fail to appear and keep increasing memory use.
- Fixed file operations and port checks getting stuck when several commands or terminals were open without producing output on Linux and macOS.
- Fixed notebook sessions continuing to consume memory after startup failed or execution was cancelled. Shutting down Open Terminal now also stops its commands, terminals, and notebook sessions.
- Fixed closed terminals leaving connections or programs running in the background, and large inputs to background commands being cut short or mixed together when sent at the same time.
- Old command logs are now deleted after the configured retention period even when the finished command has already disappeared from the command list. Previously, those logs could remain on disk indefinitely.

## [0.14.1] - 2026-10-16

### Added

- Command results now include the folder the command ran in, so assistants can see where each command started without running a separate command to check.

### Fixed

- Fixed file browsing in Open WebUI when Open Terminal runs directly on Windows. Opening a folder previously failed with a server error while checking whether its files could be edited.

## [0.14.0] - 2026-09-23

### Fixed

- Apps connecting to the MCP server over the network must now provide your Open Terminal API key. Previously, they could run commands and access files without providing the key. Connections with a missing or incorrect key are now rejected. If you had the MCP server enabled for network connections, update to the latest release.

## [0.13.0] - 2026-09-13

### Added

- Assistants in Open WebUI can now use skills installed in your terminal's home folder. Requires Open WebUI 0.11.4.
- Skills are loaded from the `.agents/skills`, `.cptr/skills`, `.claude/skills`, and `.codex/skills` folders. Skills with the same name appear only once.
- Assistants can read each skill's instructions and see a list of its supporting files, including scripts and reference documents.

## [0.12.5] - 2026-09-08

### Added

- Connected apps can now compare two files without changing either original. Results include added and removed lines, line numbers, nearby text, and highlights of changed words.
- File comparison supports plain text, PDF, Word, Excel, PowerPoint, OpenDocument, RTF, EPUB books, and saved email messages. It compares readable text and cell values, with notices explaining which content is left out, such as formatting, images, formulas, and attachments.
- Comparisons can ignore differences in spaces and tabs. Relative file names are resolved from the current terminal session's folder.
- Comparisons report encrypted PDFs, PDF pages with no readable text, and EPUB chapters that cannot be read. Files over 50 MiB or text over 2 million characters or 50,000 lines are rejected instead of returning a partial comparison. Comparisons stop after 60 seconds or when the connection closes.

### Fixed

- Reading Word files now keeps paragraphs and tables in their original order instead of moving all tables to the end.
- Reading Word files now includes tracked insertions and excludes tracked deletions, including deleted table rows and cells. Moved text appears in its new location, and deleting a paragraph break joins the surrounding text. The original file is unchanged.

### Changed

- Added automatic publishing to PyPI when a new version reaches the main branch. Versions already available on PyPI are skipped.

## [0.12.4] - 2026-09-06

### Added

- Assistants can now read recent output and send text to your connected terminal in the current chat, including answering prompts from a running program. Requires Open WebUI 0.11.4.

### Fixed

- Terminal and background command lists now show only entries belonging to the current user and chat. Other users and chats cannot read their output, send them input, or stop them through these controls.
- Opening another terminal in the same chat no longer replaces the first terminal's saved entry.
- A second connection to an already connected terminal is now rejected, preventing the two connections from splitting its output.
- Sending a large amount of text to a busy terminal no longer drops the unwritten portion. Text sent at the same time is handled in order without mixing the requests together.
- Stopped background commands remain available for five minutes so you can check their output and exit code. They stay marked as killed instead of changing to done.
- Checking for new command output after reading only the latest lines no longer repeats older output. Checks also keep their place when an oversized log has its oldest output removed, and report when output was discarded.
- Reading command output while an oversized log is being shortened no longer returns a partially rewritten log. Commands are marked done only after their final output has been saved, including when the log was shortened during the run.

## [0.12.3] - 2026-08-27

### Added

- Word and PowerPoint files can now open as PDF previews from the file viewer when preview mode is requested. If a preview cannot be made, Open Terminal still returns the original file.
- The default Docker image now includes the PDF tools needed for previews and PDF file work, so users do not have to install them separately.
- The README now explains how to use external workspace storage and how Office file previews behave.

## [0.12.2] - 2026-08-25

### Fixed

- Opening a forwarded port now only works for ports that belong to your own visible terminal session. Requests for other local ports are blocked with "Port not found" instead of being forwarded.

## [0.12.1] - 2026-08-22

### Fixed

- Added headless LibreOffice and Office-compatible fonts to the default Docker image so agents can convert Word, Excel, and PowerPoint files to PDF without installing extra system packages. The slim, alpine, and openshift images remain minimal.

## [0.12.0] - 2026-08-22

### Added

- Apps can now search terminal workspace files by file name and file contents in one file browser search.
- Search results show file name matches first, followed by files that match inside their contents.
- Content results include matching line previews so users can jump to the relevant place in a file.
- File browser search follows the current terminal session folder, so users search the same workspace their terminal is using.
- File browser search respects `.gitignore` and the app's hidden-file setting.
- Large search result sets can now load more results without restarting the search.

## [0.11.36] - 2026-08-22

### Added

- Added file search for apps that need to let users pick files from the active terminal workspace.
- File search now follows the current terminal session folder, so results come from the same place the terminal is working in.
- File search respects `.gitignore`, so ignored folders and files do not appear in picker results.
- File search can include hidden dotfiles when the connected app asks to show them.

### Fixed

- Every file search result now includes the file name, full path, whether it is a file or folder, file size, and last modified time.

## [0.11.35] - 2026-08-09

### Added

- Added an OpenShift image for restricted non-root clusters. It runs without root, blocks unsupported runtime package installs, and clearly reports when multi-user mode is requested.
- Added file browser root metadata so clients can show a friendly starting point and hide parent navigation above it.
- Added `OPEN_TERMINAL_FILE_BROWSER_ROOT` so operators can choose Home, a specific workspace path, a path under Home, or no visual root.
- Added writable status to file and folder listings so clients can disable save, upload, rename, and delete actions before users hit a permission error.
- Added template variables for custom system prompts, including operating system, host name, user, shell, Python version, and Home path.

### Fixed

- Fixed the MCP server connection when Open Terminal is protected by an API key. MCP requests now include the configured bearer token.

## [0.11.34] - 2026-04-08

### Added

- 🌐 **Path-based file serving** — new `GET /files/serve/{path}` endpoint enables HTML files loaded in iframes to resolve relative CSS, JS, and image references to sibling files. Used by Open WebUI's FileNav to render multi-file websites.

## [0.11.33] - 2026-04-08

### Fixed

- `grep_search` now defaults to regex mode, matching actual grep behaviour. Patterns like `foo|bar` and `err.*timeout` work as expected without needing to set `regex=true` explicitly.

## [0.11.32] - 2026-03-30

### Changed

- 🗂️ **Session-aware relative path resolution** — all LLM-exposed file endpoints (`list_files`, `read_file`, `write_file`, `display_file`, `replace_file_content`, `grep_search`, `glob_search`) now read the `X-Session-Id` header and resolve relative paths (e.g. `.`) against the session's working directory instead of always defaulting to `fs.home`. Added an optional `cwd` parameter to `UserFS.resolve_path()` to support this. Absolute paths are unaffected.

## [0.11.31] - 2026-03-30

### Changed

- 🗂️ **Per-session working directory** — replaced the process-global `os.chdir()` with an in-memory, session-aware dictionary keyed by `X-Session-Id` header. Multiple concurrent chat sessions now maintain independent working directories. `GET/POST /files/cwd`, `POST /execute`, and `POST /api/terminals` all read the header to resolve the correct cwd. Sessions without a header fall back to `fs.home`. Entries expire after 7 days of inactivity (sliding TTL), configurable via `OPEN_TERMINAL_SESSION_CWD_TTL` (or `session_cwd_ttl` in config.toml).

## [0.11.30] - 2026-03-25

### Changed

- 🔑 **Stronger auto-generated API keys** — increased from 192-bit (32 chars) to 384-bit (64 chars) entropy, making brute-force attacks computationally infeasible.
- 🔒 **API key required to start** — the server now refuses to start without an API key configured. The CLI auto-generates one as before, but running via `uvicorn` directly without setting `OPEN_TERMINAL_API_KEY` is no longer allowed.
- 🛡️ **Constant-time API key comparison** — both HTTP and WebSocket authentication now use `hmac.compare_digest()` instead of `!=`, preventing timing-based key extraction.
- ⚠️ **CORS default warning** — a prominent yellow box is printed at startup when `--cors-allowed-origins` is left at the default `*`, warning operators to restrict it for production.

## [0.11.29] - 2026-03-24

### Fixed

- 🔒 **File save fails in multi-user mode** — `_chown()` transferred file ownership to the provisioned user but didn't set group-write permission, leaving files at `644`. The server process (a group member) could create new files but couldn't overwrite them on subsequent saves, returning `PermissionError`. Now sets `chmod g+w` after `chown`, matching the `2770` treatment already applied to directories. ([#93](https://github.com/open-webui/open-terminal/issues/93))

## [0.11.28] - 2026-03-24

### Added

- 📦 **Compressed directory download** (`POST /files/archive`) — bundle one or more files and directories into a ZIP archive for download. Cross-platform compatible (Windows, macOS, Linux). Multi-user access control enforced.

## [0.11.27] - 2026-03-22

### Added

- 🧠 **System prompt endpoint** (`GET /system`) — returns a structured system prompt for LLM integration, grounding the model in the environment (OS, hostname, user, shell, Python version) with directives for tool usage. Gated by `OPEN_TERMINAL_ENABLE_SYSTEM_PROMPT` (default `true`); advertised via `features.system` in `GET /api/config` so consumers can check support before fetching.
- ⚙️ **`OPEN_TERMINAL_ENABLE_SYSTEM_PROMPT`** — environment variable (or `enable_system_prompt` in config.toml) to enable/disable the `/system` endpoint and feature flag. Defaults to `true`.
- ⚙️ **`OPEN_TERMINAL_SYSTEM_PROMPT`** — environment variable (or `system_prompt` in config.toml) to fully override the generated system prompt with custom content.

## [0.11.26] - 2026-03-20

### Changed

- ♻️ **`utils/documents.py`** — extracted all document text extraction logic from `read_file` into a dedicated module with an `EXTRACTORS` registry. Adding a new format now requires only a single function and a one-line registry entry. No behaviour changes.

## [0.11.25] - 2026-03-20

### Added

- 📄 **Extended document text extraction** in `read_file` — `.rtf`, `.xls`, `.odt`, `.ods`, `.odp`, `.epub`, and `.eml` files are now automatically converted to text and returned in the standard JSON format. RTF strips formatting to plain text, legacy Excel (`.xls`) renders sheets as tab-separated values, OpenDocument formats (`.odt`/`.ods`/`.odp`) parse the underlying XML, EPUB extracts body text in reading order, and `.eml` extracts headers and body (with HTML tag stripping). All support `start_line`/`end_line` range selection.

## [0.11.24] - 2026-03-20

### Added

- 📄 **Office document text extraction** in `read_file` — `.docx`, `.xlsx`, and `.pptx` files are now automatically converted to text and returned in the standard JSON format, making them readable by LLMs. Word documents extract paragraphs and table contents, Excel spreadsheets render all sheets as tab-separated values, and PowerPoint presentations extract text from all slides. Supports `start_line`/`end_line` range selection.

## [0.11.23] - 2026-03-19

### Fixed

- 🔐 **`_FILE` mutual exclusivity bypassed by empty env vars** — setting e.g. `OPEN_TERMINAL_API_KEY=""` alongside `OPEN_TERMINAL_API_KEY_FILE` silently skipped the conflict check because empty strings are falsy. The Python helper (`_resolve_file_env`), `entrypoint.sh`, and `entrypoint-slim.sh` now test whether the variable is *set* (not merely non-empty), so any explicit assignment — including `=""` — correctly triggers the mutual-exclusivity error.

## [0.11.22] - 2026-03-19

### Fixed

- 🐛 **`/ports` returns 500 in multi-user mode on restricted runtimes** — the endpoint triggered full user provisioning (`useradd`) just to filter ports by UID. On container runtimes that reject `useradd` (e.g. Azure Container Apps), this crashed with an unhandled exception. Now returns an empty port list when provisioning fails — an unprovisioned user has no ports to show. ([#80](https://github.com/open-webui/open-terminal/issues/80))
- 🐳 **Docker-in-Docker broken in multi-user mode** — mounting the Docker socket (`-v /var/run/docker.sock:/var/run/docker.sock`) with `OPEN_TERMINAL_MULTI_USER=true` failed because only the default `user` account was added to the socket's group. Dynamically provisioned users now automatically inherit Docker socket group membership when the socket is mounted. ([#83](https://github.com/open-webui/open-terminal/issues/83))

## [0.11.21] - 2026-03-19

### Fixed

- 🔍 **Port detection broken since v0.11.2** — `setcap cap_setgid+ep` on the system Python binary (added for multi-user `os.setgroups()`) made all Python processes non-dumpable, blocking `/proc/[pid]/fd/` access needed to resolve socket inodes to PIDs. Ports from user-spawned Python servers were silently filtered out. Fixed by copying the Python binary and granting `cap_setgid` only to the copy (`python3-ot`), used exclusively by the open-terminal server. The system `python3` stays capability-free so user processes remain dumpable. Slim and Alpine images had `setcap` removed entirely since they don't support multi-user mode. ([#85](https://github.com/open-webui/open-terminal/issues/85), [#63](https://github.com/open-webui/open-terminal/issues/63))
- 📖 **README** — Image Variants table incorrectly listed multi-user mode as supported on slim and alpine images. Multi-user mode requires `sudo`, which only the full image includes.

## [0.11.20] - 2026-03-15

### Fixed

- 👥 **Multi-user mode works when running as root** — `ensure_os_user()` no longer unconditionally requires `sudo`; when the process is already root (e.g. `user: "0:0"` in Docker Compose), user provisioning commands run directly. `check_environment()` now only requires `sudo` when not running as root, with an actionable error message pointing to the standard image or Terminals when neither root nor sudo is available. ([#60](https://github.com/open-webui/open-terminal/issues/60))

## [0.11.19] - 2026-03-15

### Fixed

- 🔒 **`write_file` permission denied in multi-user subdirectories** — file writes into directories created by `run_command` failed because `mkdir -p` (via `sudo -u`) creates directories with default `755` permissions, leaving the server process without group-write access. `UserFS` now creates parent directories as the provisioned user (`sudo -u mkdir -p`) and sets `2770` (setgid + group rwx) on the entire directory chain, matching the home directory's permissions. Writing to the home root was unaffected. ([#70](https://github.com/open-webui/open-terminal/issues/70))

## [0.11.18] - 2026-03-15

### Added

- ⚡ **Configurable log flush strategy** — new `OPEN_TERMINAL_LOG_FLUSH_INTERVAL` and `OPEN_TERMINAL_LOG_FLUSH_BUFFER` environment variables (or `log_flush_interval` / `log_flush_buffer` in config.toml) control how frequently process output is flushed to disk. Default `0` preserves the existing per-chunk flush behaviour. Setting `OPEN_TERMINAL_LOG_FLUSH_INTERVAL=1` reduces fsyncs from ~250/sec to ~1/sec for high-output commands, preventing I/O storms that can make ARM/eMMC systems unresponsive. ([#65](https://github.com/open-webui/open-terminal/issues/65))

### Changed

- 🔧 **Centralized flush control** — per-chunk `flush()` calls removed from `PtyRunner`, `PipeRunner`, and `WinPtyRunner`; flushing is now managed entirely by `BoundedLogWriter` based on the configured interval and buffer settings. An explicit final flush is performed before writing the process end marker.

## [0.11.17] - 2026-03-14

### Fixed

- 🌐 **Global pip packages in multi-user mode** — `OPEN_TERMINAL_PIP_PACKAGES` now installs to the system-wide site-packages (`sudo pip install`) when `OPEN_TERMINAL_MULTI_USER=true`, so all provisioned users share the same packages. Previously, packages were installed to `/home/user/.local/` and only accessible to the default user. ([#68](https://github.com/open-webui/open-terminal/issues/68))

## [0.11.16] - 2026-03-13

### Removed

- 🧹 **Removed experimental `url` parameter from `/files/upload`** — this feature was never used by any known consumer (Open WebUI uses multipart uploads). The endpoint now only accepts direct file uploads.

## [0.11.15] - 2026-03-13

### Changed

- 🖥️ **Redesigned startup output** — the CLI now displays Local and Network URLs, the generated API key, and a bind warning in a clean, modern key-value layout with color-coded labels. Network URL auto-detects your LAN IP when binding to `0.0.0.0`.
- 🔒 **Bind warning** — a yellow warning is printed at startup when binding to `0.0.0.0`, nudging bare-metal users to restrict access with `--host 127.0.0.1`.

## [0.11.14] - 2026-03-13

### Fixed

- 🏠 **Multi-user home directory hints** — `get_system_info()` no longer includes `as user 'user'` in the OpenAPI description when multi-user mode is active, removing a misleading hint that caused smaller LLMs to write to `/home/user` instead of their assigned directory. ([#57](https://github.com/open-webui/open-terminal/issues/57))
- 🔄 **`/home/usr` path rewrite** — `resolve_path()` now also rewrites `/home/usr` (a common LLM hallucination) to the provisioned user's home directory, matching the existing `/home/user` rewrite.

## [0.11.13] - 2026-03-13

### Fixed

- 🐛 **Recursive home directory ownership fix** — `chown` in `ensure_os_user()` now uses `-R` to recursively fix ownership of all files within a user's home directory when the OS user is recreated with a different UID (e.g. after container recreation with a persistent volume). Previously only the top-level directory was re-owned, leaving files inside with a mismatched UID. ([#62](https://github.com/open-webui/open-terminal/issues/62))

## [0.11.12] - 2026-03-12

### Added

- 🔒 **Network egress filtering** (Docker only) — restrict which domains the container can access via the `OPEN_TERMINAL_ALLOWED_DOMAINS` env var. Supports wildcards (e.g. `*.github.com`). Set to empty string to block all outbound traffic; omit for full access. Skips gracefully on bare-metal installs.

## [0.11.11] - 2026-03-11

### Fixed

- 🔒 **Upload path traversal** — `/files/upload` now resolves the `directory` parameter through `fs.resolve_path()` and sanitizes the uploaded filename with `os.path.basename()`, preventing path traversal attacks (e.g. `../../etc/passwd`) that could escape the user's home directory in multi-user mode. The composed path is normalized with `os.path.normpath()` and validated by `_check_path` before writing. All other file endpoints already had these protections.

## [0.11.10] - 2026-03-11

### Fixed

- 🧟 **Zombie process cleanup** — process runner `kill()` methods now use `os.killpg()` to signal the entire process group instead of just the direct child PID. Background processes started inside terminals or `/execute` sessions (e.g. `sleep 100 &`) are now properly terminated on cleanup. `_cleanup_session()` always calls `process.wait()` after force-killing to prevent zombie entries in the process table.
- 🐳 **Docker PID 1 reaping** — added `tini` as the container's init process (`ENTRYPOINT ["/usr/bin/tini", "--", ...]`). Python/uvicorn no longer runs as PID 1, so orphaned grandchild processes are automatically reaped instead of accumulating as zombies.

## [0.11.9] - 2026-03-11

### Added

- ⏰ **Timestamp-sortable process IDs** — process IDs now use a `YYYYMMDD-HHMMSS-<random>` format so log files sort chronologically in the filesystem. The most recent log is always at the bottom of `ls`. ([#54](https://github.com/open-webui/open-terminal/issues/54))
- ⚙️ **`OPEN_TERMINAL_MAX_LOG_SIZE`** — environment variable (or `max_log_size` in config.toml) to set the per-process log file size limit in bytes. Default: 50 MB.
- ⚙️ **`OPEN_TERMINAL_LOG_RETENTION`** — environment variable (or `log_retention` in config.toml) to set how long finished-process log files are kept on disk before automatic cleanup. Default: 7 days.
- 📁 **`utils/log.py`** — extracted process log management code (`BoundedLogWriter`, `log_process`, `read_log`, `tail_log`) into a dedicated module to reduce `main.py` size.

### Fixed

- 🐛 **Memory leak — unbounded process log growth** — JSONL log files for background processes now rotate when they exceed a configurable size limit (`OPEN_TERMINAL_MAX_LOG_SIZE`, default 50 MB). When the limit is reached, the oldest half of the file is discarded and writing continues, so the most recent output is always available. Previously, a long-running process could grow its log file without limit, and `_read_log()` loaded the entire file into memory on every status poll — causing the container to consume all available host RAM (~26 GB) and trigger the OOM killer. ([#52](https://github.com/open-webui/open-terminal/issues/52))

## [0.11.8] - 2026-03-11

### Fixed

- 🔒 **Multi-user search isolation** — `glob_search`, `grep_search`, `listdir`, and `walk` now filter out entries belonging to other users' home directories during traversal. Previously, searching a parent directory like `/home` would expose all users' files. Added `is_path_allowed()` to `UserFS` for per-entry validation during `os.walk`. ([#46](https://github.com/open-webui/open-terminal/issues/46))

## [0.11.7] - 2026-03-11

### Fixed

- 🐛 **Multi-user relative path resolution** — relative paths (e.g. `abcdef.txt`, `.`) now resolve against the provisioned user's home directory instead of the server process's `/home/user`. All file, search, and execute endpoints use the new `UserFS.resolve_path()` method. ([#47](https://github.com/open-webui/open-terminal/issues/47))
- 🔄 **Auto-swap `/home/user` paths** — in multi-user mode, absolute paths under `/home/user` are automatically rewritten to the provisioned user's home directory, handling LLMs that hardcode the default path from the system description.

## [0.11.6] - 2026-03-10

### Added

- ℹ️ **Conditional `/info` endpoint** — new `OPEN_TERMINAL_INFO` environment variable (or `info` in config.toml) registers a `GET /info` endpoint that returns operator-provided context to the AI. Use it to describe the environment (e.g. container base OS, available tools, GPU access). When the variable is unset, the endpoint is not registered.

## [0.11.5] - 2026-03-09

### Fixed

- 🐛 **Terminal PTY warnings** — wrapped multi-user terminal sessions with `script -qc` for proper PTY allocation, eliminating `cannot set terminal process group` and `no job control` warnings.
- 🐛 **Stale home directory ownership** — added `chown` after `useradd` to handle pre-existing home directories with mismatched UID/GID from previous container runs.

### Changed

- 📖 **README** — updated multi-user documentation with accurate description and production warning.

## [0.11.4] - 2026-03-09

### Added

- 🔌 **Per-user port visibility** — in multi-user mode, `/ports` now filters by socket UID so each user only sees their own listening ports.

### Changed

- 📁 **Module reorganization** — moved `runner.py`, `notebooks.py`, and `user_isolation.py` into `open_terminal/utils/` for a cleaner package layout.

## [0.11.3] - 2026-03-09

### Fixed

- 🔒 **Cross-user file API isolation** — file endpoints now block access to other users' home directories via path validation, returning `403 Forbidden`. System paths (`/etc`, `/usr`, etc.) remain accessible.
- 🐛 **Terminal spawn directory** — interactive terminals now start in the user's home directory instead of `/home/user` (`sudo -i -u`).

### Changed

- ♻️ **Native Python I/O for writes** — replaced `sudo tee`, `sudo mkdir -p`, `sudo rm -rf`, `sudo mv` with native `aiofiles`/`os`/`shutil`. The only remaining subprocess is `sudo chown` for ownership fixup after writes. Home directories use `chmod 2770` (setgid + group rwx).

## [0.11.2] - 2026-03-09

### Changed

- ♻️ **Native Python I/O for multi-user reads** — replaced subprocess-based file reads (`cat`, `find -printf`, `stat -c`, `test`) with native `aiofiles`/`os` calls. Home directories now use `chmod 750` with group membership so the server can read directly. Writes still use `sudo -u` for correct ownership. Cross-user isolation preserved via Unix group permissions.
- 🐳 **Dockerfile** — grants `CAP_SETGID` to the Python binary via `setcap` so the server can refresh supplementary groups at runtime when provisioning new users.

## [0.11.1] - 2026-03-09

### Fixed

- 🐛 **Multi-user file operations** — all file endpoints (list, read, view, display, replace, grep, glob, upload) now correctly run as the provisioned user. Previously only write/delete/move were handled, causing `PermissionError` on reads in user home directories.

### Changed

- ♻️ **UserFS abstraction** (`open_terminal/utils/fs.py`) — unified filesystem interface that transparently routes I/O through `sudo -u` in multi-user mode. Endpoints receive a `UserFS` instance via dependency injection and no longer branch on mode. Replaces per-endpoint sudo wrappers.

## [0.11.0] - 2026-03-09

### Added

- 👥 **Multi-user mode** (`OPEN_TERMINAL_MULTI_USER=true`) — per-user OS accounts inside a single container, with standard Unix permissions (`chmod 700`) providing kernel-enforced isolation between users. When enabled, Open Terminal reads the `X-User-Id` header (set by the Open WebUI proxy), provisions a dedicated Linux user on first access via `useradd`, and runs all commands, file operations, and terminal sessions as that user via `sudo -u`. No Docker socket, no per-user containers, no enterprise license required. Fails fast with a clear error on non-Linux platforms. ([#38](https://github.com/open-webui/open-terminal/issues/38))
- ⚙️ **`OPEN_TERMINAL_UVICORN_LOOP`** — environment variable (or `uvicorn_loop` in config.toml) to configure the Uvicorn event loop implementation. Defaults to `auto`.

## [0.10.2] - 2026-03-06

### Added

- 🐳 **Docker CLI, Compose, and Buildx** bundled in the container image via [get.docker.com](https://get.docker.com). Mount the host's Docker socket (`-v /var/run/docker.sock:/var/run/docker.sock`) to let agents clone repos, build images, and run containers. The entrypoint automatically fixes socket group permissions so `docker` commands work without `sudo`.

## [0.10.1] - 2026-03-06

### Fixed

- 🌐 **UTF-8 encoding on Windows** — all text file I/O now explicitly uses UTF-8 encoding instead of the system default. Fixes Chinese (and other non-ASCII) content being written as GB2312 on Chinese Windows, which broke tool-call chaining and produced garbled files. ([#21](https://github.com/open-webui/open-terminal/issues/21))

## [0.10.0] - 2026-03-05

### Added

- 📓 **Notebook execution** (`/notebooks`) — multi-session Jupyter notebook execution via REST endpoints. Each session gets its own kernel via `nbclient`. Supports per-cell execution with rich outputs (images, HTML, LaTeX). `nbclient` and `ipykernel` are now core dependencies.
- ⚙️ **`OPEN_TERMINAL_ENABLE_NOTEBOOKS`** — environment variable (or `enable_notebooks` in config.toml) to enable/disable notebook execution endpoints. Defaults to `true`. Exposed in `GET /api/config` features.

## [0.9.3] - 2026-03-05

### Added

- 📓 **Notebook execution support** — new `notebooks` optional extra (`pip install open-terminal[notebooks]`) adds `nbclient` and `ipykernel` for running Jupyter notebooks with per-cell execution and full rich output (images, HTML, LaTeX). Keeps the core package lightweight for users who don't need notebook support.

## [0.9.2] - 2026-03-05

### Added

- 📝 **Custom execute description** — new `OPEN_TERMINAL_EXECUTE_DESCRIPTION` environment variable (or `execute_description` in config.toml) appends custom text to the execute endpoint's OpenAPI description, letting you tell AI models about installed tools, capabilities, or conventions.

## [0.9.1] - 2026-03-05

### Added

- 📦 **Startup package installation** — new `OPEN_TERMINAL_PACKAGES` and `OPEN_TERMINAL_PIP_PACKAGES` environment variables install additional apt and pip packages automatically when the Docker container starts. No need to fork the Dockerfile for common customizations.

## [0.9.0] - 2026-03-04

### Added

- 🔍 **Port detection** (`GET /ports`) — discovers TCP ports listening on localhost, scoped to descendant processes of open-terminal (servers started via the terminal or `/execute`). Cross-platform: parses `/proc/net/tcp` on Linux, `lsof` on macOS, `netstat` on Windows. Zero new dependencies.
- 🔀 **Port proxy** (`/proxy/{port}/{path}`) — reverse-proxies HTTP requests to `localhost:{port}`, enabling browser access to servers running inside the terminal environment. Supports all HTTP methods, forwards headers and body, returns 502 on connection refused. Uses the existing `httpx` dependency.
- 📦 **`utils.port` module** — port detection and process-tree utilities extracted into `open_terminal/utils/port.py` for reusability.

## [0.8.3] - 2026-03-04

### Added

- ⏱️ **Default execute timeout** — new `OPEN_TERMINAL_EXECUTE_TIMEOUT` environment variable (or `execute_timeout` in config.toml) sets a default wait duration for command execution. Smaller models that don't set timeouts now get command output inline instead of assuming failure.

## [0.8.2] - 2026-03-02

### Added

- 🎨 **Terminal color support** — terminal sessions now set the `TERM` environment variable (default `xterm-256color`) so programs emit ANSI color codes. Configurable via `OPEN_TERMINAL_TERM` environment variable or `term` in config.toml.

## [0.8.1] - 2026-03-02

### Added

- ⚙️ **Configurable terminal feature** — new `OPEN_TERMINAL_ENABLE_TERMINAL` environment variable (or `enable_terminal` in config.toml) to enable or disable the interactive terminal. When disabled, all `/api/terminals` routes and the WebSocket endpoint are not mounted. Defaults to `true`.
- 🔍 **Config discovery endpoint** (`GET /api/config`) — returns server feature flags so clients like Open WebUI can discover whether the terminal is enabled and adapt the UI accordingly.

## [0.8.0] - 2026-03-02

### Added

- 🪟 **Windows PTY support** — terminal sessions and command execution now work on Windows via [pywinpty](https://github.com/andfoy/pywinpty) (ConPTY). `pywinpty` is auto-installed on Windows. Interactive terminals (`/api/terminals`), colored output, and TUI apps now work on Windows instead of returning 503.
- 🏭 **WinPtyRunner** — new `ProcessRunner` implementation using `winpty.PtyProcess` for full PTY semantics on Windows, including resize support. The `create_runner` factory now prefers Unix PTY → WinPTY → pipe fallback.

## [0.7.2] - 2026-03-02

### Added

- 🔒 **Terminal session limit** — new `OPEN_TERMINAL_MAX_SESSIONS` environment variable (default `16`) caps the number of concurrent interactive terminal sessions. Dead sessions are automatically pruned before the limit is checked. Returns `429` when the limit is reached.

### Fixed

- 🐳 **PTY device exhaustion** — fixed `OSError: out of pty devices` by closing leaked file descriptors when subprocess creation fails after `pty.openpty()`. Both `PtyRunner` (command execution) and `create_terminal` (interactive sessions) now properly clean up on error paths.
- 🛡️ **Graceful PTY error handling** — `create_terminal` now returns a clear `503` with a descriptive message when the system runs out of PTY devices, instead of an unhandled server error.

## [0.7.1] - 2026-03-02

### Fixed

- 🐳 **Docker terminal shell** — fixed `can't access tty; job control turned off` error by setting the default shell to `/bin/bash` for the container user. Previously the user was created with `/bin/sh` (dash), which does not support interactive job control in a PTY.

## [0.7.0] - 2026-03-02

### Added

- 🖥️ **Interactive terminal sessions** — full PTY-based terminal accessible via WebSocket, following the JupyterLab/Kubernetes resource pattern. `POST /api/terminals` to create a session, `GET /api/terminals` to list, `DELETE /api/terminals/{id}` to kill, and `WS /api/terminals/{id}` to attach. Non-blocking I/O ensures the terminal never starves other API requests. Sessions are automatically cleaned up on disconnect.

## [0.6.0] - 2026-03-02

### Added

- 📄 **Configuration file support** — settings can now be loaded from TOML config files at /etc/open-terminal/config.toml (system-wide) and $XDG_CONFIG_HOME/open-terminal/config.toml (per-user, defaults to ~/.config/open-terminal/config.toml). Supports host, port, api_key, cors_allowed_origins, log_dir, and binary_mime_prefixes. CLI flags and environment variables still take precedence. Use --config to point to a custom config file. This keeps the API key out of ps / htop output.

## [0.5.0] - 2026-03-02

### Changed

- 📂 **XDG Base Directory support** — the default log directory moved from ~/.open-terminal/logs to the XDG-compliant path $XDG_STATE_HOME/open-terminal/logs (defaults to ~/.local/state/open-terminal/logs when XDG_STATE_HOME is not set). The OPEN_TERMINAL_LOG_DIR environment variable still overrides the default.

## [0.4.3] - 2026-03-02

### Added

- 🔐 **Docker secrets support** — set OPEN_TERMINAL_API_KEY_FILE to load the API key from a file (e.g. /run/secrets/...), following the convention used by the official PostgreSQL Docker image.

## [0.4.2] - 2026-03-02

### Added

- 📦 **Move endpoint** (POST /files/move) for moving and renaming files and directories. Uses shutil.move for cross-filesystem support. Hidden from OpenAPI schema.

## [0.4.1] - 2026-03-01

### Fixed

- 🙈 **Hidden upload_file from OpenAPI schema** — the /files/upload endpoint is now excluded from the public API docs, consistent with other internal-only file endpoints.

## [0.4.0] - 2026-03-01

### Removed

- 📥 **Temporary download links** (GET /files/download/link and GET /files/download/{token}) — deprecated in favour of direct file navigation built into Open WebUI.
- 🔗 **Temporary upload links** (POST /files/upload/link, GET /files/upload/{token}, and POST /files/upload/{token}) — deprecated in favour of direct file navigation built into Open WebUI.

## [0.3.0] - 2026-02-25

### Added

- 🖥️ **Pseudo-terminal (PTY) execution** — commands now run under a real PTY by default, enabling colored output, interactive programs (REPLs, TUI apps), and proper isatty() detection. Falls back to pipe-based execution on Windows.
- 🏭 **Process runner abstraction** — new ProcessRunner factory pattern (PtyRunner / PipeRunner) in runner.py for clean, extensible process management.
- 🔡 **Escape sequence conversion** in send_process_input — literal escape strings from LLMs (\n, \x03 for Ctrl-C, \x04 for Ctrl-D, etc.) are automatically converted to real characters.

### Changed

- 📦 **Merged output stream** — PTY output is logged as type "output" (merged stdout/stderr) instead of separate streams, matching real terminal behavior.

## [0.2.9] - 2026-02-25

### Added

- 📺 **Display file endpoint** (GET /files/display) — a signaling endpoint that lets AI agents request a file be shown to the user. The consuming client is responsible for handling the response and presenting the file in its own UI.

### Changed

- ⏳ **Improved wait behavior** — wait=0 on the status endpoint now correctly triggers a wait instead of being treated as falsy, so commands that finish quickly return immediately rather than requiring a non-zero wait value.

## [0.2.8] - 2026-02-25

### Added

- 📄 **PDF text extraction** in read_file — PDF files are now automatically converted to text using pypdf and returned in the standard text-file JSON format, making them readable by LLMs. Supports start_line/end_line range selection.

## [0.2.7] - 2026-02-25

### Added

- 👁️ **File view endpoint** (GET /files/view) for serving raw binary content of any file type with the correct Content-Type. Designed for UI previewing (PDFs, images, etc.) without the MIME restrictions of read_file.
- 📂 **--cwd CLI option** for both run and mcp commands to set the server's working directory on startup.
- 📍 **Working directory endpoints** — GET /files/cwd and POST /files/cwd to query and change the current working directory at runtime.
- 📁 **mkdir endpoint** (POST /files/mkdir) to create directories with automatic parent directory creation.
- 🗑️ **delete endpoint** (DELETE /files/delete) to remove files and directories.

### Changed

- 📄 **Binary-aware read_file** returns raw binary responses for supported file types (images, etc.) and rejects unsupported binary files with a descriptive error. Configurable via OPEN_TERMINAL_BINARY_MIME_PREFIXES env var.

## [0.2.6] - 2026-02-24

### Added

- 🔍 **File Search Endpoints**: Added a new /files/glob endpoint (alias glob_search) to search for files by name/pattern using wildcards.
- 🔄 **Alias Update**: Renamed and aliased the existing /files/search endpoint to /files/grep (alias grep_search) to establish a clear distinction between content-level search (grep) and filename-level search (glob).

## [0.2.5] - 2026-02-23

### Fixed

- 🛡️ **Graceful permission error handling** across all file endpoints (write_file, replace_file_content, upload_file). PermissionError and other OSError exceptions now return HTTP 400 with a descriptive message instead of crashing with HTTP 500.
- 🐳 **Docker volume permissions** via entrypoint.sh that automatically fixes /home/user ownership on startup when a host volume is mounted with mismatched permissions.
- 🔧 **Background process resilience** — _log_process no longer crashes if the log directory is unwritable; commands still execute and complete normally.

## [0.2.4] - 2026-02-19

### Changed

- ⚡ **Fully async I/O** across all file and upload endpoints. Replaced blocking os.* and open() calls with aiofiles and aiofiles.os so the event loop is never blocked by filesystem operations. search_files and list_files inner loops use asyncio.to_thread for os.walk/os.listdir workloads.

## [0.2.3] - 2026-02-15

### Added

- 🤖 **Optional MCP server mode** via open-terminal mcp, exposing all endpoints as MCP tools for LLM agent integration. Supports stdio and streamable-http transports. Install with pip install open-terminal[mcp].

## [0.2.2] - 2026-02-15

### Fixed

- 🛡️ **Null query parameter tolerance** via HTTP middleware that strips query parameters with the literal value "null". Prevents 422 errors when clients serialize null into query strings (e.g. ?wait=null) instead of omitting the parameter.

## [0.2.1] - 2026-02-14

### Added

- 📁 **File-backed process output** persisted to JSONL log files under 'logs/processes/', configurable via 'OPEN_TERMINAL_LOG_DIR'. Full audit trail survives process cleanup and server restarts.
- 📍 **Offset-based polling** on the status endpoint with 'offset' and 'next_offset' for stateless incremental reads. Multiple clients can independently track the same process without data loss.
- ✂️ **Tail parameter** on both execute and status endpoints to return only the last N output entries, keeping AI agent responses bounded.

### Changed

- 🗑️ **Removed in-memory output buffer** in favor of reading directly from the JSONL log file as the single source of truth.
- 📂 **Organized log directory** with process logs namespaced under 'logs/processes/' to accommodate future log types.

### Removed

- 🔄 **Bounded output buffers** and the 'OPEN_TERMINAL_MAX_OUTPUT_LINES' environment variable, no longer needed without in-memory buffering.

## [0.2.0] - 2026-02-14

### Added

- 📂 **File operations** for reading, writing, listing, and find-and-replace, with optional line-range selection for large files.
- 📤 **File upload** by URL or multipart form data.
- 📥 **Temporary download links** that work without authentication, making it easy to retrieve files from the container.
- 🔗 **Temporary upload links** with a built-in drag-and-drop page for sharing with others.
- ⌨️ **Stdin input** to send text to running processes, enabling interaction with REPLs and interactive commands.
- 📋 **Process listing** to view all tracked background processes and their current status at a glance.
- ⏳ **Synchronous mode** with an optional 'wait' parameter to block until a command finishes and get output inline.
- 🔄 **Bounded output buffers** to prevent memory issues on long-running commands, configurable via 'OPEN_TERMINAL_MAX_OUTPUT_LINES'.
- 🛠️ **Rich toolbox** pre-installed in the container, including Python data science libraries, networking utilities, editors, and build tools.
- 👤 **Non-root user** with passwordless 'sudo' available when elevated privileges are needed.
- 🚀 **CI/CD pipeline** for automated multi-arch Docker image builds and publishing via GitHub Actions.
- 💾 **Named volume** in the default 'docker run' command so your files survive container restarts.

### Changed

- 🐳 **Expanded container image** with system packages and Python libraries for a batteries-included experience.

## [0.1.0] - 2026-02-12

### Added

- 🎉 **Initial release** of Open Terminal, a lightweight API that turns any container into a remote shell for AI agents and automation workflows.
- ▶️ **Background command execution** with async process tracking, supporting shell features like pipes, chaining, and redirections.
- 🔑 **Bearer token authentication** to secure your instance using the 'OPEN_TERMINAL_API_KEY' environment variable.
- 🔐 **Zero-config setup** with an auto-generated API key printed to container logs when none is provided.
- 💚 **Health check** endpoint at '/health' for load balancer and orchestrator integration.
- 🌐 **CORS enabled by default** for seamless integration with web-based AI tools and dashboards.
