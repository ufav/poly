# Архитектура

Python 3.11, виртуальное окружение `.venv`, секреты в `.env` (в git не попадает), данные в `data/` (в git не попадают).

```
pmbot/                    пакет с переиспользуемым кодом
  http.py                 HTTP-клиент: User-Agent, ретраи, пауза между запросами
  storage.py              SQLite-схема и запись
  polymarket.py           Gamma / CLOB / Data API: события, рынки, история цен
  cybersport.py           cybersport.ru: матчи CS2, карты, раунды
scripts/
  check_env.py            проверка .env
  collect_polymarket.py   сбор истории Polymarket за период (повторный запуск докачивает)
  collect_cybersport.py   сбор матчей/карт/раундов cybersport.ru по дням (повторный запуск докачивает)
  db_summary.py           статистика базы и пример траектории цены
  research/               разовые исследовательские скрипты
data/
  cs2.sqlite              основная база
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

## Источники данных

| Источник | Что даёт | Ограничения |
|----------|----------|-------------|
| Polymarket Gamma `gamma-api.polymarket.com` | События CS2 (`series_id=10310`), рынки, исходы, `gameId`, счёт по картам | `closed=false` возвращает и старые нерассчитанные события; без своего `User-Agent` из Python — 403 |
| Polymarket CLOB `prices-history` | История цены токена. С `startTs/endTs` и `fidelity=1` — поминутно | Без окна (`interval=max`) точки редкие (~10 мин) |
| Polymarket Data API `/trades` | Все сделки по рынку с временем, ценой, объёмом | — |
| Polymarket sports WS `wss://sports-api.polymarket.com/ws` | Статус, номер карты, счёт по картам, раз в ~20 с | Раунды почти всегда `000-000`, сторон нет |
| cybersport.ru `/api/matches?filter[date]=…&filter[disciplineIds]=22` | Матчи CS2 за день (дата МСК), команды, турнир, коэффициенты | Неофициальный API |
| cybersport.ru `/api/matches/<id>` | Карты матча: название, счёт, id для связки с раундами | ~260 КБ на матч, `include` игнорируется |
| cybersport.ru `/api/v1/match-stats/csgo?filter[internalId]=<id>` | Раунды: CT/T, победившая сторона и команда, исход раунда; статистика игроков | Есть примерно у 40% матчей; лайв не подтверждён |
| GRID Open Access | Официальные данные CS2, раунды, стороны, история | Нужна заявка; 10 запросов/мин |

`gameId` Polymarket = `externalId` в match-stats cybersport.ru (проверено на 6 из 6 матчей, имена команд совпадают; вероятно, оба берут данные у PandaScore) — ключ для связки матчей.

## Рынки CS2 на Polymarket

`sportsMarketType`: `moneyline` (матч), `child_moneyline` (карта N), `map_handicap`, `totals` (карты), `round_handicap_game_N`, `round_over_under_game_N`, `cs2_odd_even_total_kills/rounds`.

Торговые условия: `secondsDelay = 1` (рыночный ордер ждёт 1 с и не отменяется), taker fee `0.05 × p × (1 − p)` за долю, maker без комиссии.
