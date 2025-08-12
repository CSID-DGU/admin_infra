from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime
import subprocess

from db import BackgroundJob, get_db

router = APIRouter()

# 세션 종료 시에 실행
@router.post("/session-end")
def session_end(username: str, pod_name: str, has_background: bool, db: Session = Depends(get_db)):
    job = db.query(BackgroundJob).filter_by(username=username, pod_name=pod_name).first()
    if not job:
        job = BackgroundJob(username=username, pod_name=pod_name, has_background=has_background)
        db.add(job)
    else:
        job.has_background = has_background
        job.last_checked = datetime.utcnow()
    db.commit()
    return {"status": "recorded", "has_background": has_background}


# 주기적으로 백그라운드 작업이 남아있는지 확인 및 삭제
@router.post("/cron-check")
def cron_check(db: Session = Depends(get_db)):
    jobs = db.query(BackgroundJob).filter(BackgroundJob.has_background == True).all()
    deleted_pods = []
    for job in jobs:
        cmd = ["kubectl", "exec", job.pod_name, "--", "ps", "-eo", "user,cmd"]
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT).decode()
        except subprocess.CalledProcessError:
            continue  # Pod가 이미 사라진 경우

        user_procs = [line for line in output.splitlines() if job.username in line and "bash" not in line and "grep" not in line]

        if not user_procs:  # 백그라운드 없는 경우, Pod 삭제
            subprocess.run(["kubectl", "delete", "pod", job.pod_name])
            db.delete(job)
            deleted_pods.append(job.pod_name)
    db.commit()
    return {"deleted_pods": deleted_pods}

