# Security Policy

Orchivio is **pre-alpha** and not ready for production use or for handling real data.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability
reporting ("Security" tab → "Report a vulnerability") on this repository.

> **Maintainer action required:** enable private vulnerability reporting for the repository and,
> if desired, add a contact address here. No other reporting channel is currently monitored.

Include the affected version or commit, steps to reproduce, and the impact you observed.

## What to expect

This project does not yet have a staffed response team. Reports are handled on a best-effort
basis; we aim to acknowledge a report within a few days and will coordinate disclosure with the
reporter. Response times will be tightened before the first public release.

## Scope

In scope: the code in this repository (model validation, expression engine, access resolution,
persistence, API). Out of scope: third-party services and the security of your own deployment
environment.

Design-level security requirements and known gaps are tracked in
[docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) and [docs/STATUS.md](docs/STATUS.md).
