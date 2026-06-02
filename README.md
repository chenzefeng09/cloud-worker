# Agent Platform — Phase B, Milestone 1

Enterprise Agent Business Platform, built on the saas-foundation SDK.

## Quick Start

```bash
# 1. Start foundation stack
make foundation-up

# 2. Start agent platform services
make agent-up

# 3. Run migrations
make migrate

# 4. Start frontend
make web-install
make web-dev
```

Open http://localhost:5173 in your browser.

## Architecture

```
Browser → BFF (8005) → Session Orchestrator (8006) → Agent Scheduler (8007)
                                                           ↓
                                                  Sandbox (Docker/Cube)
                                                           ↓
                                                  AgentScope worker
```

## Services

| Service | Port | Description |
|---|---|---|
| bff | 8005 | Backend For Frontend — auth proxy, API aggregation, SSE relay |
| session-orchestrator | 8006 | Session and message lifecycle management |
| agent-scheduler | 8007 | Spawns sandbox containers/VMs running the AgentScope worker |
| sandbox-pool | 8008 | Pre-warmed CubeSandbox microVM pool (B-M2) |

## Development

```bash
make lint        # ruff + mypy
make test        # pytest with coverage
make conformance # run conformance suite
```
