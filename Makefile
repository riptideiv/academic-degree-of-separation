.PHONY: test lint run

test:
	python -m pytest -q
	node --test tests/js/*.test.cjs

lint:
	ruff check backend tests scripts

run:
	uvicorn backend.app:app --reload --port 8000
