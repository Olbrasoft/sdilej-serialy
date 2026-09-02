# Sdílej.cz → Přehraj.to: seriály

Tento projekt z produkční databáze CR **pouze čte** hodnocený katalog seriálů
a jejich epizod. Pro každou epizodu vyhledá zdroj na Sdílej.cz, ověří shodu
`SxxExx`, kvalitu a jazyk, vybere nejmenší vhodný soubor z nejvyšší dostupné
kvalitativní třídy a přímo jej přenese do Přehraj.to. Video se na runner
neukládá.

Před změnou pipeline si přečti [provozní poznatky z filmového přenosu](docs/OPERATIONAL_LESSONS.md).
Shrnují skutečné selhávací scénáře, pravidla obnovy přenosu a odlišnosti pro
seriálové epizody.

## Bezpečnostní hranice

- Databázové připojení se vždy otevře jako `readonly`; program navíc ověří
  `SHOW transaction_read_only` před každým exportem. Do DB neobsahuje žádný
  zápisový SQL příkaz.
- Přihlašovací údaje nejsou v souborech projektu. V CI patří výhradně do
  secrets `DATABASE_URL`, `SDILEJ_EMAIL`, `SDILEJ_PASSWORD`,
  `PREHRAJTO_EMAIL` a `PREHRAJTO_PASSWORD`.
- Cílový účet je kontrolován proti `PREHRAJTO_EMAIL`; pro tento projekt musí
  být nastaven na `share.series@email.cz`.
- Stabilní manifest nikdy neukládá dočasnou autorizovanou download URL.
- `pilot-upload` vyžaduje SHA vytvořeného plánu. Kontinuální upload není
  součástí prvního nasazení.

## První bezpečné spuštění

1. Nastav secrets uvedené výše. `DATABASE_URL` musí ukazovat na produkční CR
   databázi, ale účet musí mít jen právo čtení.
2. Spusť `pilot-plan` s `series_limit=1`, `episode_limit=1` a prostuduj
   `plans/pilot-plan.json`.
3. Spusť `pilot-upload` se stejným SHA a jednou epizodou.
4. Ověř cílové video v Přehraj.to, až poté navyšuj dávku.

## Lokální ověření

```bash
python -m venv .venv
.venv/bin/pip install ".[test]"
.venv/bin/pytest
```

Pro skutečnou přípravu zdroje je potřeba FFmpeg, `faster-whisper` a proměnné
prostředí odpovídající CI secrets. Export katalogu funguje bez Whisperu.
