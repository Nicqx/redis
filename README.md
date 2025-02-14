# redis
host path: /home/user/redis/data

kubectl apply -f redis-pv.yaml
kubectl apply -f redis-pvc.yaml
kubectl apply -f redis-config.yaml
kubectl apply -f redis-deployment.yaml
kubectl apply -f redis-service.yaml

kubectl rollout restart deployment/redis

módosításnál kubectl delete

ha beragad a pvc:

kubectl patch pvc redis-pvc -p '{"metadata":{"finalizers":null}}' --type=merge
