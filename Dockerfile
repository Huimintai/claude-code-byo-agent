FROM python:3.12-slim

# System dependencies
RUN apt-get update && apt-get install -y \
    curl git ca-certificates jq xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install kubectl
RUN ARCH=$(dpkg --print-architecture) && \
    curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/${ARCH}/kubectl" \
    && chmod +x kubectl && mv kubectl /usr/local/bin/

# Install Node.js 22 (needed for Claude Code CLI)
RUN ARCH=$(dpkg --print-architecture) && \
    if [ "$ARCH" = "arm64" ]; then NODE_ARCH="arm64"; else NODE_ARCH="x64"; fi && \
    curl -fsSL "https://nodejs.org/dist/v22.15.0/node-v22.15.0-linux-${NODE_ARCH}.tar.xz" -o /tmp/node.tar.xz && \
    tar -xf /tmp/node.tar.xz -C /usr/local --strip-components=1 && \
    rm /tmp/node.tar.xz

# Install Claude Code CLI via npm
RUN npm install -g @anthropic-ai/claude-code

# Python dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user (Claude Code CLI refuses --dangerously-skip-permissions as root)
RUN useradd -m -s /bin/bash agent && chown -R agent:agent /app

# Configure git to disable credential prompts (needed for non-TTY environments)
RUN git config --system credential.helper '' && \
    git config --system core.askPass '' && \
    printf '[credential]\n\thelper =\n[core]\n\taskPass =\n' > /etc/gitconfig
USER agent

# Application code
COPY --chown=agent:agent bridge/ ./bridge/

# A2A port
EXPOSE 8080

CMD ["python", "-m", "bridge.server"]
