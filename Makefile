.PHONY: up down test

up:
	@test -f .env || cp .env.example .env
	docker compose up --build -d
	@echo "Backend: http://localhost:8000  Frontend: http://localhost:3000"

down:
	docker compose down

test:
	cd backend && pip install -q -r requirements.txt && pytest -q
