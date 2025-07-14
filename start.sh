
# containerssh
cd ~/admin_infra/containerssh
kubectl apply -f k8s/ -n containerssh


# auth server
## rebuild
cd ~/admin_infra/containerssh-auth-server
docker build -t containerssh-auth-server:latest .

## apply k8s
kubectl apply -f k8s/ -n containerssh
kubectl rollout restart deployment containerssh-auth-server -n containerssh


