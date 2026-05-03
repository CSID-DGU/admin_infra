cd ~/admin_infra
helm upgrade --install pvc-test-jy ./pvc-chart -n cssh

kubectl get pvc -n cssh
kubectl get pv | grep -E 'pvc-jy-share|nfs-user-storage' || kubectl get pv
