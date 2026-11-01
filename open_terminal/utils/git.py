"""Git operations using the same filesystem identity as the terminal."""

import asyncio
import os
import signal
import json
import sys

from fastapi import HTTPException

from open_terminal.env import sanitized_environ


class GitRepository:
    def __init__(self, fs, directory, cwd=None):
        self.fs = fs
        self.root = self.checked_path(fs.resolve_path(directory, cwd=cwd))

    def checked_path(self, path):
        try:
            self.fs._check_path(path)
            self.fs._check_path(os.path.realpath(path))
        except PermissionError as exc:
            raise HTTPException(403, "Access denied") from exc
        return os.path.realpath(path)

    async def run(self, *args, check=True, program="git", input_data=None):
        env = {key: value for key, value in sanitized_environ().items() if not key.startswith("GIT_")}
        env.update(HOME=self.fs.home, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GH_PROMPT_DISABLED="1", LC_ALL="C")
        command = ["git", "-c", "core.quotePath=false", *args]
        if program == "gh":
            command = ["gh", *args]
        elif program == "line-stats":
            command = [sys.executable, "-m", "open_terminal.utils.git"]
        options = {}
        if self.fs.username:
            if hasattr(os, "getuid") and os.getuid() == 0:
                import pwd

                account = pwd.getpwnam(self.fs.username)
                options = {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": []}
            else:
                command = ["sudo", "-H", "-n", "-u", self.fs.username, "--", *command]
        try:
            process = await asyncio.create_subprocess_exec(
                *command, cwd=self.root, env=env, stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name != "nt", **options,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, f"{program} or the directory is unavailable") from exc

        async def read(stream):
            data = bytearray()
            while chunk := await stream.read(65536):
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise HTTPException(413, "Git output exceeds the 8 MiB limit")
            return data.decode("utf-8", errors="replace")

        readers = [asyncio.create_task(read(process.stdout)), asyncio.create_task(read(process.stderr))]
        async def exchange():
            if input_data:
                process.stdin.write(input_data)
                await process.stdin.drain()
                process.stdin.close()
            output = await asyncio.gather(*readers)
            await process.wait()
            return output

        try:
            out, err = await asyncio.wait_for(exchange(), timeout=60)
            if check and process.returncode:
                raise HTTPException(400, err.strip() or out.strip() or "Git operation failed")
            return process.returncode, out, err
        except asyncio.TimeoutError as exc:
            raise HTTPException(408, "Git operation exceeded the 60 second limit") from exc
        finally:
            if os.name != "nt":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.returncode is None:
                process.kill()
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await asyncio.gather(process.stdout.read(), process.stderr.read())
            await process.wait()

    async def detect(self):
        code, output, error = await self.run("rev-parse", "--show-toplevel", check=False)
        if code:
            if "not a git repository" in error.lower():
                return False
            raise HTTPException(400, error.strip() or "Git repository detection failed")
        self.root = self.checked_path(output.removesuffix("\n"))
        # Linked worktrees can keep their metadata outside the working directory.
        for flag in ("--absolute-git-dir", "--git-common-dir"):
            _, path, _ = await self.run("rev-parse", flag)
            self.checked_path(os.path.join(self.root, path.removesuffix("\n")))
        return True

    def file_path(self, path):
        if not path or os.path.isabs(path):
            raise HTTPException(400, "Expected a repository-relative file path")
        absolute = os.path.abspath(os.path.join(self.root, path))
        if os.path.commonpath([self.root, absolute]) != self.root:
            raise HTTPException(400, "File is outside the repository")
        self.checked_path(absolute)
        return path

    async def status(self):
        if not await self.detect():
            return {"is_repo": False, "branch": "", "files": [], "ahead": 0, "behind": 0}
        _, output, _ = await self.run("status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
        result = {"is_repo": True, "root": self.root, "branch": "", "upstream": "", "ahead": 0, "behind": 0, "files": []}
        records = iter(output.split("\0"))
        for record in records:
            if record.startswith("# branch.head "):
                result["branch"] = record[14:]
            elif record.startswith("# branch.oid "):
                result["head"] = record[13:]
            elif record.startswith("# branch.upstream "):
                result["upstream"] = record[18:]
            elif record.startswith("# branch.ab "):
                ahead, behind = record[12:].split()
                result.update(ahead=int(ahead), behind=abs(int(behind)))
            elif record.startswith(("1 ", "2 ", "u ", "? ")):
                if record.startswith("? "):
                    path, xy = record[2:], "??"
                else:
                    fields = record.split(" ", {"1": 8, "2": 9, "u": 10}[record[0]])
                    path, xy = fields[-1], fields[1]
                entry = {"path": path, "staged": xy[0] not in ".?", "unstaged": xy[1] != ".", "staged_status": xy[0], "unstaged_status": xy[1], "additions": 0, "deletions": 0}
                entry["status"] = "conflict" if record.startswith("u ") else "untracked" if xy == "??" else self.status_name(xy[1] if xy[1] != "." else xy[0])
                if record.startswith("2 "):
                    entry["original_path"] = next(records)
                result["files"].append(entry)
        files = {file["path"]: file for file in result["files"]}
        for args in ((), ("--cached",)):
            _, stats, _ = await self.run("diff", "--no-ext-diff", "--no-textconv", "--numstat", "-z", *args)
            records = iter(stats.split("\0"))
            for record in records:
                if not record:
                    continue
                added, deleted, path = record.split("\t", 2)
                if not path:
                    next(records)  # old path for a rename
                    path = next(records)
                file = files.get(path)
                if file is None:
                    continue
                if added == "-":
                    file["binary"] = True
                else:
                    file["additions"] += int(added)
                    file["deletions"] += int(deleted)
        untracked = []
        for file in result["files"]:
            if file["status"] == "untracked":
                try:
                    untracked.append(self.file_path(file["path"]))
                except HTTPException:
                    pass
        if untracked:
            _, stats, _ = await self.run(program="line-stats", input_data=json.dumps(untracked).encode())
            for path, count in json.loads(stats).items():
                if count is None:
                    files[path]["binary"] = True
                else:
                    files[path]["additions"] = count
        code, remote, _ = await self.run("remote", "get-url", "origin", check=False)
        result["remote_url"] = remote.strip() if code == 0 else ""
        return result

    @staticmethod
    def status_name(code):
        return {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "T": "type-changed", "U": "conflict"}.get(code, code)

    async def diff(self, path, staged=False, untracked=False, ignore_whitespace=False):
        if not await self.detect():
            raise HTTPException(400, "Not a Git repository")
        path = self.file_path(path)
        args = ["diff", "--no-ext-diff", "--no-textconv", "--color=never", "--unified=3"]
        if ignore_whitespace:
            args.append("--ignore-all-space")
        if untracked:
            args += ["--no-index", "--", os.devnull, path]
        else:
            if staged:
                args += ["--cached"]
            args += ["--", f":(literal){path}"]
        code, output, err = await self.run(*args, check=False)
        if code not in ((0, 1) if untracked else (0,)):
            raise HTTPException(400, err.strip() or "Cannot read diff")
        return {"diff": output}

    async def history(self, limit=50, offset=0):
        if not await self.detect():
            raise HTTPException(400, "Not a Git repository")
        code, _, _ = await self.run("rev-parse", "--verify", "HEAD", check=False)
        if code:
            return []
        _, output, _ = await self.run("log", f"-{max(1, min(limit, 200))}", f"--skip={max(0, offset)}", "--format=%H%x00%h%x00%an%x00%aI%x00%s%x00")
        fields = output.split("\0")
        return [dict(zip(("hash", "short_hash", "author", "date", "message"), [fields[i].lstrip("\n"), *fields[i + 1:i + 5]])) for i in range(0, len(fields) - 5, 5)]

    async def show(self, commit):
        import re
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
            raise HTTPException(400, "Expected a commit hash")
        if not await self.detect():
            raise HTTPException(400, "Not a Git repository")
        _, output, _ = await self.run("show", "--format=", "--no-ext-diff", "--no-textconv", "--color=never", commit, "--")
        return {"diff": output}

    async def branches(self):
        if not await self.detect():
            raise HTTPException(400, "Not a Git repository")
        _, output, _ = await self.run("for-each-ref", "--format=%(refname:short)%00%(HEAD)%00%(upstream:short)%00%(symref)%00%(refname)", "refs/heads", "refs/remotes")
        return [{"name": name, "current": current == "*", "upstream": upstream, "is_local": ref.startswith("refs/heads/")} for line in output.splitlines() if len(parts := line.split("\0")) == 5 for name, current, upstream, symbolic, ref in [parts] if not symbolic]

    async def worktrees(self):
        if not await self.detect():
            raise HTTPException(400, "Not a Git repository")
        _, output, _ = await self.run("worktree", "list", "--porcelain", "-z")
        trees = []
        tree = {}
        for field in output.split("\0"):
            if field.startswith("worktree "):
                tree = {"path": field[9:], "branch": "", "current": os.path.realpath(field[9:]) == self.root}
                try:
                    self.checked_path(tree["path"])
                except HTTPException:
                    continue
                trees.append(tree)
            elif field.startswith("branch "):
                tree["branch"] = field[7:].removeprefix("refs/heads/")
            elif field == "detached":
                tree["branch"] = "(detached)"
        return trees

    async def pull_requests(self, view="list", number=0, state="open"):
        if not await self.detect():
            return {"available": False} if view == "capabilities" else []
        if view == "capabilities":
            try:
                code, _, _ = await self.run("--version", program="gh", check=False)
                if code:
                    return {"available": False}
                code, output, _ = await self.run("repo", "view", "--json", "nameWithOwner,defaultBranchRef", program="gh", check=False)
                repo = json.loads(output) if code == 0 else {}
                return {"available": code == 0, "default_branch": (repo.get("defaultBranchRef") or {}).get("name", "main")}
            except HTTPException:
                return {"available": False}
        fields = "number,title,state,isDraft,author,headRefName,baseRefName,url,body,mergeable,reviewDecision"
        if view == "list":
            if state not in ("open", "closed", "merged", "all"):
                raise HTTPException(400, "Invalid pull request state")
            args = ["pr", "list", "--state", state, "--limit", "50", "--json", fields]
        elif number <= 0:
            raise HTTPException(400, "Select a pull request")
        elif view == "view":
            args = ["pr", "view", str(number), "--json", fields + ",comments,reviews,statusCheckRollup,reviewRequests,assignees,labels"]
        elif view == "diff":
            args = ["pr", "diff", str(number), "--color=never"]
        elif view == "checks":
            code, output, error = await self.run("pr", "checks", str(number), "--json", "bucket,completedAt,description,event,link,name,startedAt,state,workflow", program="gh", check=False)
            if code not in (0, 1, 8):
                raise HTTPException(400, error.strip() or "Cannot read checks")
            return {"items": json.loads(output) if output.strip() else []}
        else:
            raise HTTPException(400, "Unknown pull request view")
        _, output, _ = await self.run(*args, program="gh")
        return {"diff": output} if view == "diff" else {"items": json.loads(output)} if view == "list" else json.loads(output)

    async def pull_request_action(self, action, number=0, title="", body="", base="", head="", draft=False, method="squash"):
        if not await self.detect():
            raise HTTPException(400, "Not a Git repository")
        if action == "create":
            if not title.strip() or not base.strip() or not head.strip():
                raise HTTPException(400, "Enter a title, base branch, and head branch")
            args = ["pr", "create", "--title", title, "--body", body, "--base", base, "--head", head]
            if draft:
                args.append("--draft")
        elif number <= 0:
            raise HTTPException(400, "Select a pull request")
        elif action in ("checkout", "close", "reopen", "ready", "update-branch"):
            args = ["pr", action, str(number)]
        elif action in ("comment", "approve", "request-changes", "review-comment"):
            if action != "approve" and not body.strip():
                raise HTTPException(400, "Enter a comment")
            args = ["pr", "comment", str(number), "--body", body] if action == "comment" else ["pr", "review", str(number), "--" + ("comment" if action == "review-comment" else action), "--body", body]
        elif action == "edit":
            if not title.strip():
                raise HTTPException(400, "Enter a title")
            args = ["pr", "edit", str(number), "--title", title, "--body", body]
        elif action == "merge":
            if method not in ("squash", "merge", "rebase"):
                raise HTTPException(400, "Invalid merge method")
            args = ["pr", "merge", str(number), "--" + method]
        else:
            raise HTTPException(400, "Unknown pull request action")
        _, output, _ = await self.run(*args, program="gh")
        return {"message": output.strip()}

    async def action(self, action, paths=None, message="", branch="", target=""):
        if not await self.detect():
            raise HTTPException(400, "Not a Git repository")
        paths = [self.file_path(path) for path in (paths or [])]
        pathspecs = [f":(literal){path}" for path in paths]
        if action in ("stage", "unstage", "discard") and not paths:
            raise HTTPException(400, "Select files first")
        if action == "stage":
            args = ["add", "--", *pathspecs]
        elif action == "unstage":
            code, _, _ = await self.run("rev-parse", "--verify", "HEAD", check=False)
            args = ["rm", "--cached", "--", *pathspecs] if code else ["reset", "HEAD", "--", *pathspecs]
        elif action == "discard":
            # Restore tracked working-tree changes; never delete untracked files.
            args = ["restore", "--worktree", "--", *pathspecs]
        elif action == "commit":
            if not message.strip():
                raise HTTPException(400, "Enter a commit message")
            args = ["commit", "-m", message]
        elif action == "fetch":
            args = ["fetch"]
        elif action == "pull":
            args = ["pull", "--ff-only"]
        elif action == "push":
            args = ["push"]
        elif action == "publish":
            _, current, _ = await self.run("symbolic-ref", "--short", "HEAD")
            args = ["push", "--set-upstream", "origin", current.strip()]
        elif action in ("checkout", "branch"):
            if not branch or branch.startswith("-"):
                raise HTTPException(400, "Enter a branch name")
            await self.run("check-ref-format", "--branch", branch)
            if action == "branch":
                args = ["switch", "-c", branch]
            else:
                remote, _, _ = await self.run("show-ref", "--verify", f"refs/remotes/{branch}", check=False)
                args = ["switch", "--track", branch] if remote == 0 else ["switch", branch]
        elif action == "worktree":
            if not branch or branch.startswith("-") or not target:
                raise HTTPException(400, "Enter a branch and worktree directory")
            await self.run("check-ref-format", "--branch", branch)
            destination = self.checked_path(self.fs.resolve_path(target, cwd=self.root))
            args = ["worktree", "add", "-b", branch, destination]
        elif action == "rename_branch":
            if not branch or not target or branch.startswith("-") or target.startswith("-"):
                raise HTTPException(400, "Enter valid branch names")
            await self.run("check-ref-format", "--branch", target)
            args = ["branch", "-m", branch, target]
        elif action == "delete_branch":
            if not branch or branch.startswith("-"):
                raise HTTPException(400, "Enter a branch name")
            args = ["branch", "-d", branch]
        elif action == "uncommit":
            args = ["reset", "--soft", "HEAD~1"]
        elif action == "stash":
            args = ["stash", "push", "-u"]
            if message.strip():
                args += ["-m", message]
        elif action == "unstash":
            args = ["stash", "pop"]
        else:
            raise HTTPException(400, "Unknown Git action")
        _, output, _ = await self.run(*args)
        return {"message": output.strip(), **({"path": destination} if action == "worktree" else {})}


if __name__ == "__main__":
    # Count untracked files in one bounded worker under the terminal user's identity.
    counts = {}
    for path in json.load(sys.stdin):
        try:
            if os.path.islink(path):
                data = os.readlink(path).encode()
                counts[path] = data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))
                continue
            count, last, binary = 0, b"", False
            with open(path, "rb") as source:
                while chunk := source.read(65536):
                    if b"\0" in chunk:
                        binary = True
                        break
                    count += chunk.count(b"\n")
                    last = chunk[-1:]
            counts[path] = None if binary else count + int(bool(last) and last != b"\n")
        except OSError:
            counts[path] = None
    json.dump(counts, sys.stdout)
