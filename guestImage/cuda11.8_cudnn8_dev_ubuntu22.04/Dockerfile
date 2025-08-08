FROM containerssh/agent AS agent

FROM nvidia/cuda:11.8.0-cudnn8-devel-ubuntu22.04

# Install bash (기본 쉘로 사용하려면 bash 설치 필요)
RUN apt-get update && apt-get install -y \
    bash wget curl net-tools fcitx-hangul fonts-nanum* vim software-properties-common sudo \
    && rm -rf /var/lib/apt/lists/* \
    && apt-key adv --keyserver keyserver.ubuntu.com --recv-keys A4B469963BF863CC

# Copy ContainerSSH Agent
COPY --from=agent /usr/bin/containerssh-agent /usr/bin/containerssh-agent

# 최신 Anaconda 버전 다운로드 및 설치 (2025.06-1)
RUN wget https://repo.anaconda.com/archive/Anaconda3-2025.06-1-Linux-x86_64.sh \
    && bash Anaconda3-2025.06-1-Linux-x86_64.sh -b -p /opt/anaconda3 \
    && rm Anaconda3-2025.06-1-Linux-x86_64.sh

# Anaconda 실행을 위한 PATH 설정
ENV PATH=/opt/anaconda3/bin:$PATH

# 로그인 환경에도 적용되도록 profile에 PATH 추가 및 conda init
RUN echo "export PATH=/opt/anaconda3/bin:\$PATH" >> /etc/profile \
    && /opt/anaconda3/bin/conda init

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["bash", "/entrypoint.sh"]
