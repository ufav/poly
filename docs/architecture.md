# Архитектура

Python 3.11, виртуальное окружение `.venv`, секреты в `.env` (в git не попадает), данные в `data/` (в git не попадают).

```
pmbot/                    пакет с переиспользуемым кодом
  http.py                 HTTP-клиент: User-Agent, ретраи, пауза между запросами
  storage.py              SQLite-схема и запись
  polymarket.py           Gamma / CLOB / Data API: события, рынки, история цен
  cybersport.py           cybersport.ru: матчи CS2 / Dota 2, карты, раунды (CS2), составы на матч и история составов
  join.py                 связка cybersport ↔ Polymarket (external_id, иначе имена + дата ±1 день)
  geo.py                  часовые пояса: дом команды (страны игроков) и город турнира
  pandascore.py           PandaScore REST (CS2, Dota 2): время карт, расписание, турнир, команды, составы (бесплатный тариф)
  opendota.py             OpenDota: игры Dota 2 — драфт, игроки, золото/опыт по минутам, объекты, логи
  dota_join.py            связка карт Dota: Polymarket (game_id + номер карты) ↔ OpenDota ↔ cybersport
  maptiming.py            оценка начала/конца карты по траектории цены (смещена, см. R-6; есть PandaScore)
  olimpbet.py             Olimpbet JSON API: результаты тенниса по дням, разбор турнира (уровень, город, страна, покрытие)
  tennisdata.py           tennis-data.co.uk: годовые xlsx ATP/WTA (раунд, рейтинг, кэфы), чтение xlsx без зависимостей
  tennis_join.py          связка Polymarket ↔ Olimpbet ↔ tennis-data + признаки (покрытие, дом, джетлаг, усталость, кэфы)
  tiers.py                уровень турнира по названию (tier1 / tier2+ / квалификации) — анализ и бот
  telegram.py             уведомления (выключены без TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
  live/                   бот-регистратор и бумажная торговля
    feeds.py              sports WebSocket (счёт), Gamma (матчи, рынки, расчёт), CLOB /book
    store.py              база data/live.sqlite (CS2), data/live_<sport>.sqlite (dota2 / tennis)
    paper.py              правила (по счёту, по цене, мейкер; Dota — по счёту карт), симуляция исполнения по стакану / ленте, расчёт
    report.py             сводка по правилам
scripts/
  check_env.py            проверка .env
  collect_polymarket.py   сбор истории Polymarket за период (повторный запуск докачивает)
  collect_cybersport.py   сбор матчей/карт/раундов cybersport.ru по дням (повторный запуск докачивает; --sport dota2)
  collect_cs_teams.py     составы команд с датами и национальностью игроков (--sport dota2)
  collect_cs_players.py   статистика игроков по картам для уже собранных матчей (новые — в collect_cybersport.py)
  collect_pandascore.py   PandaScore для всех матчей Polymarket (+ все прошлые матчи с --since; нужен PANDASCORE_ACCESS_TOKEN)
  collect_opendota.py     OpenDota: все лиговые игры Dota 2 (SQL explorer пачками) + недостающие по Steam id cybersport
  link_dota.py            пересборка dota_map_links
  collect_olimpbet.py     теннис: результаты Olimpbet по дням (медленно, пауза ~4 с; повторный запуск докачивает)
  collect_tennisdata.py   теннис: tennis-data.co.uk ATP/WTA за годы
  collect_tennis.bat      теннис одной командой: цены Polymarket / trades / olimpbet / tennisdata / link
  link_tennis.py          пересборка tennis_links (после collect_*)
  db_summary.py           статистика базы и пример траектории цены
  progress.py             живой прогресс сборщиков
  paper_bot.py            бот: регистратор live-матчей + бумажные сделки (Docker; PAPER_SPORT=cs2 / dota2 / tennis)
  paper_report.py         отчёт по бумажным сделкам (--sport dota2|tennis)
  research/               разовые исследовательские скрипты
analysis/                 проверки гипотез (результаты — docs/research.md)
data/
  cs2.sqlite              основная база
  dota2.sqlite            Dota 2: та же схема (`storage.db_path("dota2")`; сборщики и анализ — `--sport dota2`)
  tennis.sqlite           теннис ATP/WTA/ITF (`collect_tennis.bat`): pm_* (`--sport tennis` у collect_polymarket / collect_trades),
                          ob_* — Olimpbet (турнир: город/страна/покрытие; матчи), td_matches — tennis-data.co.uk,
                          tennis_links — связка трёх источников + признаки (D-20)
  live/                   записи живых матчей (jsonl)
docs/                     CJM, архитектура, бэклог
```

## База `data/cs2.sqlite`

- `pm_events` — матч: `game_id`, время начала, BO, турнир, команды, итоговый счёт по картам, объём.
- `pm_markets` — рынки матча: тип, номер карты, исходы, токены, итоговая цена исхода A, `history_status` (`pending`/`done`/`empty`/`skipped`).
- `pm_prices` — поминутная цена токена исхода A (`token_id`, `t`, `p`), окно от −6 ч до +12 ч от начала. Цена исхода B ≈ 1 − p.

История качается только для `moneyline` и `child_moneyline`; остальные рынки хранятся без цен.

- `cs_matches` — матч cybersport.ru: дата (МСК), формат, команды и счёт по картам в порядке сайта, турнир, tier, `external_id` (= `pm_events.game_id`), коэффициенты букмекера, `stats_status` (`done` — есть раунды, `none` — нет).
- `cs_games` — карты матча по порядку: название карты, счёт (`score1/score2` в порядке команд сайта), число раундов.
- `cs_rounds` — раунд: команда на CT, команда на T, победившая сторона и команда, исход (`eliminated`/`exploded`/`defused`/...). Имена команд — как в Polymarket.
- В `cs_matches` также: `team1_id/team2_id` (id команд cybersport), `location` турнира («Город, Страна»; пусто — онлайн), `stage_since/stage_till` — даты турнира.
- `cs_player_stats` — игрок на карте (`game_id` = `cs_games.id`): kills, deaths, assists, ADR, KAST, `rating` (**общий рейтинг игрока на момент загрузки, не за карту** — для анализа не использовать). Команда игрока — через `cs_team_players` на дату матча. `cs_player_stats_status` — какие матчи уже загружены.
- `ps_games` — карта по PandaScore: `match_id` (= `pm_events.game_id`), `position` (номер карты), `begin_at`, `end_at`, `length` (с), победитель. `ps_matches` — статус матча и время загрузки (`missing` — нет в PandaScore).
- `cs_teams`, `cs_team_players` — составы: игрок, страна (`country_code`), статус (`player`/`substitute`/`coach`), `since/till`. `cs_countries` — код страны ↔ русское название (для разбора `location`).
- `ps_match_info` — матч PandaScore (обе игры): расписание и переносы (`scheduled_at`, `original_scheduled_at`, `rescheduled`), формат, счёт, команды, турнир; `raw` — весь JSON. `ps_tournaments` — стадия: `type` (online/offline), страна, регион, tier (s/a/b/c/d), призовые, серия, лига. `ps_teams` — `location` (страна команды). `ps_players` — текущий состав на момент загрузки: национальность, роль.

Связка с Polymarket (`pmbot/join.py`): точная по `external_id`, иначе по нормализованным именам команд и дате ±1 день; синонимы имён («FaZe Clan» ↔ «FaZe») выучиваются из точных связок. На 880 точных связках: 876 совпадений, 0 ошибок.

## База `data/dota2.sqlite` (сверх общей схемы)

- `pm_*` — как в CS2 (series 10309); `pm_trades` — сделки (D-15).
- `cs_matches` / `cs_games` (cybersport, дисциплина 21): `cs_games.external_id` = **Steam id игры** = OpenDota `match_id`; в `cs_matches` также `prize_pool`, `bracket` (стадия), `round_name` (раунд/группа), коэффициенты букмекера. `cs_match_players` — кто реально играл матч: игрок, страна, `steam_id` (= OpenDota `account_id`). `cs_teams` / `cs_team_players` — история составов.
- `ps_*` — PandaScore: все матчи Dota с 06.2025 (не только Polymarket), карты (`ps_games.length` = OpenDota `duration` до секунды), турниры, команды, игроки.
- `od_matches` — игра (карта) OpenDota: время, длительность, победитель, лига, серия, команды Radiant/Dire, капитаны, патч, первая кровь, состояние башен/бараков; JSON: `radiant_gold_adv` / `radiant_xp_adv` (по минутам), `objectives` (башни, Рошан, первая кровь, курьеры — со временем), `draft_timings`. `od_picks_bans` — драфт по порядку (`team` 0 = Radiant). `od_players` — 10 игроков: герой, KDA, GPM/XPM, урон, вардов, линия/роль, предметы, `gold_t`/`xp_t`/`lh_t` по минутам. `od_logs` — сжатый JSON (`opendota.load_logs`): тимфайты, по игроку — убийства, покупки, выкупы, руны, варды, прокачка, нейтралки. Справочники: `od_leagues` (tier premium/professional/excluded), `od_teams` (+ рейтинг), `od_pro_players` (страна, роль), `od_heroes`, `od_items`, `od_patches`.
- `dota_map_links` — карта: OpenDota `od_match_id` ↔ Polymarket `pm_game_id` + `map_number` ↔ `cs_match_id`; `method`: `ps_length` (длина карты PandaScore = длительность OpenDota + имя команды), `ps_length_only` (без совпадения имён, длительность уникальна в окне), `cybersport` (Steam id карты cybersport). На 4 неделях: 423 карты обоими путями — 0 расхождений.

Полный сбор Dota одной командой: `scripts\collect_dota.bat` (cybersport → составы → OpenDota → связка), параллельно `scripts\collect_dota.bat prices` (цены Polymarket). Порядок сбора Dota: `collect_polymarket.py --sport dota2` → `collect_trades.py --sport dota2` → `collect_pandascore.py --sport dota2 --since 2025-06-01` → `collect_cybersport.py --sport dota2 --since 2025-06-01` → `collect_cs_teams.py --sport dota2` → `collect_opendota.py` (недостающие игры по Steam id cybersport — после cybersport; при упоре в 3000/день — повторить завтра) → `link_dota.py`.

## Бот-регистратор и бумажная торговля (`scripts/paper_bot.py`)

Реальных ордеров не ставит, приватный ключ не нужен. Асинхронные циклы:

- поиск матчей: открытые события CS2 в Gamma раз в 60 с → `live_games`, `live_markets`;
- счёт: sports WebSocket, каждое изменение счёта/карты со временем прихода → `live_scores` (у части матчей раунды «000-000» — данных нет);
- стакан текущей карты (оба исхода) раз в `BOOK_INTERVAL` с, при изменении → `live_books` (топ-10 уровней);
- правила (`pmbot/live/paper.py`) в перерыве карты (12–13 раундов) и во второй половине (`leader_late`), каждое не больше раза на карту: сигнал → через 1 с берётся стакан, покупка на `PAPER_STAKE_PCT` % от виртуального банка правила (`paper_bank`: `PAPER_BANK_USD` при первом запуске правила + P&L рассчитанных с тех пор сделок; удалить строку — начать банк заново) по заявкам продавцов + комиссия → `paper_trades` (`open` / `unfilled`, если продавцов нет);
- ценовое правило `fav_band_t1` (только tier1, на перерыве): покупка стороны, чей ask в 0.70–0.92 (R-14/R-15);
- бумажный мейкер `maker_t1` (только tier1, текущая карта): раз в `MAKER_INTERVAL` с последние 500 тейкерских сделок (Data API) → `paper.maker_fills`: заявка на покупку фаворита (0.70–0.92) по последней цене сделки, исполняется только если продающий тейкер прошёл ниже неё (очередь не важна); одна позиция на карту, добор до ставки, без комиссии (проверка на истории — `scripts/research/maker_replay.py`, совпадает с `analysis/maker_sim.py`);
- расчёт по закрытию рынка в Gamma раз в 2 мин → `settled`, P&L;
- Telegram: сигнал, расчёт, итог дня в `REPORT_HOUR_UTC`; в `CS_COLLECT_HOUR_UTC` — сбор cybersport.ru за 2 дня (стороны первой половины для анализа).

База `data/live.sqlite`: `live_games`, `live_markets`, `live_scores`, `live_books`, `paper_trades` (статусы `open` / `settled` / `unfilled` / `rejected` — ask противоречит счёту / `invalid` — помечено вручную, в отчёт не идёт), `paper_bank`.

Dota 2 — тот же бот вторым контейнером `paper-bot-dota` (`PAPER_SPORT=dota2`, `PAPER_STAKE_PCT=1`, все правила Доты; база `data/live_dota2.sqlite`, свои банки): Gamma series 10309, WebSocket лиги `dota2` (счёт `убийства|карты|BoN`, у части кадров убийства «000-000»). Правила `paper.DOTA_RULES` (R-20) — только tier1 BO3: когда фид переключается на карту 2, покупка проигравшего карту 1 на карту 2 (по кэфу матча до начала: 2.0–2.5, 2.5–3.33, любой). Срабатывают, только если бот в этом запуске видел карту 1 (после перезапуска посреди карты 2 — пропуск). Сбор cybersport в контейнере Доты не запускается. Отчёт: `docker compose exec paper-bot-dota python scripts/paper_report.py --sport dota2`.

Теннис — третий контейнер `paper-bot-tennis` (`PAPER_SPORT=tennis`, `PAPER_STAKE_PCT=1`, база `data/live_tennis.sqlite`): Gamma series ATP/WTA/ITF (10365/10366/11634), WebSocket лиги `atp`/`wta`/`itf` (счёт `6-3, 4-3`, период `S1`/`S2`/`S3`). Правила `paper.TENNIS_RULES` (R-23b): когда фид переходит на сет 2 и сет 1 завершён — покупка победителя сета 1 на moneyline матча (`tennis_s1_winner` любой; `tennis_dog_s1` прематч-аутсайдер; `tennis_fav_s1` прематч-фаворит). Нужен живой сет 1 в этом запуске (рестарт посреди S2 — пропуск). Отчёт: `docker compose exec paper-bot-tennis python scripts/paper_report.py --sport tennis`.

### Деплой (Linux + Docker)

1. `git clone https://github.com/ufav/poly.git ~/polymarket` (`.env` и `data/` в репозиторий не попадают).
2. На сервере создать `~/polymarket/.env` только с переменными бота (`TELEGRAM_*`, `PAPER_*` из `.env.example`) — **приватный ключ на сервер не класть**.
3. Telegram: создать бота у @BotFather → `TELEGRAM_BOT_TOKEN`; написать боту любое сообщение, открыть `https://api.telegram.org/bot<TOKEN>/getUpdates` → `chat.id` → `TELEGRAM_CHAT_ID`.
4. `cd ~/polymarket && docker compose up -d --build`; логи — `docker compose logs -f`; отчёт — `docker compose exec paper-bot python scripts/paper_report.py`.
5. Обновление: `git pull && docker compose up -d --build`. Данные лежат в `~/polymarket/data` (том), переживают перезапуск.

### Автодеплой (GitHub Actions, `.github/workflows/deploy.yml`)

Push в `main` с изменениями в `pmbot/`, `scripts/` (кроме `scripts/research/`), `Dockerfile`, `docker-compose.yml`, `requirements.txt` → SSH на сервер → `git pull --ff-only && docker compose up -d --build`. Правки только `docs/` и `analysis/` бота не перезапускают. Другие приложения на сервере не затрагиваются: работа только в `~/polymarket`, проект compose `polymarket`, портов нет, чистятся только старые образы с меткой `app=polymarket-paper-bot`. Ручной запуск — вкладка Actions → «deploy paper bot» → Run workflow.

Секреты репозитория (Settings → Secrets and variables → Actions): `SSH_HOST`, `SSH_USER`, `SSH_KEY` (приватный ключ отдельной пары только для деплоя), `SSH_PORT` (если не 22). Пара создаётся на сервере: `ssh-keygen -t ed25519 -f ~/.ssh/gh_deploy -N "" -C gh-actions && cat ~/.ssh/gh_deploy.pub >> ~/.ssh/authorized_keys`; содержимое `~/.ssh/gh_deploy` → `SSH_KEY`.

## Источники данных

| Источник | Что даёт | Ограничения |
|----------|----------|-------------|
| Polymarket Gamma `gamma-api.polymarket.com` | События CS2 (`series_id=10310`), рынки, исходы, `gameId`, счёт по картам | `closed=false` возвращает и старые нерассчитанные события; без своего `User-Agent` из Python — 403 |
| Polymarket CLOB `prices-history` | История цены токена. С `startTs/endTs` и `fidelity=1` — поминутно | Без окна (`interval=max`) точки редкие (~10 мин) |
| Polymarket Data API `/trades` | Все сделки по рынку с временем, ценой, объёмом | — |
| Polymarket sports WS `wss://sports-api.polymarket.com/ws` | `gameId`, команды, счёт `раунды|карты|BoN`, номер карты, статус; изменения приходят почти сразу (проверено 10.10.2026) | Раз в ~20 с по всем матчам CS2 приходит кадр с раундами `000-000`, между ними — настоящий счёт (бот держит последний настоящий); у части матчей только `000-000`; сторон CT/T нет; поле `score` в Gamma обновляется лишь раз в ~5 мин |
| cybersport.ru match-stats в лайве | — | Раунды появляются только после окончания карты — для лайва не годится |
| cybersport.ru `/api/matches?filter[date]=…&filter[disciplineIds]=22` | Матчи CS2 за день (дата МСК), команды, турнир, коэффициенты | Неофициальный API |
| cybersport.ru `/api/matches/<id>` | Карты матча: название, счёт, id для связки с раундами | ~260 КБ на матч, `include` игнорируется |
| cybersport.ru `/api/teams/<id>` | История состава: игроки, страна, даты прихода/ухода; статистика команды по картам | Страна игрока ≠ место жительства команды (буткемпы) |
| cybersport.ru `/api/v1/match-stats/csgo?filter[internalId]=<id>` | Раунды: CT/T, победившая сторона и команда, исход раунда; статистика игроков | Есть примерно у 40% матчей; лайв не подтверждён |
| PandaScore `api.pandascore.co/csgo/matches?filter[id]=…` | Матч по `gameId` Polymarket (id совпадают): карты с `begin_at`/`end_at`/`length`/победителем; до 100 матчей за запрос | Бесплатно: только списки, 1000 запросов/ч; `/matches/{id}`, `/games/{id}`, раунды — 403; live-WS — 4003 «Unsufficient permission» (платно) |
| GRID Open Access | Официальные данные CS2, раунды, стороны, история | Нужна заявка; 10 запросов/мин |
| cybersport.ru Dota 2 (`filter[disciplineIds]=21`, `/api/matches/<id>`) | Матчи, карты со Steam id (`externalApiId`), состав на матч со Steam id игроков, страна, город турнира, призовые, стадия/раунд, коэффициенты | `match-stats` для Доты нет (404); названий карт нет |
| PandaScore `/dota2/matches`, `/matches/past`, `/teams` | Матч по `gameId` Polymarket, карты с `length` и временем, турнир (LAN/онлайн, страна, регион, tier, призовые), команды с `location`, текущие составы (национальность, роль) | Бесплатно: только списки; драфта и Steam id карт нет; `/matches/{id}`, `/games` — 404 |
| OpenDota `api.opendota.com/api/explorer?sql=` | SQL по лиговым играм: драфт, игроки, золото/опыт по минутам, объекты, тимфайты, логи — 100 игр за запрос | Без ключа 60 запросов/мин, 3000/день; **таблица полная до ~05.2026, дальше 7–25% игр** |
| OpenDota `/matches/{id}` | То же по одной игре — для недостающих (Steam id из cybersport) | Тот же дневной лимит; `/proMatches` тоже не видит недостающие игры |

`gameId` Polymarket = `externalId` в match-stats cybersport.ru (проверено на 6 из 6 матчей, имена команд совпадают; вероятно, оба берут данные у PandaScore) — ключ для связки матчей.

## Рынки CS2 на Polymarket

`sportsMarketType`: `moneyline` (матч), `child_moneyline` (карта N), `map_handicap`, `totals` (карты), `round_handicap_game_N`, `round_over_under_game_N`, `cs2_odd_even_total_kills/rounds`.

Торговые условия: `secondsDelay = 1` (рыночный ордер ждёт 1 с и не отменяется), taker fee `0.05 × p × (1 − p)` за долю, maker без комиссии.
