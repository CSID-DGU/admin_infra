FROM containerssh/agent AS agent

FROM ubuntu:22.04

# Install bash (기본 쉘로 사용하려면 bash 설치 필요)
RUN apt-get update && apt-get install -y \
    bash \
    && rm -rf /var/lib/apt/lists/*

# Copy ContainerSSH Agent
COPY --from=agent /usr/bin/containerssh-agent /usr/bin/containerssh-agent
