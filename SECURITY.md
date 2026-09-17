# Security Policy

Open Terminal is led and maintained by the same core team as Open WebUI, and it follows the same security process.

## Relationship to the Open WebUI Security Policy

The [Open WebUI Security Policy](https://github.com/open-webui/open-webui/blob/main/docs/SECURITY.md) applies to Open Terminal in full wherever it is semantically applicable. That includes, and is not limited to: the rules for reporting a vulnerability, the proof-of-concept and remediation requirements, AI disclosure, CVSS accuracy, admin actions being out of scope, default configuration testing, already-fixed reports, self-affecting issues, duplicate and consolidation handling, our position on foreign CNAs and vendor disposition, and responsible disclosure.

Read that policy before filing. Everything in it that can sensibly be applied to Open Terminal is applied to Open Terminal. This document adds what is specific to this project, and where the two documents differ, this one governs reports filed here.

## Reporting Channel

Report through [GitHub Security Advisories on this repository](https://github.com/open-webui/open-terminal/security/advisories/new). Reports for Open Terminal code filed against `open-webui/open-webui` are closed there and have to be re-filed here, which costs you time, so file in the right place from the start.

## Supported Versions

Only the latest release is supported. Reproduce your finding against the latest release or against `main` before filing.

## What Open Terminal Is

Open Terminal runs commands on behalf of whoever holds its API key. Executing commands, reading and writing files, installing packages, and reaching the network from inside the terminal are the product working as designed. The following therefore are not vulnerabilities, and reports about them will be closed:

1. **Command execution through the API.** Running arbitrary commands is the entire purpose of this software.
2. **The API key grants everything.** The key is a single full-access credential. Anyone holding it can run any command, read any file the service can reach, and use every endpoint. Treat it like a root password, and never hand it to users you would not give a shell to.
3. **`X-User-Id` is asserted by the caller.** Open Terminal does not authenticate end users. The calling application (for example Open WebUI) authenticates its users and sets the header, and Open Terminal trusts it. A client that holds the API key can name any user it likes, by design.
4. **Root inside the container in images that ship `sudo`.** The `latest` image gives its default user passwordless `sudo` so that packages can be installed at runtime. Reaching root inside that container is documented behaviour. Use `slim`, `alpine`, or `openshift` when you do not want that.
5. **Bare metal runs as you.** Installed with `pip`, commands run as your own user with your own permissions and your own files. That is the point of the bare metal mode.
6. **Operator configuration.** Mounting the Docker socket, granting extra capabilities, disabling the egress firewall, or exposing the API to an untrusted network are operator decisions with documented consequences.

## Multi-User Mode

`OPEN_TERMINAL_MULTI_USER=true` gives each user their own Linux account and home directory inside one shared container. It separates workspaces so that users and their agents do not walk over each other's files. It does not isolate users from each other, and it is not designed for production multi-user deployments.

All users share one kernel, one process list, one network stack, and one temp directory, and the service that provisions their accounts has to hold elevated privileges inside that container. In that shape, the following are expected consequences of running many users in one container, and are out of scope:

- One user reading, observing, or interfering with another user's processes, command lines, ports, or resources.
- Privilege escalation to root inside the container.
- Cross-user access to files, sessions, terminals, notebooks, or background processes in a single instance.
- Resource exhaustion affecting other users of the same instance.

If your users need to be protected from each other, run one Open Terminal per user, for example with [Terminals](https://github.com/open-webui/terminals), which provisions a separate container per user and requires an [Open WebUI Enterprise License](https://openwebui.com/enterprise) for production use. Reports asking us to defend a boundary inside a single shared container are closed as documented behaviour under this section.

## What We Do Want to Hear About

Findings that cross out of the instance or out of the API's own trust model are in scope, including:

- Acting on the API without a valid API key, or any bypass of the key check.
- Escaping the container onto the host in a default Docker deployment.
- Anything that lets terminal content or terminal responses run with the privileges of the calling application, for example executing at the client application's origin or reaching its credentials.
- Path traversal or file access that reaches outside what the API is meant to expose in a single-user deployment.
- Anything that affects the operator, the host, or a party other than the reporter.

When in doubt, file it. A report that lands outside the scope above is closed quickly and without drama, and we would rather read one of those than miss something real.

---

_Last updated on **2026-09-17**._
