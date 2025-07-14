# ContainerSSH Kubernetes Deployment Makefile
NAMESPACE = containerssh
KUBECTL = kubectl

.PHONY: help deploy clean status logs ssh-test generate-hostkey generate-modern-hostkey

help: ## Show this help message
	@echo "ContainerSSH Kubernetes Deployment"
	@echo ""
	@echo "Available commands:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

generate-hostkey: ## Generate SSH host key secret (RSA - legacy)
	@echo "Generating SSH host key (RSA)..."
	@openssl genrsa 2048 | $(KUBECTL) create secret generic containerssh-hostkey --from-file=host.key=/dev/stdin -n $(NAMESPACE) --dry-run=client -o yaml | $(KUBECTL) apply -f -
	@echo "SSH host key secret created successfully"

generate-modern-hostkey: ## Generate modern SSH host key secret (Ed25519)
	@echo "Generating modern SSH host key (Ed25519)..."
	@ssh-keygen -t ed25519 -f /tmp/containerssh_host_key -N "" -C "containerssh@kubernetes" > /dev/null 2>&1
	@$(KUBECTL) create secret generic containerssh-hostkey --from-file=host.key=/tmp/containerssh_host_key -n $(NAMESPACE) --dry-run=client -o yaml | $(KUBECTL) apply -f -
	@rm -f /tmp/containerssh_host_key /tmp/containerssh_host_key.pub
	@echo "Modern SSH host key secret created successfully"

deploy: ## Deploy ContainerSSH to Kubernetes
	@echo "Deploying ContainerSSH..."
	@$(KUBECTL) apply -f namespace.yaml
	@$(KUBECTL) apply -f serviceaccount.yaml
	@$(KUBECTL) apply -f rbac.yaml
	@$(KUBECTL) apply -f configmap.yaml
	@make generate-modern-hostkey
	@$(KUBECTL) apply -f deployment.yaml
	@$(KUBECTL) apply -f service.yaml
	@echo ""
	@echo "Deployment completed!"
	@echo "Waiting for ContainerSSH to be ready..."
	@$(KUBECTL) wait --for=condition=available --timeout=300s deployment/containerssh -n $(NAMESPACE)
	@echo ""
	@make status

clean: ## Delete all ContainerSSH resources
	@echo "Cleaning up ContainerSSH resources..."
	@$(KUBECTL) delete -f service.yaml --ignore-not-found=true
	@$(KUBECTL) delete -f deployment.yaml --ignore-not-found=true
	@$(KUBECTL) delete secret containerssh-hostkey -n $(NAMESPACE) --ignore-not-found=true
	@$(KUBECTL) delete -f configmap.yaml --ignore-not-found=true
	@$(KUBECTL) delete -f rbac.yaml --ignore-not-found=true
	@$(KUBECTL) delete -f serviceaccount.yaml --ignore-not-found=true
	@$(KUBECTL) delete -f namespace.yaml --ignore-not-found=true
	@echo "Cleanup completed!"

status: ## Show ContainerSSH deployment status
	@echo "ContainerSSH Status:"
	@echo "===================="
	@$(KUBECTL) get all -n $(NAMESPACE)
	@echo ""
	@echo "SSH Access Information:"
	@echo "======================="
	@NODE_PORT=$$($(KUBECTL) get service containerssh -n $(NAMESPACE) -o jsonpath='{.spec.ports[0].nodePort}'); \
	echo "SSH Command (Modern): ssh testuser@localhost -p $$NODE_PORT"; \
	echo "SSH Command (Legacy): ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa testuser@localhost -p $$NODE_PORT"

logs: ## Show ContainerSSH logs
	@echo "ContainerSSH Logs:"
	@echo "=================="
	@$(KUBECTL) logs -l app=containerssh -n $(NAMESPACE) --tail=50

logs-follow: ## Follow ContainerSSH logs
	@echo "Following ContainerSSH logs (Ctrl+C to stop):"
	@echo "=============================================="
	@$(KUBECTL) logs -l app=containerssh -n $(NAMESPACE) --follow

ssh-test: ## Test SSH connection to ContainerSSH
	@NODE_PORT=$$($(KUBECTL) get service containerssh -n $(NAMESPACE) -o jsonpath='{.spec.ports[0].nodePort}'); \
	echo "Testing SSH connection..."; \
	echo "NodePort: $$NODE_PORT"; \
	echo "Command: ssh testuser@localhost -p $$NODE_PORT"; \
	echo ""; \
	echo "Note: Use any username and password for testing"

ssh-test-legacy: ## Test SSH connection with legacy options
	@NODE_PORT=$$($(KUBECTL) get service containerssh -n $(NAMESPACE) -o jsonpath='{.spec.ports[0].nodePort}'); \
	echo "Testing SSH connection with legacy options..."; \
	echo "NodePort: $$NODE_PORT"; \
	echo "Command: ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa testuser@localhost -p $$NODE_PORT"; \
	echo ""; \
	echo "Note: Use any username and password for testing"

restart: ## Restart ContainerSSH deployment
	@echo "Restarting ContainerSSH..."
	@$(KUBECTL) rollout restart deployment/containerssh -n $(NAMESPACE)
	@$(KUBECTL) rollout status deployment/containerssh -n $(NAMESPACE)
	@echo "ContainerSSH restarted successfully"

update-config: ## Update ConfigMap and restart deployment
	@echo "Updating ContainerSSH configuration..."
	@$(KUBECTL) apply -f configmap.yaml
	@make restart
	@echo "Configuration updated successfully"

# 호스트키 업그레이드를 위한 추가 명령어
upgrade-hostkey: ## Upgrade to modern host key
	@echo "Upgrading to modern host key..."
	@$(KUBECTL) delete secret containerssh-hostkey -n $(NAMESPACE) --ignore-not-found=true
	@make generate-modern-hostkey
	@make restart
	@echo "Host key upgraded successfully"
