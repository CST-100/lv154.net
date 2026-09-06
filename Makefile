.PHONY: build serve deploy docker docker-up docker-down clean edit tui

build:
	python3 build.py

edit:
	python3 tools/edit.py

tui:
	python3 tools/tui.py $(ARGS) $(FILE)

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
