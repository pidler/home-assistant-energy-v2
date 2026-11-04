# ENERGY V3 — první implementace (pouze preview)

Vychází ze [schváleného návrhu v commitu 51dff57](https://github.com/pidler/home-assistant-energy-v2/blob/51dff571c82fb8c84f9473c4be75e5136c753978/docs/ENERGY_V3_DESIGN.md).
Samostatný PR proti main; PR #29 ani jeho větev se nemění.

## Co lze spustit

`homeassistant/packages/energy_v3.yaml` je standardní HA package pro **izolovanou testovací instanci**.
Tento PR nic nenasazuje. Nepřipojuje se k produkci, nevypíná staré automatizace a neobsahuje fyzický transport.
`input_boolean.energy_v3_enabled` zapíná výhradně simulaci; výchozí stav je off, profil current.
Ani zapnutí všech příznaků a ruční spuštění adapteru nemůže vykonat příkaz do střídače.

Vykonávané služby jsou pouze čtyři `script.energy_v3_*` a služby vlastních `input_boolean/input_select/input_text`.
Adaptéry vracejí seznam dat `{device, entity, value}` a potvrzovací bariéry. Nikde se nepřevádí na dynamické service call.
Událost `energy_v3_preview_result` je pouze diagnostika; v package nemá vykonávajícího odběratele.
Externí automatizace nesmí být na tuto událost připojena jako transport.

## Implementované chování

- **Submit:** jeden sériový vstup optimalizátoru, validace všech polí, konečný TTL do 120 s, explicitní časové pásmo, numerický konečný výkon, idempotence bez prodloužení a zákaz starších požadavků.
- **Reconcile:** sériová logika sedmi režimů rozlišuje `transition`, `check`, `setpoint` a `renew`. Plný HOLD Deye → SolaX STOP → nová operace vznikne pouze při změně režimu. Potvrzení konce SolaXu je vždy před plánem uvolnění Deye.
- **Supervisor:** start, změna profilu/enabled/topology a 20s kontrola. Po startu zruší požadavek, zachová replay watermark a vypne simulaci.
- **Safety:** události zdravotního stavu a 5s kontrola platnosti přes stejný reconcile. Neexistuje druhá zapisující větev.
- **Blokace:** current topology, neověřená asymetrie/nativní export/remote/VPP/SOC/komunikace, neznámá polarita, chybějící nebo zastaralé fyzické vzorky, chybný stav zařízení, směrové SOC rezervy, výkon a nedostatečná zbývající HW lease.
- **Fault latch:** chyba v platném požadavku přejde do návrhu SAFE a nastaví latch. Nový požadavek jej automaticky nemaže. V izolované instanci po odstranění příčiny ručně potvrdit `input_boolean.energy_v3_fault=off`; při reálném řízení bude potřeba provozní akceptace resetu.
- **PCC:** připraven signed `sensor.energy_v3_pcc_export_w`, +export / −import. Výchozí stav unavailable. Žádná náhrada unavailable nulou ani fallback na zónový SolaX. Safety ověřuje raw zdroj a fyzický timestamp při každém průchodu, nezávisle na minutové obnově časových template senzorů.
- **Žádná exportní regulace:** 9800 W je provozní parametr profilu; 10000 W je diagnostický okamžitý alarm, nikoli důkaz porušení či dodržení 15min průměru. Neimplementuje se budget/allocator/PID ani regulace setpointu podle PCC.

## API a uchování stavu

Příklad pro izolovaný test s právě platnými časy:

```yaml
action: script.energy_v3_submit
data:
  mode: SOLAX_SELL
  power_w: 2400
  issued_at: "2026-11-04T16:00:00+01:00"
  valid_until: "2026-11-04T16:02:00+01:00"
  command_id: optimizer-a-123
```

`power_w` je nezáporná **velikost bateriového cíle**, ne PCC ani AC výkon. Nulové režimy SAFE/SELF_USE/PV_AUTO vyžadují 0.
Obchodní vstup má technický strop 12000 W; nejde o schválení takového provozu. Další strop konkrétní baterie musí být vyplněn v ověřeném profilu.
SolaX krok se kvantuje k nule po 100 W, Deye po 12 W / 0,1 % z 12 kW; podkrokový vstup se odmítne.
Pro mode 8 platí +vybíjení, −nabíjení. Deye stejná znaménka zůstávají podmíněná ověřením celého Remote profilu.

Časy jsou ISO 8601 s pásmem, expiry musí být v budoucnosti a nejvýše 120 s od issued_at. Tolerance budoucího issued_at je 2 s.
Jeden producent musí vydávat pro nové ID **striktně rostoucí issued_at**. Shodný čas s jiným ID se odmítne.
Stejné ID a payload je idempotentní; změněný payload se stejným ID je konflikt. Nové prodloužení vyžaduje nové ID a pozdější issued_at.
ID je 1–48 ASCII znaků `[A-Za-z0-9_.:-]`, začíná alfanumerickým znakem.

Jeden `input_text.energy_v3_command` obsahuje kompaktní záznam `i/m/p/t/e/x`: ID, režim, výkon, issued timestamp, expiry, cancelled.
Před zápisem se kontroluje délka 255 znaků. Není zde sada neatomických helperů mode/power/expiry.
Record se obnovuje při restartu kvůli watermarku, nikoli obnovení operace. Startup nastaví x=true; staré ID nelze znovu aktivovat.
Nevymazávat tento helper ani jeho recorder/restore historii za účelem resetu provozu. První instalace nemá historii před tímto systémem; globální archiv všech ID se záměrně nezavádí.
Poškozený watermark je blokace vyžadující kontrolovaný reset v testovací instanci, nikoli pokyn přeskočit replay ochranu.

`accepted=true` znamená **platný uložený požadavek**, nikoli povolenou nebo provedenou operaci. Pro rozhodnutí číst i `reason` a `preview_mode`.
`accepted=false` ponechá poslední přijatý požadavek beze změny. Idempotentní retry nevytváří další plán.
Přímé volání soukromých adaptérů není API optimalizátoru, i tak pouze vrací data.

## Profily a diagnostika

Konfigurace je na jediném místě: atribut `profile` senzoru `sensor.energy_v3_configuration` v YAML.
Není zde druhý konfigurační soubor, Python konfigurátor ani několik desítek helperů.

| Profil | Kandidát měření | Co lze tvrdit |
|---|---|---|
| current (výchozí) | sensor.solax_measured_power | Dnešní hranice není v této implementaci nově ověřena; veškeré PV_AUTO a obchodní preview je blokováno |
| asymmetric | sensor.deye_external_power | Autorita PCC až po fyzickém ověření polohy CT, polarity a timestampů; SolaX je pouze zóna |

Přepnutí selectu samo nic neověří. Kromě `topology_verified` musí být v profilu výslovně doplněny potvrzené vlastnosti, znaménko ±1,
fyzický timestamp atribut a maximální stáří vzorku, zdravé SolaX stavy, oba SOC rozsahy, výkony, proudy, Deye Remote profil/watchdog a SolaX PV limit/timeout.
Výchozí hodnoty jsou `false`, `null` nebo prázdné seznamy. **Nejsou to předvolby k obejití bezpečnosti.**
Příznak `communication_verified` znamená, že časový atribut skutečně dokládá fyzický vzorek; `last_changed/last_updated` zde nejsou použity jako jeho náhrada.
Testy používají smyšlený `sampled_at` a `TEST_NORMAL`, ne návrh existujících produkčních atributů.

Šest helperů je nezbytných pro preview: enabled, topology_verified, fault, topology a dva kompaktní záznamy command/applied.
`input_text.energy_v3_applied` nahrazuje původní `input_select.energy_v3_preview_mode`; atomicky obsahuje `m` (simulovaný režim), `p` (kvantovaný BAT cíl ve W) a `u` (simulovaný konec krátkého příkazu, UNIX čas).
Applied se při startu resetuje na UNKNOWN/0/0, neobnovuje se z historie. Reconcile čte přímo tento záznam, nikoli opožděnou diagnostickou událost.
`sensor.energy_v3_request` ukazuje požadavek; odvozený `sensor.energy_v3_preview_mode` pouze simulovaný aplikovaný režim.
`sensor.energy_v3_status` je vždy **DISABLED**. V atributech má poslední rozhodnutí, důvod fault, pořadí plánu, `executed=false` a `hardware_confirmed=false`.
Žádný simulovaný HOLD nebo SAFE není prezentován jako fyzicky dosažený stav.

## Opakované kontroly, setpoint a obnova

| Situace | Výstup reconcile |
|---|---|
| Jiný režim včetně odchodu do SAFE | `transition`: plná bezpečná sekvence, poté jednorázové uložení simulovaného výsledku |
| Stejný režim a stejný kvantovaný výkon | `check`: prázdný plán, žádné volání adaptérů ani přepis applied |
| Stejný režim, jiný kvantovaný výkon | `setpoint`: pouze cílové number a potvrzovací bariéry; žádný HOLD, Disabled, TOU ani opětovná aktivace režimu |
| Stejná operace, nové command_id/valid_until | Aktualizuje se pouze request; ID není klíč přechodu a samo neposouvá `u` |
| Krátký příkaz potřebuje obnovit | `renew`: pouze ověřený refresh, pokud dosud nevypršel, nastal ověřený předstih a celý nový interval se vejde do optimizer lease |
| Expirace, ztráta zdraví nebo fault | Jednou přechod na simulované SAFE; další tiky pouze kontrolují. Skutečné HW dosažení SAFE není potvrzeno |

Nové `solax/deye_refresh_verified` a `solax/deye_renew_before_s` jsou výchozí false/null.
Deye navíc vyžaduje potvrzené `deye_refresh_via=watchdog|setpoint`: 1101 status sám nepotvrzuje, jaký zápis skutečně obnoví watchdog.
Předstih musí být kladný, menší než délka příkazu a musí být provozně ověřen; testovací hodnoty nejsou produkční doporučení.
SolaX obnovovací plán znovu odešle direct mode 8 (jeden celý blok integrace), bez Disabled nebo přípravy všech parametrů.
Deye obnoví pouze zvolený ověřený registr; režim, proudové limity a AC coupling nepřepíná.

Změna SolaX direct setpointu sama posílá celý blok. Simulovaný čas se při ní prodlouží pouze s ověřenou refresh sémantikou.
U Deye s refresh přes 1101 samotná změna 1109 neposouvá `u`; pokud je současně skutečně nutná obnova, plán obsahuje setpoint a samostatný watchdog refresh.
Změna raw požadavku uvnitř stejného HW kvanta nevytváří zápis setpointu.
Pokud optimizer lease na celý další interval nestačí, `check` s důvodem `renewal_waiting_for_longer_lease` neobnovuje příkaz. Pozdější prodloužení může povolit obnovu bez STOP/START.
Při dosažení `u` bez obnovy nebo zkrácení optimizer lease pod již naplánovaný interval vznikne jednorázový SAFE a fault; expirovaná HW operace se potichu znovu nespouští.

`simulated_applied` a `change` jsou součástí diagnostiky. `hw_readback` zveřejňuje zvlášť SolaX HW VPP senzor, pozorovanou Deye remote select hodnotu a oba bateriové výkony.
Deye select je pouze pozorování, nemusí být nezávislým HW potvrzením. Žádný z těchto údajů se nepřepisuje simulovaným režimem.
Obnovovací potřeba je zde **simulovaná** podle `u`, nikoli změřený HW odpočet. `hardware_confirmed=false` a `actual_mode=DISABLED` zůstávají vždy.
Budoucí transport musí místo úspěšného vytvoření plánu použít skutečné potvrzení zápisu a přečteného stavu; samotné připojení service call k plánu není autorizace fyzického provozu.
Konfigurační profil během operace neměnit; nejprve disabled/SAFE, potom úprava a nové ověření profilu.

## Co je pouze připravené

| Část | Připraveno | Záměrně neimplementováno |
|---|---|---|
| Deye HOLD | Nulový cíl, oba proudy 0, potvrzovací bariéry, remote off | Zápisy, měření skutečné blokace, reboot/persistence akceptace |
| Deye obchod | 1104 Battery-side, 1105 Power, 1109 procenta, 1100 profil, 1101 watchdog, směrové proudy a oddělené setpoint/refresh plány | Přenos a fyzické potvrzení obnovovacího registru; 1110 není domnělá SOC ochrana |
| PV_AUTO | Kandidátní celý nativní profil včetně TOU, AC coupling Grid, zákaz běžného Grid Charge a vybíjení | Potvrzení optimálního profilu v instalaci a práce s jeho fyzickým fallbackem |
| SolaX | Direct mode 8, příprava parametrů při VPP off, aktivace poslední, BAT target a Disabled | Fyzické FC16, readback/timeout/restart test, obnova krátkého příkazu za běhu |
| Interlocky | Logická výlučnost a správné pořadí bariér všech přechodů | Vykonávání/čekání na skutečné HW potvrzení, rollback reálného selhání |
| Nativní limity | Commissioning gate a PCC alarm | Důkaz Max Sell Power = celý PCC při Remote Mode, další HA regulátor |

Návrh SolaX příkazu vznikne jen při dostatečné zbývající době pro duration 60 s + potvrzený timeout; Deye analogicky pro watchdog.
To je preview kontrola, nikoli potvrzení jeho skutečné časové sémantiky. Periodický tick může připravit jen potřebnou a ověřeným profilem povolenou simulovanou obnovu; žádný HW příkaz neodesílá.
Před budoucím přidáním transportu bude nutné znovu zkontrolovat délku zbývající lease přímo před aktivací a všechny bariéry fyzicky splnit.
Samotné přidání `number.set_value` do dnešního plánu není schválený způsob zprovoznění.

## Testy a hranice ověření

```text
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_energy_v3_package.py -q

# Izolovaný Linux, Python >=3.14.2:
python -m pip install homeassistant==2026.10.4 pytest==8.4.2
python -m pytest tests/test_energy_v3_package.py tests/ha_energy_v3 -q
```

CI job `energy-v3-preview` používá skutečný Home Assistant 2026.10.4; běžný projektový Python 3.11 tuto samostatnou sadu přeskočí.
Runtime testy načtou místní helpery, template a skripty, ověří schéma automatizací a vykonají **dodávaný YAML** skutečným HA enginem.
Nevytvářejí druhou implementaci řídicí logiky v Pythonu. Kontrolují i skutečný seznam volaných HA služeb.

Scénáře: všech 7×7 přechodů, nuly Deye před SolaX VPP, konec VPP před uvolněním proudů,
vstupní typy/NaN/Inf/rozsahy/časová pásma, replay/idempotence/konflikt, default off,
current/neověřená asymetrie, SOC floor/ceiling, výpadek zařízení, chybějící/stará PCC data,
exportní alarm, expiry a krátká lease, restart bez replay, opakované tiky supervisor/safety bez přechodu,
změny setpointu a kvantování, nová ID/prodloužení lease, potřebné a zakázané obnovy, jednorázový SAFE po fault/expiry a žádný fyzický service call.

**Provozní ověření nadále čeká** na body T1–T11 návrhu: polarita a hranice PCC, spolupráce nativních limitů v Remote/VPP,
celodoby HOLD včetně timeoutů a restartů, skutečné SOC/BMS limity, fyzická čerstvost/latence, writer inventory a smluvní 15min výpočet.
Žádný test tohoto PR nemůže tyto vlastnosti firmware nebo zapojení potvrdit.
