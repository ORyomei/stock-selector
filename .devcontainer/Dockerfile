FROM ubuntu:24.04

RUN apt-get update && apt-get install -y \
    git curl wget ca-certificates sudo \
    python3 python3-pip python3-venv \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu \
    && chmod 0440 /etc/sudoers.d/ubuntu

# GitHub CLI
RUN mkdir -p -m 755 /etc/apt/keyrings \
    && wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg \
       | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
       | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update && apt-get install -y gh \
    && rm -rf /var/lib/apt/lists/*

# uv
RUN curl -LsSf https://astral.sh/uv/install.sh | sudo -u ubuntu sh
ENV PATH="/home/ubuntu/.local/bin:${PATH}"

# Python 依存パッケージ（uv で管理）
COPY pyproject.toml /tmp/pyproject.toml
RUN cd /tmp \
    && sudo -u ubuntu /home/ubuntu/.local/bin/uv venv /home/ubuntu/.stock-venv \
    && sudo -u ubuntu /home/ubuntu/.local/bin/uv pip install --python /home/ubuntu/.stock-venv/bin/python -r /tmp/pyproject.toml \
    && rm /tmp/pyproject.toml
