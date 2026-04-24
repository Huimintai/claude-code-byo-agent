"""
Claude Code BYO Agent Bridge - A2A compatible server for kagent.
Routes kagent A2A requests to Claude Code CLI via subprocess.
"""
import os
import json
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("claude-code-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

MCP_CONFIG_PATH = "/tmp/mcp-config.json"


def _write_mcp_config():
    """Write MCP server config for Claude Code CLI at startup."""
    github_token = os.getenv("GITHUB_TOKEN", "")
    config = {
        "mcpServers": {
            "kubectl": {
                "type": "http",
                "url": "http://kubectl-mcp-server.dbci-agent:8000/mcp"
            },
            "github": {
                "type": "http",
                "url": "http://github-mcp-server.dbci-agent:8082/mcp",
                **({"headers": {"Authorization": f"Bearer {github_token}"}} if github_token else {})
            },
            "dod": {
                "type": "http",
                "url": "http://dod-mcp-server.dbci-agent:3000/mcp"
            },
            "landscape-pipeline": {
                "type": "http",
                "url": "http://landscape-pipeline-mcp-server.dbci-agent:3000/mcp"
            }
        }
    }
    with open(MCP_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    logger.info(f"MCP config written to {MCP_CONFIG_PATH} (github_token={'set' if github_token else 'not set'})")


# CI/CD credentials availability (logged at startup, injected per-request)
_CI_ENV_KEYS = [
    "GITLAB_URL", "GITLAB_TOKEN",
    "JENKINS_URL", "JENKINS_USER", "JENKINS_TOKEN",
    "HANA_HOST", "HANA_PORT", "HANA_USER", "HANA_PASSWORD",
    "JIRA_URL", "JIRA_TOKEN",
]


def _ci_skills_section() -> str:
    """Build the CI/CD skills section of the system prompt based on available env vars."""
    gitlab_url = os.getenv("GITLAB_URL", "")
    jenkins_url = os.getenv("JENKINS_URL", "")
    hana_host = os.getenv("HANA_HOST", "")
    jira_url = os.getenv("JIRA_URL", "")

    sections = []

    if gitlab_url:
        sections.append(f"""## GitLab CI/CD
GITLAB_URL is set to: {gitlab_url}
Use `curl` with the GITLAB_TOKEN header for all GitLab API calls:
- List pipelines:   curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_URL/api/v4/projects/{{id}}/pipelines?ref=main&per_page=5"
- Get pipeline:     curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_URL/api/v4/projects/{{id}}/pipelines/{{pipeline_id}}"
- List jobs:        curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_URL/api/v4/projects/{{id}}/pipelines/{{pipeline_id}}/jobs"
- Job log:          curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_URL/api/v4/projects/{{id}}/jobs/{{job_id}}/trace" | tail -c 10000
- Search projects:  curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_URL/api/v4/projects?search={{name}}&membership=true"
- MR diff:          curl -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_URL/api/v4/projects/{{id}}/merge_requests/{{mr_iid}}/diffs"
- Retry pipeline:   curl -X POST -H "PRIVATE-TOKEN: $GITLAB_TOKEN" "$GITLAB_URL/api/v4/projects/{{id}}/pipelines/{{pipeline_id}}/retry"
Tips: Project IDs can be found via search or URL-encode the path (namespace%2Fproject).
      Always use `tail -c 10000` or `head` to limit large job log output.
      When reporting failures include: project name, branch, failed stage, last 50 lines of job log.""")

    if jenkins_url:
        sections.append(f"""## Jenkins CI/CD
JENKINS_URL is set to: {jenkins_url}
Use `curl` with Basic Auth (JENKINS_USER:JENKINS_TOKEN) for all Jenkins API calls:
- List jobs:        curl -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/api/json?tree=jobs[name,url,color]"
- Build status:     curl -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/job/{{job}}/lastBuild/api/json"
- Console log:      curl -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/job/{{job}}/lastBuild/consoleText" | tail -200
- Trigger build:    curl -X POST -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/job/{{job}}/build"
- Build history:    curl -u "$JENKINS_USER:$JENKINS_TOKEN" "$JENKINS_URL/job/{{job}}/api/json?tree=builds[number,result,timestamp,duration]{{,10}}"
Tips: Use `wcrumb` if CSRF protection is enabled. Console logs can be huge — always tail.""")

    if hana_host:
        sections.append("""## SAP HANA Cloud Pipeline Statistics
Use python3 with hdbcli to query the HANA pipeline database:

```python
import os
from hdbcli import dbapi
import pandas as pd

conn = dbapi.connect(
    address=os.environ['HANA_HOST'],
    port=int(os.environ['HANA_PORT']),
    user=os.environ['HANA_USER'],
    password=os.environ['HANA_PASSWORD'],
    encrypt=True,
    sslValidateCertificate=False
)
cursor = conn.cursor()
# execute queries...
conn.close()  # Always close after use
```

Schema (4-level hierarchy): PIPELINE_RUN → PIPELINE_STAGE → PIPELINE_JOB → PIPELINE_TASK
Key tables: PIPELINE_RUN (~705K rows), PIPELINE_STAGE (~5.2M), PIPELINE_JOB (~5.5M), PIPELINE_TASK (~77M — MUST use WHERE+LIMIT)
Analysis tables: PIPELINE_ERROR_ANALYSIS, FAILED_DEPLOY_JOBS_ERROR_LOG, ADO_BUILD_TIMELINE
Always prefix tables with DBADMIN. (e.g. DBADMIN.PIPELINE_RUN)
Use HANA functions: ADD_DAYS(), SECONDS_BETWEEN(), WEEK(), DAYS_BETWEEN(), TO_DATE()
Use pandas + to_markdown(index=False) for tabular output.

Key baselines: PR failure rate ~48%, Deployment ~6.6%, Integration Tests cause 91% of zombie pipelines.""")

    if not sections:
        return ""

    return "\n\n# CI/CD Skills\n" + "\n\n".join(sections)


def _monthly_report_skill() -> str:
    """Return the monthly HC01 deployment error report skill if Jira is configured."""
    jira_url = os.getenv("JIRA_URL", "")
    if not jira_url:
        return ""
    return f"""

# Monthly HC01 Deployment Error Report Skill

When asked to generate a monthly deployment error report (e.g. "generate March 2026 report",
"last month's deployment errors", "HC01 monthly report"), follow these steps using Bash + curl.

JIRA_URL={jira_url}
Use `Authorization: Bearer $JIRA_TOKEN` for all Jira API calls.

## Step 1 — Resolve the target month
Determine YYYY-MM-01 and YYYY-MM-LD (last day). Default to previous calendar month if unspecified.

## Step 2 — Fetch all tickets via JQL (paginate in batches of 100)
```bash
curl -s -X POST "$JIRA_URL/rest/api/2/search" \\
  -H "Authorization: Bearer $JIRA_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{{"jql":"project = HC01 AND labels = \\"Deployment-Error\\" AND summary ~ \\"Deployment issue\\" AND created >= \\"YYYY-MM-01\\" AND created <= \\"YYYY-MM-LD\\" ORDER BY created ASC","maxResults":100,"startAt":0,"fields":["summary","status","created"]}}'
```
Repeat with startAt=100, 200, ... until startAt >= total.
Extract per ticket: key, landscape (from summary "Deployment issue in X" → X), status, created.

## Step 3 — Fetch first comment for each ticket (extract error summary)
```bash
curl -s "$JIRA_URL/rest/api/2/issue/HC01-XXXXXX/comment?maxResults=1" \\
  -H "Authorization: Bearer $JIRA_TOKEN"
```
From the first comment body, extract the text between {{code:java}} and {{code}} after "*Error Summary*".
Process tickets in batches — write a short python3 script to /tmp/classify.py if needed to handle
JSON parsing and classification efficiently. Do NOT fetch comments one-by-one interactively —
use a bash loop or python script to batch process all tickets.

## Step 4 — Classify each ticket into an error category
Use keyword matching (first match wins) on the error_summary:
- "machine image" / "unsupported" / version numbers like "1887" → Unsupported Machine Image Version
- "etcd" / "database space" / "exceeded" / "etcdserver" → etcd Disk Full
- "invalidclienterror" / "client authentication failure" → InvalidClientError (Auth Failure)
- "vault" + ("400" or "403" or "log in") → Vault Login Failure
- "kyverno" / "mutate-policy" → Kyverno Webhook Failure
- "ssl" / "unexpected eof" / "bad certificate" / "maxretryerror" → SSL/TLS Certificate Error
- "forbidden" / "403" / "serviceaccount" / "rbac" → 403 Forbidden / RBAC Error
- "worker resource" / "machinedeployment" / "not updated" → Worker/Machine Deployment Timeout
- "context deadline exceeded" → Context Deadline Exceeded
- "name resolution" / "nameresolutionerror" → DNS / Name Resolution Error
- "seed" + ("not ready" or "unhealthy") → Seed Not Ready / Unhealthy
- "common.py" / "wrapper function" → hc-tool Script Error
- "timeouterror" / "reconciliation" / "time limit" → Generic Reconciliation Timeout
- "deploy_landscape" / "configuration parameters" → Deploy Landscape Config Error
- "non-zero exit" / "kubectl apply" → kubectl Apply Non-zero Exit
- "admission webhook" / "immutable" → Admission Webhook Immutable Field
- "service unavailable" / "503" → Service Unavailable (503)
- empty error_summary → (No error info available)
- anything else → Other / Uncategorized

## Step 5 — Identify mass incidents
Group tickets by category. A mass incident = 5+ tickets in the same category within a 48h window.

## Step 6 — Render markdown report
Output:
```
# Deployment Error Report — [Month YYYY]
**Total tickets:** N  |  **Date range:** YYYY-MM-01 – YYYY-MM-LD

## Error Category Summary
| # | Error Category | Tickets | Unique Landscapes | Potential Improvement |
...

## Mass Incidents (if any)
...

## Top Recurring Landscapes
...
```
"""

SYSTEM_PROMPT = os.getenv("CLAUDE_SYSTEM_PROMPT", """You are a Kubernetes and CI/CD operations agent for SAP HANA Cloud environments.
You have kubectl configured with multiple cluster contexts.
Use Bash tool to run kubectl commands directly.

Available clusters:
- shoot--hc-can-ac--prod-haas (Canary HaaS - default)
- shoot--hc-can-ac--prod-hdl (Canary HDL)
- shoot--hc-can-ac--prod-orc (Canary ORC)
- shoot--hc-dev--demo-ac-haas (Dev HaaS)
- shoot--hc-dev--demo-ac-hdl (Dev HDL)
- shoot--hc-dev--demo-ac-orc (Dev ORC)

Rules:
1. Always run `kubectl config current-context` first to confirm which cluster you're on.
2. Use `kubectl config use-context <name>` to switch clusters.
3. Prefer read-only operations (get, describe, logs). Never delete or modify resources.
4. Be explicit about which cluster and namespace each result comes from.
5. For long-running operations (git clone, large downloads, builds), always run them in the
   background using nohup and redirect output to a log file, then return immediately with
   status. Example:
     nohup git clone <url> /path/to/dir > /tmp/clone.log 2>&1 &
     echo "Clone started (PID $!). Monitor with: tail -f /tmp/clone.log"
   Never block waiting for slow network operations.
6. Before analyzing a repository, ALWAYS check first if it exists locally with `ls /tmp/<repo-name>`.
   - If it EXISTS: proceed with analysis directly using the local files.
   - If it does NOT exist: start a background clone ONLY, then immediately return a message
     telling the user the clone has started and to ask again in ~2 minutes once clone completes.
     Do NOT attempt to analyze in the same request as the clone.
     IMPORTANT: Always clone in background using nohup:
       nohup git clone <url> <dir> > /tmp/<repo>-clone.log 2>&1 &
     Example response: "Cloning kubectl-mcp-server in background (PID X). Please ask me again
     in ~2 minutes to analyze the architecture once the clone is complete."
7. For code analysis tasks, be concise and focused. Analyze the most important files first
   (README, main entry points, core modules). Do not read every file — sample representative
   ones to form a complete picture efficiently.

Available MCP tools (prefer over raw bash when applicable):
- kubectl MCP: K8s operations (get_pods, describe_pod, get_logs, etc.)
- github MCP: GitHub (list_pull_requests, get_file_contents, search_code, create_pull_request, etc.)
- dod MCP: Jira tickets (analysis_ticket, search_tickets)
- landscape-pipeline MCP: CI/CD analysis (get_pipeline_info, get_suggestion, list_pipeline_logs)
""") + _ci_skills_section() + _monthly_report_skill()

AGENT_NAME = os.getenv("AGENT_NAME", "claude_code_k8s_agent")
AGENT_DESCRIPTION = os.getenv("AGENT_DESCRIPTION", "K8s and CI/CD operations agent powered by Claude Code")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Claude Code Bridge starting...")
    _write_mcp_config()
    logger.info(f"ANTHROPIC_BASE_URL: {os.getenv('ANTHROPIC_BASE_URL', 'NOT SET')}")
    # Log which CI/CD integrations are configured
    configured = [k for k in _CI_ENV_KEYS if os.getenv(k)]
    logger.info(f"CI/CD credentials configured: {configured if configured else 'none'}")
    # Verify claude CLI is available
    proc = await asyncio.create_subprocess_exec(
        "claude", "--version",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    logger.info(f"Claude Code CLI: {stdout.decode().strip()}")
    yield
    logger.info("Claude Code Bridge shutting down.")


app = FastAPI(lifespan=lifespan)


@app.get("/.well-known/agent-card.json")
async def agent_card():
    """A2A agent discovery endpoint."""
    return {
        "name": AGENT_NAME,
        "description": AGENT_DESCRIPTION,
        "url": f"http://localhost:8080",
        "capabilities": {"streaming": True, "pushNotifications": False},
        "skills": [
            {
                "id": "k8s-operations",
                "name": "Kubernetes Operations",
                "description": "Execute kubectl commands across multiple clusters via Claude Code",
            },
            {
                "id": "cicd-operations",
                "name": "CI/CD Operations",
                "description": "Query GitLab, Jenkins pipelines and SAP HANA pipeline statistics",
            }
        ],
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
    }


@app.post("/")
async def a2a_root(request: Request):
    """A2A JSON-RPC root endpoint — dispatches by method field."""
    body = await request.json()
    method = body.get("method", "")
    if method in ("message/stream", "tasks/resubscribe"):
        return await _handle_stream(body)
    return await _handle_send(body)


@app.post("/send-message")
async def send_message(request: Request):
    """Legacy send-message endpoint."""
    body = await request.json()
    return await _handle_send(body)


@app.post("/send-message-stream")
async def send_message_stream(request: Request):
    """Legacy streaming endpoint."""
    body = await request.json()
    return await _handle_stream(body)


async def _handle_send(body: dict):
    """Handle message/send — returns JSON response."""
    user_message = _extract_text(body)
    task_id = body.get("params", {}).get("taskId") or str(uuid.uuid4())
    context_id = body.get("params", {}).get("contextId") or task_id

    logger.info(f"Received message (task={task_id}): {user_message[:100]}...")

    try:
        result_text = await run_claude_code(user_message)
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "kind": "task",
                "id": task_id,
                "contextId": context_id,
                "status": {"state": "completed"},
                "artifacts": [
                    {
                        "parts": [{"kind": "text", "text": result_text}],
                    }
                ],
            },
        })
    except Exception as e:
        logger.error(f"Claude Code execution failed: {e}")
        return JSONResponse(content={
            "jsonrpc": "2.0",
            "id": body.get("id"),
            "result": {
                "id": task_id,
                "status": {"state": "failed", "message": str(e)},
                "artifacts": [],
            },
        })


async def _handle_stream(body: dict):
    """Handle message/stream — returns SSE response."""
    user_message = _extract_text(body)
    task_id = body.get("params", {}).get("taskId") or str(uuid.uuid4())
    context_id = body.get("params", {}).get("contextId") or task_id

    logger.info(f"Received streaming message (task={task_id}): {user_message[:100]}...")

    async def event_stream():
        # Send working status
        yield _sse_event({
            "kind": "status-update",
            "taskId": task_id,
            "contextId": context_id,
            "status": {"state": "working", "message": {"role": "agent", "parts": [{"kind": "text", "text": "Running Claude Code..."}]}},
            "final": False,
        })

        try:
            result_text = await run_claude_code(user_message)
            last_chunk = True
            # Send artifact
            yield _sse_event({
                "kind": "artifact-update",
                "taskId": task_id,
                "contextId": context_id,
                "artifact": {
                    "artifactId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": result_text}],
                },
                "lastChunk": last_chunk,
                "append": False,
            })
            # Send completed (final)
            yield _sse_event({
                "kind": "status-update",
                "taskId": task_id,
                "contextId": context_id,
                "status": {"state": "completed"},
                "final": True,
            })
        except Exception as e:
            yield _sse_event({
                "kind": "status-update",
                "taskId": task_id,
                "contextId": context_id,
                "status": {"state": "failed", "message": {"role": "agent", "parts": [{"kind": "text", "text": str(e)}]}},
                "final": True,
            })

    return StreamingResponse(event_stream(), media_type="text/event-stream")


CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "550"))  # 50s before kagent's 600s streaming-timeout


async def run_claude_code(prompt: str) -> str:
    """Run Claude Code CLI as subprocess and return the result text."""
    cmd = [
        "claude",
        "--print",  # Non-interactive, output result only
        "--output-format", "text",
        "--max-turns", "50",
        "--model", CLAUDE_MODEL,
        "--system-prompt", SYSTEM_PROMPT,
        "--mcp-config", MCP_CONFIG_PATH,
        "--allowedTools", "Bash,Read,Glob,Grep,mcp__kubectl,mcp__github,mcp__dod,mcp__landscape-pipeline",
        "--permission-mode", "bypassPermissions",
        prompt,
    ]

    env = {**os.environ}
    # Ensure API routing through sap-ai-proxy
    if "ANTHROPIC_BASE_URL" not in env:
        logger.warning("ANTHROPIC_BASE_URL not set! Claude Code may use default Anthropic API.")

    logger.info(f"Executing Claude Code: prompt={prompt[:80]}...")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.warning(f"Claude Code timed out after {CLAUDE_TIMEOUT}s, process killed")
        return (
            f"The operation is taking longer than {CLAUDE_TIMEOUT} seconds. "
            "For long-running tasks like git clone or large downloads, the background process "
            "may still be running in the container. You can check its status by sending another "
            "message, e.g. 'check if the git clone is still running with: ps aux | grep git' "
            "or 'tail -f /tmp/clone.log'."
        )

    if proc.returncode != 0:
        error_msg = stderr.decode().strip()
        logger.error(f"Claude Code failed (rc={proc.returncode}): {error_msg}")
        raise RuntimeError(f"Claude Code exited with code {proc.returncode}: {error_msg}")

    result = stdout.decode().strip()
    logger.info(f"Claude Code result: {result[:200]}...")
    return result


def _extract_text(body: dict) -> str:
    """Extract text content from A2A message format."""
    params = body.get("params", {})
    message = params.get("message", {})
    parts = message.get("parts", [])
    texts = [p.get("text", "") for p in parts if p.get("kind") == "text" or p.get("type") == "text"]
    return " ".join(texts).strip() or params.get("prompt", "")


def _sse_event(data: dict) -> str:
    """Format as SSE event."""
    return f"data: {json.dumps(data)}\n\n"


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
