BASE_URL ?= http://localhost:8000
.PHONY: up burst test
up:
	docker compose up --build
burst:
	python scripts/burst.py $(BASE_URL)
test:
	pytest -q
