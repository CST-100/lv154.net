.PHONY: build serve deploy docker docker-up docker-down clean edit

build:
	python3 build.py

edit:
	python3 tools/edit.py

serve: build
	cd dist && python3 -m http.server 8000

deploy: build
	bash deploy.sh

docker:
	docker compose build

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

clean:
	rm -rf dist
