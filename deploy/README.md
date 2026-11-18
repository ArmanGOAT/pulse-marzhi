# Развёртывание

Демо: https://pulse.modeus.cc. Сервер vm1.modeus.cc (Ubuntu, 2 CPU, 4 ГБ), DNS в Cloudflare без проксирования.

```
push в main ─► test ─► build ─────────────────────────► deploy ─────────────────────────────► vm1
               ruff    образ из checkout: снимки реестра   ssh с ключом, ограниченным       nginx :443 (certbot)
               pytest  и отчёты радара из git, панель      командой pulse-deploy:            └► 127.0.0.1:8000
                       собирается в Dockerfile → ghcr.io   pull по digest, healthy, иначе откат    контейнер
```

| Событие | test | build | deploy |
| --- | --- | --- | --- |
| pull request | да | нет | нет |
| push в `main` | да | да | да |
| запуск вручную (Actions → CI/CD → Run workflow) | да | да | только из `main` |

CI ничего не скачивает с mpa.uz и не переобучает радар: эталонные снимки реестра (SHA-256 в
`data/raw/mpa_api/manifest.json`) и основной запуск радара лежат в git, и на сайте ровно те цифры, что в README.
Повторно скачанные снимки mpa.uz отличаются от эталона (docs/datasheet.md), поэтому новые данные попадают
на сайт только через коммит.

## Кэширование

| Что | Где | Когда сбрасывается |
| --- | --- | --- |
| `.venv` целиком (задача test) | кэш Actions, ключ — версия Python и `requirements.lock` | при смене зависимостей |
| Слои образа | кэш BuildKit в Actions (`type=gha`, `mode=max`) | зависимости ставятся заново только при смене `requirements.lock`; панель реестра собирается заново при правке данных, `config`, `src`; правка `web` пересобирает только последний слой |
| Слои на сервере | docker на vm1 | скачиваются только изменившиеся слои, данные (~70 МБ) — только при их изменении |

## Секреты и переменные

Нужны права администратора репозитория. Settings → Environments → New environment → `production`,
в Deployment branches лучше оставить только `main`.

| Имя | Тип | Значение |
| --- | --- | --- |
| `VM1_SSH_KEY` | secret | закрытый ключ ed25519 для CI, целиком вместе со строками BEGIN/END |
| `VM1_KNOWN_HOSTS` | variable | вывод `ssh-keyscan -t ed25519,ecdsa,rsa vm1.modeus.cc`, сверенный с отпечатком сервера |
| `VM1_HOST` | variable, необязательно | по умолчанию `vm1.modeus.cc` |
| `VM1_USER` | variable, необязательно | по умолчанию `root` |

Токен GHCR не нужен: задачи используют встроенный `GITHUB_TOKEN`. Секреты не попадают в pull request из форков.

## Серверная часть (ставится вручную, CI её не меняет)

| Что | Где на vm1 |
| --- | --- |
| Скрипт выкатки | `/usr/local/sbin/pulse-deploy` ← `deploy/pulse-deploy` |
| Compose | `/opt/pulse-marzhi/compose.prod.yaml` ← `deploy/compose.prod.yaml` |
| Текущий образ | `/opt/pulse-marzhi/.deployed-image` |
| Ключ CI | `/root/.ssh/authorized_keys`: `restrict,command="/usr/local/sbin/pulse-deploy" ssh-ed25519 … pulse-marzhi-ci` |
| nginx | `/etc/nginx/sites-available/pulse.modeus.cc`, `/etc/nginx/snippets/pulse-proxy.conf`; на `/api/plan` и `/api/export/` не больше 60 запросов в минуту с IP |
| HTTPS | certbot (`--nginx --redirect`), продление `certbot.timer` |

После правки `deploy/pulse-deploy` или `deploy/compose.prod.yaml` поставить их заново:

```bash
scp deploy/pulse-deploy root@vm1.modeus.cc:/usr/local/sbin/pulse-deploy
scp deploy/compose.prod.yaml root@vm1.modeus.cc:/opt/pulse-marzhi/compose.prod.yaml
ssh root@vm1.modeus.cc 'chmod 755 /usr/local/sbin/pulse-deploy'
```

## Откат

Автоматически: если новый контейнер за 3 минуты не стал healthy, `pulse-deploy` возвращает предыдущий образ
и задача deploy падает. Вручную: Actions → нужный прошлый успешный запуск → Re-run jobs → deploy
(образы держатся на сервере неделю и всегда есть в GHCR). Или на сервере:

```bash
SSH_ORIGINAL_COMMAND=ghcr.io/armangoat/pulse-marzhi@sha256:<digest> pulse-deploy </dev/null
```

## Новая волна пересмотров

Локально: `make data-latest && make train`, проверить отчёт запуска, добавить в `.gitignore` исключение
`!artifacts/experiments/<новый запуск>/scores/` (как у `20261117-193129_radar-main`), закоммитить новые снимки,
`manifest.json`, `LATEST` и папку запуска, затем push в `main` — CI выкатит. Цифры на странице после этого изменятся,
сценарий демо (`docs/demo-script.md`) и README нужно сверить заново.
