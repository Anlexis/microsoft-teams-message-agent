# Microsoft Teams Messaging Agent

AI agent for sending and managing Microsoft Teams messages, built with Agentic Star.

> **Category**: Cat 2 (multi-step domain workflow — tool-calling pipeline)
> **Industry**: Common
> **Template ID**: CMN-C2-234

## Overview

Posts Microsoft Teams channel and chat messages from natural-language requests. A deterministic five-step workflow validates the request, classifies the intent (channel post vs. 1:1/group chat post), extracts the message body and the explicit target ids, assembles the Microsoft Graph API v1.0 `chatMessage` call (`POST /teams/{team-id}/channels/{channel-id}/messages`, `POST /chats/{chat-id}/messages`), and returns a human-readable confirmation with the posted message's id and link. Write safety is explicit-only: a target and a message body are taken only from what the caller named — an unresolved target or body is an error, never a guess. The bundled Teams client ships a deterministic, network-free stub transport by default, so the pipeline runs and tests without a live Microsoft 365 tenant; live Graph delivery is a construction-time transport injection with no business-logic change.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. The agent imports its base classes from the framework package at start-up, so without that
package installed and configured, import and graph compile fail outright rather than leaving the
agent running in a partially working state. This is intentional — a half-running agent is worse
than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
