# routers/pvc.py
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from kubernetes import client, config

router = APIRouter()

class PVCRequest(BaseModel):
    username: str
    storage: str  # 예: '30Gi'

@router.post("/pvc")
def create_or_resize_pvc(req: PVCRequest):
    try:
        try:
            config.load_incluster_config()
        except:
            config.load_kube_config()

        core_v1 = client.CoreV1Api()
        pvc_name = f"pvc-{req.username}-share"
        pv_name = f"pv-{req.username}-share"
        namespace = "containerssh"

        # 이미 존재하는 pvc가 있는지 확인
        existing_pvc = None
        try:
            existing_pvc = core_v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise

        if existing_pvc:
            # PVC 크기 변경
            patch_body = {
                "spec": {
                    "resources": {
                        "requests": {
                            "storage": req.storage
                        }
                    }
                }
            }
            core_v1.patch_namespaced_persistent_volume_claim(pvc_name, namespace, patch_body)
            return {"status": "resized", "message": f"{pvc_name} resized to {req.storage}"}

        # 존재하는 PVC가 없으면, PV와 PVC 생성
        pv_body = client.V1PersistentVolume(
            metadata=client.V1ObjectMeta(name=pv_name),
            spec=client.V1PersistentVolumeSpec(
                capacity={"storage": req.storage},
                access_modes=["ReadWriteMany"],
                storage_class_name="nfs-nas-v3",
                persistent_volume_reclaim_policy="Retain",
                nfs=client.V1NFSVolumeSource(
                    server="100.100.100.120",
                    path=f"/volume1/share/{req.username}"
                ),
                mount_options=["vers=3"]
            )
        )
        pvc_body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=pvc_name),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteMany"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": req.storage}
                ),
                storage_class_name="nfs-nas-v3",
                volume_name=pv_name
            )
        )
        core_v1.create_persistent_volume(body=pv_body)
        core_v1.create_namespaced_persistent_volume_claim(namespace, pvc_body)
        return {"status": "created", "message": f"{pvc_name} created with {req.storage}"}

    except client.exceptions.ApiException as e:
        raise HTTPException(status_code=400, detail=f"K8s API Error: {e.body}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

