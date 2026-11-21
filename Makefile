.PHONY: install test integration demo library-demo lint doctor realworld-image realworld-prepare realworld-verify comparison-image comparison-benchmark
REALWORLD_ROOT ?= experiments/realworld-003
COMPARISON_BUNDLE ?= experiments/comparison-benchmark-001
install:
	uv sync --locked
test:
	uv run --locked pytest -m 'not docker' -q
integration:
	uv run --locked pytest -m docker -q
demo:
	uv run --locked evocoder --mode mock demo
library-demo:
	uv run --locked python scripts/run_library_demo.py --output experiments/library-demo-$$(date +%Y%m%d-%H%M%S)
lint:
	uv run --locked ruff check .
doctor:
	uv run --locked evocoder doctor
realworld-image:
	docker build -f docker/realworld.Dockerfile -t evocoder-realworld:py312-v1 docker
realworld-prepare:
	uv run --locked evocoder --mode mock realworld-prepare --output $(REALWORLD_ROOT)/bundle
realworld-verify:
	uv run --locked evocoder --mode mock realworld-verify --manifest $(REALWORLD_ROOT)/bundle/manifest.json --image evocoder-realworld:py312-v1 --output $(REALWORLD_ROOT)/verification.json
comparison-image: realworld-image
	docker build -f docker/comparison.Dockerfile -t evocoder-comparison:py312-v1 docker
comparison-benchmark:
	uv run --locked python scripts/prepare_comparison_benchmark.py --output $(COMPARISON_BUNDLE)
	uv run --locked python scripts/verify_frozen_benchmark.py --root $(COMPARISON_BUNDLE)
