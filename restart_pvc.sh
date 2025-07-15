cd ~/admin_infra
helm upgrade pvc-test-jy ./pvc-chart -n containerssh

kubectl get pv -n containerssh
kubectl get pvc -n containerssh
