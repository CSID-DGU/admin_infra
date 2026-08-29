FROM python:3.10-slim

WORKDIR /app
COPY . .

RUN apt-get update && apt-get install -y --no-install-recommends \
    krb5-user \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir -r requirements.txt

# --graceful-timeout: SIGTERM(롤링 업데이트 등) 시 진행 중인 요청을 마칠 때까지 기다리는
# 시간. 기본값 30초는 /create-pod가 pod ready를 최대 500초까지 기다리는 것보다 훨씬 짧아서,
# 배포할 때마다 진행 중인 Pod 생성이 중간에 끊겼다. deployment.yaml의
# terminationGracePeriodSeconds(630)보다는 작게 잡아 k8s가 SIGKILL하기 전에 gunicorn이
# 먼저 정상 종료할 기회를 준다.
CMD ["gunicorn", "--workers=4", "--bind=0.0.0.0:8000", "--timeout", "700", "--graceful-timeout", "600", "main:app"]

