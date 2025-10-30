# Makefile for building CUDA guest images

.PHONY: help build-all build-cuda11.8 build-cuda12.2 build-cuda11.8-test build-cuda12.2-test build-all-test clean

# Default Anaconda version
ANACONDA_VERSION ?= 2025.06-1

# Tag suffix (empty by default, set to _test for test builds)
TAG_SUFFIX ?=

help:
	@echo "Available targets:"
	@echo "  build-all           - Build all CUDA image variants"
	@echo "  build-cuda11.8      - Build CUDA 11.8 image"
	@echo "  build-cuda12.2      - Build CUDA 12.2 image"
	@echo "  build-cuda11.8-test - Build CUDA 11.8 test image (tag: cuda11.8_test)"
	@echo "  build-cuda12.2-test - Build CUDA 12.2 test image (tag: cuda12.2_test)"
	@echo "  build-all-test      - Build all CUDA test images"
	@echo "  clean               - Remove old Dockerfiles (after migration)"
	@echo ""
	@echo "Usage:"
	@echo "  make build-cuda11.8"
	@echo "  make build-cuda12.2"
	@echo "  make build-all"
	@echo "  make build-cuda11.8-test"
	@echo "  make build-cuda12.2-test"
	@echo "  make build-all-test"

build-all: build-cuda11.8 build-cuda12.2

build-cuda11.8:
	docker build \
		--build-arg CUDA_VERSION=11.8.0 \
		--build-arg CUDNN_VERSION=8 \
		--build-arg UBUNTU_VERSION=22.04 \
		--build-arg ANACONDA_VERSION=$(ANACONDA_VERSION) \
		-t containerssh-guest:cuda11.8$(TAG_SUFFIX) \
		-f Dockerfile.cuda \
		.

build-cuda12.2:
	docker build \
		--build-arg CUDA_VERSION=12.2.2 \
		--build-arg CUDNN_VERSION=8 \
		--build-arg UBUNTU_VERSION=22.04 \
		--build-arg ANACONDA_VERSION=$(ANACONDA_VERSION) \
		-t containerssh-guest:cuda12.2$(TAG_SUFFIX) \
		-f Dockerfile.cuda \
		.

# Test build targets
build-cuda11.8-test:
	$(MAKE) build-cuda11.8 TAG_SUFFIX=_test

build-cuda12.2-test:
	$(MAKE) build-cuda12.2 TAG_SUFFIX=_test

build-all-test: build-cuda11.8-test build-cuda12.2-test

# Optional: clean up old directory-based Dockerfiles after migration
clean:
	@echo "⚠️  This will remove old Dockerfile directories."
	@echo "Make sure you've tested the new unified Dockerfile first!"
	@read -p "Continue? [y/N] " -n 1 -r; \
	if [[ $$REPLY =~ ^[Yy]$$ ]]; then \
		rm -rf cuda11.8_cudnn8_dev_ubuntu22.04 cuda12.2_cudnn8_dev_ubuntu22.04; \
		echo "Old directories removed"; \
	else \
		echo "Cancelled"; \
	fi
