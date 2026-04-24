# Claude Code BYO Agent

A [kagent](https://kagent.dev) **Bring Your Own (BYO) Agent** that wraps the [Claude Code CLI](https://github.com/anthropics/claude-code) as an A2A-compatible server. It routes natural language requests from kagent to Claude Code, giving the agent full CLI capability for Kubernetes operations, CI/CD pipelines, and SAP HANA analytics — without needing to configure MCP tools in kagent itself.

## Architecture

```
kagent UI / CronJob
       │  A2A JSON-RPC
       ▼
 bridge/server.py  (FastAPI, port 8080)
       │  subprocess
       ▼
  Claude Code CLI
       │  MCP (HTTP)
       ├──► kubectl MCP server
       ├──► GitHub MCP server
       ├──► DoD MCP server
       └──► Landscape-Pipeline MCP server
```

The bridge exposes two A2A endpoints:
- `POST /` — dispatches `message/send` (JSON) or `message/stream` (SSE) based on the `method` field
- `GET /.well-known/agent-card.json` — agent discovery

## Features

- **Kubernetes operations** — multi-cluster `kubectl` via MCP or direct Bash
- **GitLab CI/CD** — query pipelines, jobs, logs, merge requests via GitLab API
- **Jenkins CI/CD** — build status, console logs, trigger builds
- **SAP HANA analytics** — query pipeline statistics database via `hdbcli` + pandas
- **GitHub** — PRs, file contents, code search via GitHub MCP
- **Scheduled reports** — CronJob sends periodic pipeline health reports to the agent

## Repository Structure

```
├── bridge/
│   ├── __init__.py
│   └── server.py        # FastAPI A2A bridge server
├── Dockerfile           # Container image build
├── requirements.txt     # Python dependencies
├── k8s-deploy.yaml      # Full K8s deployment (Secret + sap-ai-proxy + Agent CRD)
└── cronjob.yaml         # Scheduled pipeline report CronJob
```

## Prerequisites

- Kubernetes cluster with [kagent](https://kagent.dev) installed
- [SAP AI Core](https://help.sap.com/docs/ai-core) instance (for Claude model access via `sap-ai-proxy`)
- The following K8s secrets pre-created in the target namespace:
  - `github-mcp-pat` — GitHub Personal Access Token
  - `bastion-ci-credentials` — GitLab, Jenkins, and HANA connection details

## Deployment

### 1. Fill in credentials

Edit `k8s-deploy.yaml` and replace the placeholders in the `sap-ai-proxy-secret` section:

```yaml
stringData:
  AICORE_CLIENT_ID: "<YOUR_AICORE_CLIENT_ID>"
  AICORE_CLIENT_SECRET: "<YOUR_AICORE_CLIENT_SECRET>"
  AICORE_AUTH_URL: "<YOUR_AICORE_AUTH_URL>"
  AICORE_BASE_URL: "<YOUR_AICORE_BASE_URL>"
```

### 2. Deploy

```bash
# Create namespace if needed
kubectl create namespace dbci-agent

# Deploy everything (Secret + sap-ai-proxy + Agent CRD)
kubectl apply -f k8s-deploy.yaml -n dbci-agent

# (Optional) Deploy scheduled pipeline report
kubectl apply -f cronjob.yaml -n dbci-agent
```

### 3. Build and push the agent image

```bash
docker build -t <your-registry>/claude-code-byo-agent:<tag> .
docker push <your-registry>/claude-code-byo-agent:<tag>
```

Then update the `image` field in `k8s-deploy.yaml` accordingly.

## Configuration

The agent behavior is controlled by environment variables set in `k8s-deploy.yaml`:

| Variable | Description |
|---|---|
| `ANTHROPIC_BASE_URL` | Proxy URL for Claude API (e.g. `sap-ai-proxy` in-cluster) |
| `ANTHROPIC_API_KEY` | API key (not needed when using `sap-ai-proxy`) |
| `CLAUDE_MODEL` | Claude model ID to use |
| `AGENT_NAME` | Agent name shown in kagent UI |
| `AGENT_DESCRIPTION` | Agent description |
| `PORT` | Server port (default: `8080`) |
| `CLAUDE_TIMEOUT` | Max seconds per Claude Code invocation (default: `550`) |
| `GITHUB_TOKEN` | GitHub PAT for MCP GitHub server |
| `GITLAB_URL` / `GITLAB_TOKEN` | GitLab instance credentials |
| `JENKINS_URL` / `JENKINS_USER` / `JENKINS_TOKEN` | Jenkins credentials (optional) |
| `HANA_HOST` / `HANA_PORT` / `HANA_USER` / `HANA_PASSWORD` | SAP HANA connection details |

## Local Development

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key
export PORT=8080
python -m bridge.server
```

The server starts on `http://localhost:8080`. Send an A2A request:

```bash
curl -X POST http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "1",
    "method": "message/send",
    "params": {
      "message": {
        "role": "user",
        "parts": [{"kind": "text", "text": "List all pods in the default namespace"}]
      }
    }
  }'
```
