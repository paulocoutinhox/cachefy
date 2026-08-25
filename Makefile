.DEFAULT_GOAL := help

PYTHON ?= python3
VENV ?= .venv

help:
	@echo "cachefy development commands"
	@echo "  make install    create the virtualenv and install the package with its development tools"
	@echo "  make servers    start the redis, mysql and postgres the full suite needs"
	@echo "  make servers-stop  remove those same containers"
	@echo "  make test       run the suite"
	@echo "  make coverage   run the suite with the 100% branch coverage gate"
	@echo "  make stress     run many processes against every server that answers"
	@echo "  make lint       check the code"
	@echo "  make format     format the code"
	@echo "  make build      build the wheel and the sdist"
	@echo "  make clean      remove build and coverage artifacts"

install:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip
	# the mysql and postgres drivers belong here and not in the extras: the suite reaches a store whenever its port answers, so with `make servers` up and a driver missing every test of that store errors on the engine instead of being left out
	$(VENV)/bin/python -m pip install -e ".[sqlalchemy,redis]" pytest pytest-asyncio pytest-cov pytest-timeout "ruff==0.16.4" "black==26.5.1" aiosqlite aiomysql asyncpg cryptography pyyaml build

servers:
	docker run -d --name cachefy-redis -p 6398:6379 redis:7-alpine
	docker run -d --name cachefy-mysql -e MYSQL_ROOT_PASSWORD=root -e MYSQL_DATABASE=cachefy -p 3398:3306 mysql:8.4
	docker run -d --name cachefy-postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=cachefy -p 5498:5432 postgres:16-alpine

servers-stop:
	docker rm -f cachefy-redis cachefy-mysql cachefy-postgres

test:
	$(VENV)/bin/python -m pytest

coverage:
	$(VENV)/bin/python -m pytest --cov

stress:
	$(VENV)/bin/python -m pytest -m stress -v

lint:
	$(VENV)/bin/python -m ruff check .
	$(VENV)/bin/python -m black --check .

format:
	$(VENV)/bin/python -m ruff check --fix .
	$(VENV)/bin/python -m black .

build:
	$(VENV)/bin/python -m build

clean:
	rm -rf dist build htmlcov .coverage coverage.xml .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
