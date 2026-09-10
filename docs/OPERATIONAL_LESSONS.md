# Provozní poznatky z filmového přenosu

Tento dokument zachycuje problémy, které se objevily při zprovozňování
produkčního přenosu **Sdílej.cz → Přehraj.to** pro filmy. Slouží jako předání
pro seriálový projekt. Nejde o návod, jak obcházet omezení služeb: cílem je
bezpečný, dohledatelný a obnovitelný přenos schválených položek.

## Nejdůležitější pravidlo

Zdroj lze považovat za správný až po ověření **identity, jazyka, kvality a
úplného dokončení přenosu**. Název vyhledávače ani odpověď HTTP samotné nestačí.

## Co se v provozu stalo a jak tomu předcházet

| Problém | Projev | Ochrana pro seriály |
| --- | --- | --- |
| Přenos skončil chybou, přesto se video v cíli vytvořilo. | Opakovaný pokus by založil duplicitní video. | Před opakováním vždy hledej přesnou shodu jména v seznamu nahraných videí a ověř statistiku / detail cíle. Když existuje, zapiš úspěch jako reconciliaci. |
| Runner byl nahrazen nebo workflow skončilo během přenosu. | Ve stavu zůstal aktivní lease a další běhy přeskočily položku až do vypršení lease. | Claim musí obsahovat `run_id`; nový serializovaný běh smí uvolnit claimy jiného běhu. Lease zůstává pojistka, ne jediný způsob zotavení. |
| Sdílej poskytuje dočasné autorizované URL. | URL přestala platit mezi přípravou a uploadem. | Do manifestu ukládej jen stabilní detail URL a identitu souboru. Těsně před přenosem obnov detail a znovu ověř, že `source_id` i stabilní URL sedí. Nikdy neukládej download nebo sample URL. |
| Zdroj selhal dočasně. | Přenos skončil například `SdilejError` nebo `PrehrajtoError`, ale další pokus mohl fungovat. | Chyby rozlišuj na dočasné a trvalé; dočasné zdroje opakuj s omezeným cooldownem. Po neúspěchu smaž prepared/claim stav, aby nezablokoval další pokus. |
| Předčasně zvolený zdroj měl nesprávný jazyk nebo nekvalitní stream. | Text v názvu („CZ“) nebyl dostatečným důkazem. | Jazyk ověř vzorkem zvuku a při rozporu s názvem použij více vzorků. Uchovej důkaz, pravděpodobnost a jazykovou třídu. |
| Vyšší kvalita nešla ověřit. | Automatické snížení kvality by potichu vybralo horší nebo neověřený soubor. | Pokud nelze ověřit kandidáta v nejvyšší dostupné třídě, epizodu odlož a vrať se k ní později; nesnižuj kvalitu bez explicitní politiky. |
| Přenosy velkých souborů byly pomalé. | V jednom okamžiku běželo méně efektivních přenosů, než dovolovalo nastavení workerů. | Měř skutečné claimy, postup bajtů a potvrzené výstupy, ne pouze počet vytvořených workerů. Cílová služba může přijímat méně paralelních přenosů, než je nastaveno. |
| GitHub Actions nepřinesl živý log běžícího uploadu. | `gh run view --log` poskytne log až po dokončení běhu. | Vypisuj stručný průběh průběžně a hlavně po každé změně stavu persistuj checkpoint do repozitáře. Stav musí být zdrojem pravdy i bez live logu. |
| Běhy Actions čekaly nebo byly rušeny kvůli concurrency. | Nové plánované běhy zůstaly pending; čekající běh může být nahrazen novějším. | Odděl concurrency skupinu přípravy zdrojů od skupiny uploadu. U uploadu nikdy nespouštěj dva běhy nad stejným cílovým účtem. Stav udržuj odolný proti tomu, že běh skončí bez cleanupu. |
| Lokální kopie `state` zaostala za vzdáleným stavem. | Lokálně bylo vidět méně uploadů než na účtu nebo v posledním checkpointu. | Diagnostiku prováděj nad `main` / vzdáleným checkpointem a při nejistotě ověř i cílový seznam nahraných videí. |

## Odlišnosti pro seriálové epizody

### Identita epizody je silnější než název

Film se často dá určit názvem a rokem. U epizod to nestačí. Trvalá identita má
obsahovat alespoň katalogové ID série, katalogové ID epizody, sezónu a číslo
epizody. Zobrazované jméno není identifikátor stavu.

Při vyhledávání a před uploadem vyžaduj přesný kód `SxxExx` nebo `xxYyy`.
Shoda názvu seriálu bez přesného kódu je nedostatečná. Zvláštní pozornost:

- jednociferné zápisy (`S1E2`, `1x02`),
- trojciferná čísla epizod,
- vánoční speciály a epizody `S00E…`,
- dvojepizody a soubory se dvěma kódy,
- stejné názvy seriálů, remaky a lokalizované názvy,
- úplné série nebo celé sezóny: ty se nesmí vydávat za jednotlivou epizodu.

Kandidáta s více kódy epizod nebo bez jednoznačného kódu zařaď do ruční
kontroly, dokud pro něj nevznikne výslovné pravidlo.

### Pořadí a názvy

Do názvu cílového videa vždy zahrň název seriálu, kanonický kód sezóny a
epizody a rozlišující název epizody, je-li dostupný. Příklad:

`Název seriálu S02E03 - Název epizody 1080p CZ Dabing`

Toto jméno je nutné tvořit deterministicky. Reconciliace podle názvu funguje
jen tehdy, když se stejná epizoda při opakovaném běhu pojmenuje stejně.

### Stav po epizodách

Stav, claimy, pokusy a výsledky musí být vedené po `episode.identity`, nikoli
jen po názvu seriálu, sezóně nebo souboru na Sdílej.cz. Do záznamu úspěchu
patří minimálně:

- identita epizody a display name,
- stabilní identita a URL zdroje,
- ID videa v Přehraj.to,
- velikost, rozlišení a jazyková evidence,
- čas a způsob potvrzení dokončení.

Připravené cílové video zaznamenej okamžitě po jeho vytvoření. Pokud následný
přenos nebo potvrzení selže, při dalším běhu nejprve ověř, zda toto ID již
existuje a odpovídá přesnému názvu.

### Fronta a paralelismus

Seriály mají podstatně více položek a obvykle menší soubory než filmy. To
zvyšuje význam plánování fronty:

- připravuj zdroje průběžně, nezadržuj upload čekáním na celé série,
- workery naplňuj ze sdílené fronty, ne pevným rozdělením jedné dávky,
- po dokončení rychlé epizody okamžitě doplň další ověřenou položku,
- respektuj skutečný limit cílové služby; vyšší počet vláken není sám o sobě
  vyšší výkon,
- neprováděj paralelní upload téhož názvu ani stejné identity.

Bezpečný počáteční limit jsou malé pilotní dávky. Zvyšuj jej až po ověření,
že počet claimů, počet cílových videí a uložený stav souhlasí.

## Kontrolní seznam pro dalšího agenta

Před změnou pipeline si ověř:

1. zda DB přístup zůstává striktně read-only;
2. zda manifest neobsahuje dočasné URL ani tajné údaje;
3. zda matcher odmítá epizodu bez přesné shody série a `SxxExx` / `xxYyy`;
4. zda upload před vytvořením cíle i po výjimce provádí exact-name
   reconciliaci;
5. zda každý claim lze bezpečně uvolnit po pádu předchozího běhu;
6. zda se stav ukládá při claimu, přípravě cíle, úspěchu i selhání;
7. zda testy pokrývají duplicitní epizodu, stale claim, změněný zdroj,
   dvojepizodu a dočasnou chybu přenosu.

## Co nedělat

- Nehádat sezónu nebo číslo epizody z pořadí výsledků vyhledávání.
- Nepřepisovat úspěšný stav jen proto, že se zdroj už nedá obnovit.
- Neoznačovat upload za hotový pouze po HTTP odpovědi upload endpointu.
- Neuchovávat hesla, cookies ani download URL v commitu nebo artefaktu.
- Nezvyšovat počet workerů bez kontroly, kolik přenosů cílová služba skutečně
  přijímá.
- Nespouštět souběžně dva upload runy proti stejnému účtu.

## Duplicate prevention after the September 10 incident

- The target sanitizes punctuation, including ellipses. A full display-name
  lookup can miss an existing upload. Search by series title and episode code,
  and extract the ID from the same listing row as the heading.
- A target that is still processing already occupies its episode identity.
  Presence is not proof of playable media, but must prevent another upload.
- Persist creation intent before calling the relay and retain prepared target
  IDs on every failure. An uncertain result requires reconciliation; never
  clear it merely to make a retry create another video.
- Catalog IDs are not sufficient to prevent duplicate uploads: reserve the
  normalized series-title/season/episode key atomically across catalog aliases.
- Same localized titles can also describe distinct series. For destructive
  cleanup, check source evidence when subtitles or catalog identities differ;
  leave ambiguous pairs untouched.
- Before cleanup, stop scheduled uploads, save the complete inventory and
  an explicit retained ID for each group, and verify that retained IDs never
  occur in the deletion set. Recheck each group after deletion.

## Original media verification

Read the fast-download button from the authenticated detail page. Resolve its
redirects with the source account session and use the returned original-media
URL for both ffprobe and Whisper. Do not infer CDN hostnames or require a
preview-player URL: previews can have a lower resolution than the original.
Read the total byte size from Content-Range (not the one-byte response length)
when probing with a range request. Signed URLs remain transient and are never
written into the manifest.

Do not apply fixed bitrate floors to series. They incorrectly rejected compact
Czech animation at 900x720 and selected larger SD originals instead. Require
positive original dimensions, duration, and byte size, plus episode/runtime and
audio verification. Rank Czech originals by resolution tier descending and
exact file size ascending, regardless of codec or audio channel count.

Search both exact episode queries and bare Czech/original series titles, follow
result pagination, and reapply strict identity matching to each result. Known
alternative release numbering belongs in `numbering.py`, with negative tests
for neighboring episodes, bundles, and other series. Planet Earth II releases
use both II-02 and franchise S2E02 for catalog S01E02; an explicit subtitle and
sequel identity are required for that mapping. Never infer a season from an
unqualified bare number in an arbitrary series.

## Pending-source revalidation

Every newly prepared manifest row carries `quality_policy=original-media-v4`.
The uploader rejects older policy versions and foreign audio. A freshly
reviewed SD source is eligible when no better acceptable Czech source was
found. The producer prioritizes rechecking every stale pending selection,
including 1080p: a search preview can hide a 4K original. Existing upload
records, claims, and prepared targets are not cleared. Historical source
selections remain recoverable through Git, rather than deleting queue history.

Inspect original resolution and exact byte size for all matching candidates
before ranking. Check Czech audio in descending resolution and ascending size
order. An unresolved original or better/smaller language candidate defers the
episode instead of silently downgrading it. Audio channel count has no ranking
bonus. A fresh policy stamp certifies this search, not perpetual freshness if
new sources are added to Sdilej later.
