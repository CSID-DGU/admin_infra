cd ~/admin_infra
helm upgrade containerssh ./containerssh -n containerssh
kubectl rollout restart deployment containerssh -n containerssh

kubectl get pods -n containerssh
