COMPOSE ?= docker compose
HELM_IMAGE ?= alpine/helm:3.16.3
CHART := deploy/helm/veon-sample-app
SENTINEL_CLI = $(COMPOSE) exec -T sentinel-1 sh -c 'redis-cli -p 26379 --no-auth-warning -a "$$SENTINEL_PASSWORD" $(1)'

.DEFAULT_GOAL := help
.PHONY: help env up up-efk down clean ps logs test smoke \
        failover failover-hard failover-freeze redis-recover redis-master \
        worker-stop worker-start helm-lint helm-template

help: ## Komutları listeler
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env:
	@test -f .env || (cp .env.example .env && echo ".env oluşturuldu (.env.example'dan)")

up: env ## Tüm stack'i derleyip başlatır (Loki ile)
	$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory urls

up-efk: env ## Stack + Elasticsearch/Kibana (loglar hem Loki hem ES'e)
	FLUENT_ES_MATCH='app.*' $(COMPOSE) --profile efk up -d --build
	@$(MAKE) --no-print-directory urls

urls:
	@set -a; . ./.env; set +a; \
	echo ""; \
	echo "  Uygulama   : http://localhost:$${FRONTEND_PORT:-8080}"; \
	echo "  Grafana    : http://localhost:$${GRAFANA_PORT:-3000}/d/veon-sample-overview"; \
	echo "  Prometheus : http://localhost:$${PROMETHEUS_PORT:-9090}"; \
	echo "  Jaeger     : http://localhost:$${JAEGER_PORT:-16686}"; \
	echo "  Kibana     : http://localhost:$${KIBANA_PORT:-5601}  (yalnız up-efk)"; \
	echo "  API docs   : http://localhost:$${FRONTEND_PORT:-8080}/api/docs"

down: ## Container'ları durdurur (veriler kalır)
	$(COMPOSE) --profile efk --profile test down

clean: ## Container'ları ve VOLUME'ları siler (Mongo/ES verisi dahil!)
	$(COMPOSE) --profile efk --profile test down -v --remove-orphans

ps: ## Servis durumları
	$(COMPOSE) ps

logs: ## Uygulama logları (JSON)
	$(COMPOSE) logs -f --tail=50 backend worker

test: env ## Birim + gerçek Sentinel kümesine karşı entegrasyon testleri
	$(COMPOSE) --profile test run --rm --build backend-tests pytest -rA

smoke: ## Uçtan uca senaryoları curl ile doğrular
	./scripts/smoke.sh

redis-master: ## Sentinel'e göre şu anki master
	@$(call SENTINEL_CLI,SENTINEL get-master-addr-by-name mymaster) | head -1

failover: ## Kontrollü failover (SENTINEL FAILOVER)
	$(call SENTINEL_CLI,SENTINEL FAILOVER mymaster)

failover-hard: ## Sert kesinti: şu anki master container'ını durdurur
	@M=$$($(call SENTINEL_CLI,SENTINEL get-master-addr-by-name mymaster) | head -1 | tr -d '\r'); \
	echo "master '$$M' durduruluyor..."; $(COMPOSE) stop -t 0 $$M

failover-freeze: ## Ağ bölünmesi benzetimi: master'ı dondurur (TCP RST yok, yalnız timeout)
	@M=$$($(call SENTINEL_CLI,SENTINEL get-master-addr-by-name mymaster) | head -1 | tr -d '\r'); \
	echo "master '$$M' donduruluyor (geri almak için: make redis-recover)"; $(COMPOSE) pause $$M

redis-recover: ## Durdurulan/dondurulan Redis düğümlerini geri getirir (replika olarak katılırlar)
	-$(COMPOSE) unpause redis-master redis-replica-1 redis-replica-2 2>/dev/null
	$(COMPOSE) start redis-master redis-replica-1 redis-replica-2

worker-stop: ## Worker'ları durdurur (stream'de lag birikir)
	$(COMPOSE) stop worker

worker-start: ## Worker'ları başlatır (birikmiş olaylar işlenir)
	$(COMPOSE) start worker

helm-lint: ## Helm chart lint + template (docker ile, yerel helm gerekmez)
	docker run --rm -v "$(CURDIR)/deploy/helm:/charts" -w /charts $(HELM_IMAGE) lint veon-sample-app -f examples/values-stage.yaml
	docker run --rm -v "$(CURDIR)/deploy/helm:/charts" -w /charts $(HELM_IMAGE) template vsa veon-sample-app -n veon-ns -f examples/values-stage.yaml > /dev/null && echo "template OK"

helm-template: ## Render edilmiş manifest'leri yazdırır
	docker run --rm -v "$(CURDIR)/deploy/helm:/charts" -w /charts $(HELM_IMAGE) template vsa veon-sample-app -n veon-ns -f examples/values-stage.yaml
