# ENERGY V3 — dokončení technického auditu PR #29

Stav: návrh po read-only auditu **2026-11-04**, BLOCKED_FOR_ACTIVATION.
Repo baseline: `688cb6b32dc242f35275720c64955cd4f866e85a`, větev `codex/energy-v3-simple-design`.
Mění se pouze tento dokument. Žádné nastavení, registr, automatizace ani fyzické zapojení nebyly změněny.

Zařízení: SolaX X3-HYBRID-12.0-D G4.2, Deye SUN-12K-SG05LP3-EU; dřívější návrh uvádí SM2, přesný štítek a MAIN/HMI firmware znovu potvrdit.
Každý má vlastní baterii, oba používají GRID port. Přesun SolaX CT dosud neproběhl.

## 1. Výsledek a síla důkazů

ENERGY V3 má **koordinovat režimy**, regulaci výkonu ponechat střídačům.
Architektura zůstává **optimalizátor → čtyři HA skripty / dvě automatizace → integrace → střídače**.
Žádný Python controller, allocator, LP planner ani průběžný HA exportní regulátor.

Značení závěrů platí v celém dokumentu:

| Značka | Význam a hranice důkazu |
|---|---|
| **DOK** | Potvrzené z uvedené dokumentace; neprokazuje chování konkrétního firmware |
| **KÓD** | Potvrzené z právě přečteného instalovaného kódu / konfigurace; neznamená úspěšný provozní test |
| **UŽIVATEL** | Prakticky ověřené uživatelem v této instalaci |
| **DEDUKCE** | Technický závěr z bilance nebo doložených vlastností |
| **NEOVĚŘENO** | Předpoklad či otevřená vlastnost vyžadující uvedený test |

Rozhodnutí:

- **UŽIVATEL:** Deye umí nabíjet z AC přebytků SolaXu přes GRID i bez běžného síťového nabíjení. Tuto schopnost znovu nedokazujeme.
- **UŽIVATEL, upřesnění v této relaci:** omezení nabíjecího proudu na 0 i vybíjecího proudu na 0 funguje. **Výchozí spolehlivý HOLD bude oba proudové limity = 0**, pouze na přechodech. Přesná priorita vůči Remote Mode, restart a persistence ještě nejsou doloženy.
- **KÓD / NEOVĚŘENO:** Remote mode 3 + Battery-side + Power + 1109 = 0 existuje v produkčních testovacích skriptech; jeho rovnocennost k obousměrnému HOLD není tím potvrzena. Je to kandidát pro pozdější zjednodušení.
- **DOK / KÓD:** preferovaná SolaX cesta je **direct VPP mode 8, bateriový cíl + konečná doba**, bez autorepeat smyčky integrace. Přesné timeout/restart chování a priorita exportního limitu při VPP zůstávají akceptačním testem.
- **NEOVĚŘENO:** dvě hodnoty 9800 W samy nejsou důkazem společného PCC limitu. U Deye chybí důkaz, že Max Sell Power zahrne externí SolaX i při 1109. **Nenahrazujeme tento chybějící důkaz HA regulátorem.**
- **NEOVĚŘENO:** fyzická polarita PCC není uzavřena. Živé HA API vrací 401; žádná historická hodnota není vydávána za živé měření.

## 2. Topologie a fyzikální meze

```text
DISTRIBUČNÍ SÍŤ
       |
 Deye CT/Meter — skutečný PCC
       |
       +---- Deye GRID ---- Deye baterie
       |
 SolaX CT/Meter — hranice zóny
       |
       +---- SolaX GRID ---- PV + SolaX baterie
       |
       +---- DŮM
```

Definice: `E_z` = export ze zóny SolaX (SolaX AC dodávka minus dům);
`D` = dodávka Deye z GRID portu, kladně ven, záporně odběr;
`E_PCC = E_z + D`. Ztráty a časový nesoulad měření jsou diagnostická tolerance.
**DEDUKCE:** SolaX CT nevidí Deye, proto nemá jeho nabíjení či prodej kompenzovat. Deye vidí celý objekt, ale nepozná původ přebytku PV versus SolaX baterie.

**DEDUKCE:** k dodržení 9800 W stačí současně splnit `E_z ≤ 9800` a `D ≤ 9800 − E_z`.
Nejde o součet dvou samostatně povolených 9,8kW zdrojů. Při 4000 W ze zóny smí Deye přidat nejvýše 5800 W **na AC větvi**; to není bateriový setpoint 5800 W bez ztrát.
Při `E_z > 9800` by bylo třeba záporné D (absorpce) nebo omezení SolaXu. Pouhé snížení Deye dodávky na nulu nestačí.

**DOK / DEDUKCE:** SolaX Export Control používá hlavní meter/CT. Po přesunu tedy logicky řídí hranici své zóny.
Standardní [SolaX Meter Installation](https://kb.solaxpower.com/solution/detail/ff8080818407e2a701840a15af480028) umísťuje měření před všechny zátěže a další výrobny; výslovné schválení vyjmutí Deye není doloženo.
Schéma není montážní dokumentace; EPS/LOAD, ATS, fázování a jištění musí posoudit projekt instalace.

## 3. PCC — co měří konkrétní entity

**KÓD:** následující entity jsou v registru s `disabled_by=null`. To nepotvrzuje jejich live dostupnost.
Solarman instaluje tyto signed power mapy (low/high word, W):

| Entita | Registry instalované definice | Význam | Polarita a autorita |
|---|---|---|---|
| `sensor.deye_external_ct1_power` | 616 / 705 (0x268 / 0x2C1) | Externí CT fáze L1 | Fyzické znaménko NEOVĚŘENO |
| `sensor.deye_external_ct2_power` | 617 / 706 | Externí CT fáze L2 | Totéž; ověřit pořadí fází |
| `sensor.deye_external_ct3_power` | 618 / 707 | Externí CT fáze L3 | Totéž |
| `sensor.deye_external_power` | 619 / 708 | Celkový výkon externích CT | **Preferovaný PCC zdroj po ověření skutečné polohy CT a polarity** |
| `sensor.deye_grid_power` | 625 / 690 (0x271 / 0x2B2) | Vnitřní měření GRID větve Deye | Není externí CT; nepoužívat jako celkový PCC |
| `sensor.solax_measured_power` | input 0x46, S32, gain/offset | Hlavní meter/CT SolaXu | Po přesunu výhradně zóna, nikdy PCC |

Mapování je **KÓD**, přiřazení fyzické měřicí hranice je **DEDUKCE podmíněná zapojením**.
Ani dnešní external CT nelze bez ověření polohy automaticky označit za fakturační PCC.
Součet CT1+CT2+CT3 má odpovídat total při sladěných vzorcích; total a součet nejsou dva nezávislé důkazy stejné polarity.

### Rozpor znaménka SolaXu je uvnitř instalované integrace

**KÓD:** `plugin_solax.py:534` a preview komentují `measured_power` jako +import.
Současně `const.py:597–620` pouze aplikuje `(raw + offset) × gain / 100`,
`value_function_grid_import` vybírá **záporné** hodnoty a `value_function_grid_export` **kladné**.
Energy Dashboard mapping `plugin_solax.py:13391` má `invert=True`.
Výchozí gain 100 %, povolený 90–110 %, sám znaménko neobrací.

**Závěr: NEOVĚŘENO fyzicky.** Původní tvrzení „instalovaný plugin potvrzuje +import“ je příliš silné.
Komentář, historický README ani matematika odvozené entity nevyřeší polaritu skutečného měření.
U Deye definice neobsahuje u external power explicitní invert; signed dekódování samo význam znaménka neurčuje.
`attributes: [inverse]` u grid power nelze bez zkoušky zaměnit za přepólování jeho hlavního stavu.

### Normalizovaná entita

Návrh `sensor.energy_v3_pcc_export_w`: **signed W, kladně export, záporně import**.
Po potvrzení CT zdroj výhradně `sensor.deye_external_power`:

```text
energy_v3_pcc_export_w = k × deye_external_power
k = -1, pokud zdroj znamená +import; k = +1, pokud znamená +export.
```

**NEOVĚŘENO:** k zatím nemá schválenou hodnotu, entitu nyní nezavádět.
Nepoužít `float(0)` pro unavailable, žádný automatický fallback na SolaX či Deye grid power.
Při chybě/nezaručené čerstvosti publikovat unavailable, ukončit nucený obchod.
Atributy: zdroj, ověřená topologie a k, čas fyzického vzorku/pollu, quality.
Pro energii použít samostatně `max(E_PCC,0)`; signed entita musí uchovat informaci o importu.

**Potřebná read-only korelace:** nezávislý směrový údaj elektroměru/analyzátoru a současné vzorky všech šesti entit při známém importu a exportu, se zaznamenaným přirozeným výkonem Deye.
Po přepojení ověřit `E_PCC ≈ E_z + D`, součet fází, reakci zóny a časový posun.
Pouhé jedno klidové 0 W měření nestačí; v této relaci nebyla změnou zátěže ani režimu vyvolána žádná zkouška.

## 4. Nativní exportní ochrana 9800 W

### Co lze doložit a co nikoli

| Otázka | Závěr a důkaz | Co ještě ověřit |
|---|---|---|
| SolaX Export Control omezuje měřenou hranici? | **DOK:** [SolaX návod pro X3-Hybrid G4](https://kb.solaxpower.com/faq/detail/8af1adf7930b02ff019319e9fc680183) váže funkci na správné CT/meter. **KÓD:** Export Control User Limit je holding 0x42, X3 scale 10, HA W. **DEDUKCE:** po přesunu hranice = zóna. | Skutečná konfigurace 9800 W, fáze/součet, VPP priorita a porucha měření na G4.2 |
| Deye Max Sell Power = celý PCC? | **DOK:** [SM2 manuál, tištěná s. 38 / PDF s. 40](https://www.deyeinverter.com/deyeinverter/2025/08/19/%E3%80%90b%E3%80%91manual_sun-3-12k-sg05lp3-eu-sm2_20250819_en.pdf) popisuje maximum dodávky do sítě; nerozlišuje výslovně tuto externí výrobnu. **NEOVĚŘENO:** limit na součet externího SolaXu + Deye není prokázán. | Externí výroba 0 / 4000 / 9800 W, stálý vyšší požadavek Deye, současné PCC a vlastní AC |
| Platí při Remote 1109? | **KÓD:** Solarman zapisuje registry, firmware regulátor neimplementuje. **NEOVĚŘENO:** priorita Max Sell Power vůči remote bateriovému požadavku. | Stejný test nativně a v každém skutečně používaném Remote mode; nelze přenést výsledek mezi mode 1/2/3 |
| SolaX sám dosáhne 9800 W? | **DEDUKCE:** Deye smí mít nejvýše nulovou kladnou dodávku. | Zda Deye snižuje svůj výkon podle externího CT, včetně malých nesymetrií |
| SolaX překročí limit? | **DEDUKCE:** Deye může přebytek absorbovat jen s dostupnou kapacitou/výkonem a povoleným nabíjením. Přes GRID neexistuje doložená řídicí vazba na SolaX. | Export musí zvládnout vlastní SolaX limiter i s plným nebo blokovaným Deye |
| Plná Deye baterie, ztráta CT, restart, změna režimu? | **NEOVĚŘENO:** nejsou doloženy fail-safe reakce ani priority všech limitů. | Samostatné scénáře T4/T5; odpojení komunikace CT nelze zaměnit za fyzicky obrácené CT |
| Rychlost a překmity? | **NEOVĚŘENO:** žádná naměřená odezva ani garantovaný překmit pro tento firmware. | T6, nezávislý rychlý záznam; HA polling není rychlost vnitřní regulace |

**KÓD — zásadně různé Deye entity:**

| Funkce | Entita / registr | Význam v návrhu |
|---|---|---|
| Max Sell Power | `number.deye_export_surplus_power`, 143 / 0x8F | Kandidát pro pevné nastavení 9800 W; není to následující registr |
| Grid Max Export Power | `number.deye_grid_max_export_power`, 231 / 0xE7, scale 10 | Jiný parametr; nepoužít jako synonymum Max Sell Power |
| Zero Export To CT | `select.deye_work_mode`, 142 = 2 | Nativní režim s externí CT zpětnou vazbou; nelze jej automaticky chápat jako „export 9800 W“ |
| Solar Sell | `switch.deye_export_surplus`, 145 | Povolení prodeje přebytků, samo není číselný limit |
| AC coupling Grid | `select.deye_ac_coupling`, 234, bit 0 | Využití externí AC energie, nikoli povel SolaXu ke snížení výroby |

**DOK:** manuál odlišuje Selling First, Zero Export To Load a Zero Export To CT.
U posledního popisuje externí CT regulující výkon **Deye**; TOU povoluje časové nabíjení/vybíjení.
Z toho neplyne priorita všech funkcí při Remote Mode ani pravomoc regulovat cizí střídač.
GEN/microinverter frekvenční omezení není důkazem omezení SolaXu připojeného na společný GRID se sítí.

### Rozhodovací pravidlo

**Návrh / NEOVĚŘENO:** SolaX Export Control = 9800 W a Deye **Max Sell Power** = 9800 W jsou preferovaná statická nastavení pro akceptační test. Dnes nebyla změněna ani jejich skutečná hodnota přečtena.
Při doložení společné funkce je ponechat a v HA pouze monitorovat, koordinovat režimy a ukončovat nucený obchod při poruše.
**Původní vzorec HA regulátoru target += 9800 − PCC se z návrhu odstraňuje.**
Pokud Deye omezuje pouze vlastní dodávku nebo Remote limit obchází, zůstává DEYE_SELL v této kombinaci nepovolený.
Neřešit to automaticky novou regulační vrstvou; nejdříve potvrdit výrobcem či testem správný nativní profil.

**DEDUKCE:** plný Deye sám není důvod pořizovat další ochranu, pokud SolaX prokazatelně drží svou zónu a Deye nepřidává nad zbývající prostor.
Požadavek je prokázaná ochrana bez HA; další hardware ani software není automatickou podmínkou.
Naopak zastavení obchodů v HA nenapraví vadný lokální SolaX limiter při autonomní výrobě.

### Okamžitý výkon a čtvrthodina

**Požadavek uživatele:** provozní cíl 9800 W; maximální 15min průměr exportu 10000 W.
**DEDUKCE:** za 900 s odpovídá 9800 W energii 2,45 kWh, 10000 W energii 2,50 kWh.
Rezerva 200 W představuje za plnou čtvrthodinu pouze 50 Wh; není zárukou zvládnutí libovolného překmitu.
Krátký překmit sám nedokazuje překročení 15min limitu a dodržení průměru negarantuje okamžitý strop.
**NEOVĚŘENO:** pravidlo distributora (pevná čtvrthodina / rolling, signed net / export-only, součet fází).
Do potvrzení sledovat export-only energii i signed bilanci s časovým vážením a označením neúplného okna.
Výpadky vzorků nejsou nula. Historie po restartu není plná čtvrthodina.
Jde o diagnostiku, nikoli nový aktivní budget controller.

## 5. Deye Remote Mode a výběr blokace

**DOK:** [Deye třífázový protokol, PDF s. 41–42](https://download.axilogi.com/DEYE/Other/MODBUS_RTU.pdf) uvádí:

| Registr | Protokol a instalovaný KÓD |
|---|---|
| 1100 | Disabled, Remote mode 1/2/3; význam rozdílů není vysvětlen |
| 1101 | Watchdog 10–18000 s; 65535 vypíná. Vypršení opustí remote, neznamená HOLD |
| 1104 | AC-side 0, Battery-side 1; Grid-side 2 je v poznámce v rozporu s rozsahem 0–1 a není vystaven |
| 1105 | Battery control; instalace vystavuje jen Power = 2 |
| 1109 | Bateriový power setpoint, signed, 0,1 % jmenovitého výkonu |
| 1110 | SOC setpoint, 0,1 %; neprokazuje SOC ochranu strategie Power |
| 1111 | Samostatný celkový AC output setpoint; není totéž co 1109 |

**KÓD:** remote skupina se čte po 30 s, zápisy FC16. Vystavené entity:
`select.deye_remote_mode`, `select.deye_remote_power_control_mode`,
`select.deye_remote_battery_side_control_strategy`, `number.deye_remote_constant_power_setpoint`,
`number.deye_remote_constant_soc_setpoint`, `number.deye_remote_watchdog`,
`sensor.deye_remote_watchdog_status`.
Watchdog status je dekódování nastavení 1101, nikoli důkaz běžícího odpočtu nebo obnovené lease.
Který zápis skutečně obnovuje watchdog, protokol neupřesňuje.

**DEDUKCE:** při potvrzeném jmenovitém základu 12 kW je 1 raw krok 12 W, procenta = W / 120, raw = W / 12.
+2400 W by znamenalo +20 % / raw +200 **jen při potvrzené polaritě vybíjení**.
**KÓD:** testovací skript předpokládá +vybíjení / −nabíjení; fyzická validace v této relaci chybí.
Rozsah ±120 % není provozní povolení ±14,4 kW.

| Varianta | Oba směry / přechody | Zápisy, paměť, timeout | Rozhodnutí |
|---|---|---|---|
| Oba max battery current = 0 | **UŽIVATEL:** směrové nuly fungují; oba použít pro HOLD | Dva změnové zápisy při vstupu, až dva při obnově; bez heartbeat. EEPROM/persistence a restart NEOVĚŘENO | **Preferovaný výchozí interlock** |
| Battery-side Power 1109 = 0 | NEOVĚŘENO, zda potlačí i nativní AC coupling za všech stavů | Po nastavení profilu jeden setpoint; potřebná obnova watchdogu. Timeout může vrátit surplus charging | Kandidát po T7; dosud nenahrazuje ověřené nuly |
| Jen nativní přepínače / AC coupling off | Off samo neprokazuje oba směry | Více konfiguračních změn a možnost částečného profilu | Použít jen celý ověřený profil PV_AUTO/SAFE |
| Nativní AC coupling + Remote | Dobré oddělení PV_AUTO a obchodu, ale priorita souběhu NEOVĚŘENO | Remote zapnout jen pro obchod; návrat nepovažovat automaticky za HOLD | Preferovaná koncepce, nikoli současně aktivní dva vlastní regulátory |

**KÓD:** `number.deye_battery_max_charging_current` / `...discharging_current` mapují 108 / 109; definice povoluje 0–350 A.
**NEOVĚŘENO:** garantované EEPROM cykly, skutečná persistence, latence a priorita BMS/remote nebyly zjištěny.
Konfigurační proudy měnit jen při změně režimu a rozdílné hodnotě; žádná proudová regulační smyčka.
Obnovovat výslovně schválené limity, ne slepě poslední neověřenou hodnotu.

### Trvalý interlock během operace SolaXu

**Návrh založený na UŽIVATEL / DEDUKCE:**

1. Ukončit předchozí nucený Deye obchod; zadat 1109 = 0, pokud byl aktivní.
2. Nastavit oba proudové limity 0 a potvrdit jejich readback i skutečný malý bateriový proud/výkon. Tolerance a doba stabilizace z T7, nikoli přesně 0 W.
3. Vypnout Deye Remote Mode až v potvrzeném nulovém profilu, ověřit, že nativní profil blokaci respektuje. AC coupling nemusí být periodicky přepínán, jestliže limity blokují obě cesty.
4. Až potom povolit SolaX VPP. Proudové nuly držet **po celou dobu**, včetně expiry, opakování příkazu a potvrzování konce VPP.
5. Nejdříve ukončit a fyzicky potvrdit SolaX VPP; teprve pak obnovit potřebné proudy / povolit PV_AUTO.
6. Při výpadku HA se HOLD sám nesmí uvolnit. Restart Deye musí splnit T7; pokud nuly neudrží nebo se obnoví jiný profil, tento postup zatím není validovaný pro takový výpadek.

Tím je `SOLAX_SELL → zákaz nabíjení Deye` i `SOLAX_AC_CHARGE → zákaz vybíjení Deye` pokryt jedním obousměrným HOLD.
**NEOVĚŘENO:** uživatelovo potvrzení nul nepokrývá automaticky restart nebo předchozí aktivní remote — to jsou samostatné testy.
Remote-only alternativa musí prokázat `T_Deye_hold > T_SolaX_remaining + reakční rezerva` nebo trvale blokující fallback.
Samotná delší lease nestačí při restartu Deye; prokázat i jeho restartový profil.

Pro Deye obchod ponechat watchdog aktivní, nikdy nevolat `button.deye_disable_remote_watchdog`.
Obnovovat jen platný neexpirovaný požadavek. Konkrétní heartbeat interval se schválí podle testu; 30s polling není důkaz 1s potvrzení.
SAFE = ověřený stav, nikoli úspěšně odeslaná služba.

## 6. SolaX VPP — jedna preferovaná cesta

**DOK:** [SolaX VPP — mode 8 a registry](https://kb.solaxpower.com/fr/solution/detail/828080819a0a88fa019a0a941afc01e7)
popisuje mode 8 jako nezávislý PV a bateriový cíl s ukončením přes dobu trvání.
Bateriový target 0xA4/A5: kladně vybíjení, záporně nabíjení; 0xA2/A3 je PV limit, 0xA6 doba, 0xA7 timeout.
**DOK:** [SolaX VPP — mode 1](https://kb.solaxpower.com/it/solution/detail/40289fdf9279482a01927a1743c40230)
naopak řídí AC port, kladně odběr / záporně dodávku. V mode 1 se nejprve vyhodnocuje duration a následně timeout; nejde o univerzální timeout od poslední komunikace.
Mode 3/9 se SOC cílem není náhradou krátké časové lease. Mode 4 má odlišné znaménko od mode 1.

**Preferovat direct mode 8** s konečnou dobou; žádná emulace Self Use nebo Grid Control v HA/integraci.
Mimo obchod skutečný nativní `Self Use Mode`.
Důvod: přesný bateriový požadavek bez neustálého přepočtu PV→AC a bez další autorepeat vrstvy.
Nativní exportní ochrana má dál chránit zónu; její priorita při mode 8 je **NEOVĚŘENO / T3**.
PV limit předem schválit podle instalace; nepoužít nulu omylem ani „max“ bez ohledu na exportní ochranu.

**KÓD:** instalovaný SolaX Modbus 2026.10.2 obsahuje `build_direct_vpp_command` a `async_write_direct_vpp`.
Mode 8 pošle jeden úplný FC16 blok **0xA0–0xA7**; mode 0 blok 0x7C–0x88 pro ukončení.
Parametry neaktivního režimu se připravují lokálně, aktivní parametr vyvolá přečtení HW mode (input 0x100) a celý blokový zápis.
Proto při startu připravit parametry při vypnutém VPP a **aktivovat mode jako poslední**.
Změna několika parametrů za běhu může znamenat několik plných bloků; pro běžné obnovení stejného cíle postačí jedno explicitní znovuodeslání režimu. Jeho skutečné obnovení doby ověřit T8.

| Úkon | Konkrétní instalované entity / minimální fyzická komunikace |
|---|---|
| Připravit | `select.solax_power_control_mode_target_set_type_direct = Set`; `number.solax_remotecontrol_pv_power_limit_direct`; `number.solax_remotecontrol_push_mode_power_8_9_direct`; `number.solax_remotecontrol_duration_8_direct`; `number.solax_remotecontrol_timeout_8_9_direct`. Při mode 0 lokální příprava, nikoli pět zápisů do střídače |
| Spustit | `select.solax_modbus_power_control_direct = Individual Setting - Duration Mode` → jeden blok |
| Nabít / vybít | Push target záporný / kladný; jde o BAT výkon, ne PCC či AC port |
| Obnovit | Jeden úplný příkaz jen při platné lease; neaktivovat druhou autorepeat cestu |
| Ukončit | `select.solax_modbus_power_control_direct = Disabled` → jeden blok; potvrdit HW mode a fyzický výkon |
| Potvrdit | `sensor.solax_modbus_power_control`, bateriový výkon/SOC, AC výkon a PCC; optimistický select nestačí |

**KÓD:** všechny uvedené mode 8 direct entity jsou registrované, `disabled_by=null`.
Duration i timeout direct mají krok **60 s**. Návrh HW duration 60 s je přípustný kandidát; 20s direct lease přes tyto entity neslibovat.
Timeout = 0 nelze bez T8 vydávat za okamžitý či bezpečný fallback. Změřit skutečný konec nuceného výkonu i návrat do Self Use.
Před vstupem musí být Self Use potvrzen; firmware se může vracet do předchozího režimu.
Při HA startu žádný replay uložených direct parametrů, platný nový požadavek až po readbacku.

**DOK integrace:** [mode 1 dokumentace](https://github.com/wills106/homeassistant-solax-modbus/blob/main/docs/solax-mode1-modbus-power-control.md)
označuje VPP příkazy za ne-EEPROM, konfigurační export limit za EEPROM. Je to tvrzení autora integrace, ne změřená endurance konkrétního firmware.
**NEOVĚŘENO:** mode 8 chování při restartu střídače/HA, komunikaci a limitu BMS.
Pokud T8 neprojde, obchodní SolaX režimy neaktivovat; nezapínat automaticky alternativní řídicí smyčku.
Původní `remotecontrol_trigger` + autorepeat cestu během migrace ukončit; direct Disabled samo neprokazuje vypnutí staré opakovací smyčky.

## 7. Režimy, příkazy a přechody

Níže jsou **návrhové minimální změny**, nikoli pokyny k provedení nyní.
Nastavení měnit pouze při rozdílu, readbacky a podmínky nepřeskakovat.
Nula BAT nerovná se nula GRID (ztráty/vlastní spotřeba).

| Režim | SolaX | Deye a minimální potřebné změny | Podmínka vstupu / ukončení |
|---|---|---|---|
| SELF_USE | VPP Disabled, Self Use | HOLD: oba max proudy 0, remote Disabled | Potvrdit obě bateriové blokace |
| PV_AUTO | Nativní Self Use | Native surplus profile: Remote Disabled, AC coupling Grid, běžné Grid Charge off, nulový discharge limit, charge limit schválený | Jen po ukončení SolaX VPP; TOU/work mode podle ověřeného celého profilu, nikoli odhad |
| DEYE_SELL | Self Use | Připravit 1104=Battery, 1105=Power, nulový cíl, aktivní 1101, potvrzený 1100 mode; charge limit 0, discharge schválený, potom 1109 target | T2/T3 společného limitu, T7/T9 remote a SOC; při konci nejprve výkon 0/HOLD |
| SOLAX_SELL | Direct mode 8, BAT target +P | Držet obousměrný proudový HOLD, remote vypnutý | Potvrdit HOLD **před** VPP; uvolnit až po skutečném konci |
| SOLAX_AC_CHARGE | Direct mode 8, BAT target −P | Stejný HOLD | Potvrdit skutečný AC import a BAT nabíjení; PV může část BAT cíle pokrýt |
| DEYE_AC_CHARGE | Self Use | Charge limit schválený, discharge 0; remote profil jako výše, 1109 opačného směru | Ověřený importní/fázový limit, SOC/BMS; žádné vybíjení Deye na pokrytí SolaXu |
| SAFE | Zastavit VPP, potvrdit Self Use | Proudový HOLD, ukončit remote, zachovat nativní ochrany | Při chybě potvrzení zůstat fault; SAFE nelze jen prohlásit |

**NEOVĚŘENO:** pro DEYE_SELL je výchozí testovací kandidát Remote mode 3 z instalovaného skriptu; rozdíly mode 1/2/3 výrobce dostatečně nepopsal.
1104/1105/mode/watchdog se nenastavují při každém ticku, pokud profil platí.
První vstup remote vyžaduje více zápisů než následná změna setpointu; přesný počet heartbeat zápisů závisí na T9.
**UŽIVATEL / NEOVĚŘENO:** PV surplus schopnost je potvrzena, ale přesný kompletní optimální profil a restartové chování nikoli.

**DEDUKCE:** SOLAX_AC_CHARGE zde znamená povolit řízené nabíjení z AC, nikoli zaručit původ každého wattu v baterii ze sítě.
Optimalizátor musí vedle BAT cíle sledovat AC import, PV a výkon domu.
Současný nucený obchod obou střídačů se v první verzi nepovoluje.

Přechody: zastavit původní obchod → potvrdit HOLD/klid → připravit nový profil → aktivovat → potvrdit výkon.
Selhání kroku neumožní pokračovat do nového režimu. Při ukončování SolaXu se nuly Deye neobnovují v timeout větvi.
Stale PCC/SOC, chyba BMS či writer conflict ukončí nucený požadavek; nemění cíle v rychlé exportní regulační smyčce.

## 8. Minimální HA architektura

Čtyři skripty (nová jména odliší existující V3 testy):

1. `script.energy_v3_submit` — jediný vstup optimalizátoru, validuje celý režim+výkon+expiry a přijme jeden požadavek.
2. `script.energy_v3_reconcile` — jediný vlastník sériových přechodů; krátké omezené wait/readback kroky, kontrola fault/expiry před každým dalším zápisem. Nepřerušovat interlock pomocí `mode: restart`.
3. `script.energy_v3_apply_solax` — soukromá direct mode 8 cesta a ukončení; žádný další plánovač.
4. `script.energy_v3_apply_deye` — soukromý HOLD / native surplus / remote profil; jediná mapa jednotek a polarity.

Dvě automatizace:

- `automation.energy_v3_supervisor`: nový požadavek, HA start, expiry, health a periodické obnovení pouze platného krátkého HW příkazu; volá reconcile.
- `automation.energy_v3_safety`: fault/neočekávané toky/PCC překročení → zneplatnit požadavek a přikázat SAFE přes stejný sériový postup, bez souběžných zápisů adapterům. Pokud přechod běží, latch kontrolovaný před dalším zápisem, omezené čekání a alarm při neúspěchu.

Žádná anti-windup, PID nebo výpočet průběžných exportních korekcí. Exportní alarm je detekce závady, nikoli certifikace autonomního limitu.
Minimum stavů: enabled (start off), topology_verified (false), fault/reason, jeden accepted request a actual/applied mode.
Expiry/command ID patří do požadavku, netvořit oddělené pomocníky pro každé pole. Časy potvrzení a výkon lze mít jako diagnostické atributy.
Dlouhý JSON neukládat do 255znakového input_text/state; použít atributy jednoho stavového objektu. Start HA vždy zneplatní požadavek.

Příklad kontraktu (BAT cíl, pouze dokumentace):

```yaml
mode: SOLAX_SELL
power_w: 2000
issued_at: "2026-11-04T14:00:00+01:00"
valid_until: "2026-11-04T14:02:00+01:00"
command_id: optimizer-a-123
```

power_w je nezáporná velikost BAT cíle, směr určuje mode; v SELF_USE/PV_AUTO/SAFE = 0.
Max TTL návrhově 120 s, nový příkaz nesmí obnovit starou/expirovanou lease.
Odmítnout NaN/boolean/mimo rozsah, neplatné časy a replay. Stejné ID+payload neprodlužuje platnost.
HW duration nikdy nesmí způsobit obchod delší než přijatá lease; s 60s krokem obnovovat pouze tehdy, pokud zbývá celý prokázaný bezpečný interval.
Žádný optimizer (HAEO, Predbat, HAESS, vlastní) nesmí zapisovat fyzické entity.
Applied znamená potvrzený režim; skutečný BAT a AC výkon publikovat odděleně.
SOC/BMS limity omezují příkaz lokálně i v HA; 1110 v Power strategii není náhrada rezervy.

## 9. Aktuální writeři — audit 2026-11-04

**KÓD:** `configuration.yaml` zahrnuje `automations.yaml`, `scripts.yaml` a `packages/`.
Registr `disabled_by=null` není on/off stav automatizace, úspěšná konfigurace není důkaz provedeného příkazu.
[Inventář z července](energy-v2-writer-inventory.md) zůstává historickým podkladem; jeho on/off nepřenášet na dnešek.

| Zdroj právě přečtené konfigurace | Doložené možnosti zápisu / stav důkazu | Při budoucí migraci |
|---|---|---|
| ENERGY TRADING plánovač, vykonavatel, watchdog a `script.energy_trading_*` | Režimy obou střídačů, TOU, AC coupling, grid charge, limity; live on/off neznámé | Vyřadit všechny spouštěcí cesty a ruční dashboard tlačítka před jediným V3 writerem |
| `script.nastaveni_fve_solax_stop_deye_export`, `...restore_deye_normal` | Přímé fyzické zápisy bez vlastního masteru | Archivovat jako legacy, nevolat z nového SAFE |
| Kalendářní nákup/prodej, cenové prodeje V1/V2, SOC stop, `automation.fve_solax_zpet_do_self_use_po_vybiti_deye` | SolaX režim/manual/current; soubor obsahuje další stop automatizace nad rámec starého inventáře | Zkontrolovat celou skupinu i opožděné běžící akce |
| `automation.gridcontrol_charge`, „Chytré řízení nabíjení baterie dle ceny“, „Deye: Řízení baterie dle ceny“ | VPP/autorepeat, charge current, Deye režimy a export; live stav neznámý | Vypnout/archivovat při migraci, nikoli nyní |
| „Zakaze pretoky pri cene nizsi nez 0,40Kč“ / „Povoli pretok kdyz je cena vyssi nez 0,4Kč“ | `automations.yaml:656–692` mění `number.solax_export_control_user_limit` | Vyřešit konflikt s pevným limitem 9800; cenové omezení vyjádřit jedním vlastníkem, bez EEPROM smyčky |
| **`script.energy_v3_stop/buy/sell/trade_test` v scripts.yaml** | **Fyzické writeři**, registrované. Trade test nastavuje Battery-side/Power/Remote mode 3/1109/watchdog 30 s; SolaX přes původní Battery Control + trigger | Oddělit od preview a archivovat při migraci; nekopírovat watchdog off ani 6s potvrzení při 30s pollu |
| `script.energy_v3_stop` | `scripts.yaml:701–708` obsahuje `button.deye_disable_remote_watchdog` | Nová V3 tuto akci nepřebírá |
| `packages/energy_v3.yaml` | REVIZE 2 preview: request event a observed mode, samo žádné fyzické writes. **To neplatí pro celý prostor názvů energy_v3** | Archivovat/odlišit od nových jmen; zónový SolaX zdroj po přesunu přestat nazývat PCC |
| AppDaemon `energy_v3_shadow.yaml` + `energy_v3/shadow_app.py` | Soubor má aktivní .yaml příponu i přes komentář o .disabled; deklaruje shadow, kód publikuje physical_control=false. Načtení za běhu nepotvrzeno | Zachovat diagnostiku jen s opravenou topologií; ověřit aktivní importy a zbytek nasazených apps |
| ENERGY V2, staré V3 Python/HAEO, EMHASS/Predbat adaptéry | Repo/deployment historie není důkaz aktuálního writer stavu; apps.yaml zde obsahuje HelloWorld, existuje samostatný shadow YAML | Optimalizátory mohou plánovat, fyzické zápisy pouze přes nové submit |
| Node-RED `flows.json` | Tab **Energy Manager disabled=false**. Execute Action vytvoří `msg.service` a `switch.solax_charge_from_grid`, ale je to function s `wires:[[]]`, bez HA call. **V tomto uloženém flow není doložena cesta vykonávající tento zápis.** Energy Test také povolen, jen čtení/debug; Flow 1/miner/entity-finder disabled=true | Zabránit pozdějšímu připojení writeru; uložený JSON neprokazuje stav runtime nebo jiné externí flow |
| Přímé Modbus skripty na začátku scripts.yaml | Cíl `wattrouter_modbus`, nikoli podle názvu automaticky Deye/SolaX | Ponechat po ověření adresáta; regulace spotřebičů může ovlivnit bilanci, není sama inverter writer |

**NEOVĚŘENO:** kompletní aktuální on/off, běžící akce/traces, cloud/VPP aplikace a všechny runtime načtené AppDaemon/Node-RED cesty.
Nelze poctivě prohlásit, že dnes zapisuje jen jeden systém.
Před migrací načíst stavy/služby a projít všechny akce `select/number/switch/button/modbus/solarman`, volající skripty i dynamické entity.
Nyní se nic nevypíná ani nearchivuje.
Nový „SAFE“ nesmí volat starý restore, který změní proudy nebo zruší watchdog mimo nový postup.

## 10. Evidence, dostupnost a přesné chybějící údaje

Read-only SSH proběhlo existujícím schváleným přístupem podle [AGENTS.md](../AGENTS.md).
HA Core 2026.10.4; integrace SolaX 2026.10.2; Solarman manifest 0.0.0 (není identifikace upstream revize).
Čteny byly cílené definice integrací, filtrovaný registr entit, konfigurace skriptů/automatizací/package a Node-RED bez credentials.
Žádné HA služby ani Modbus read/write služby nebyly volány; čtení souborů neoslovovalo registry střídačů.

**Živé stavy nezískány:** GET přes existující prostředí SSH add-onu na Supervisor Core API i přímé Core API vracel **401**, potvrzeno kolem 2026-11-04 14:44 CET.
HA MCP `home_assistant_chalupa` uvedený v AGENTS.md není mezi dostupnými nástroji.
Nebyly hledány jiné tokeny, čteny privátní klíče ani obcházena autentizace.
`core.restore_state`, registr entit a preview komentáře nejsou živá telemetrie.

Chybí konkrétně: šest PCC hodnot se stejným časem a nezávislým směrem elektroměru; skutečná poloha CT; MAIN/HMI/DSP/ARM; live options a nastavené limity/remote režim; fyzické BAT proudy/výkony; polling timestamps; aktivní on/off a traces writerů.
Pro doplnění stačí autorizovaný read-only HA přístup nebo export těchto vybraných stavů; není potřeba posílat tajné údaje do dokumentace.

Otisky právě přečtených produkčních souborů (SHA-256, snapshot kolem 14:46 CET):

| Soubor | SHA-256 |
|---|---|
| `custom_components/solax_modbus/plugin_solax.py` | `0e0ced568744798c9c9c3d8f9ca118b8e38a0c577fc834d71ec9b3cc2d52e914` |
| `custom_components/solarman/inverter_definitions/deye_p3.yaml` | `2db48b0b8fdc00df832fd9b4cf936254cd06d5bf6ab1d6117162c6d4e9f113e1` |
| `scripts.yaml` | `983597b9d53a173bf7641b6563a7db7218ca2b256906c8eb556342364b50804a` |
| `automations.yaml` | `78383a3dc3bc6af1e95008cb865e28b103e8db8daac7cff4d8272aca3b25dacf` |
| `packages/energy_v3.yaml` | `8bc95ca8eba1d3c591afff9936b3dc9d707fc8a0a27eb358f1f2482ce37e4ff4` |
| `/app_configs/a0d7b954_nodered/flows.json` | `ecfb8e8b4e6baf247a8668d7316598863e12cb62a60f49e847f7cf19da501667` |

Veřejný upstream není totožný s instalací. Například [upstream SolaX commit 3dd9be3](https://github.com/wills106/homeassistant-solax-modbus/commit/3dd9be398f91f419db8ba1686d16c048105a0668) opravuje mapování import/export energie; sám nedokazuje znaménko tohoto měřeného výkonu.
Online SolaX KB některé jazykové varianty vracely 500 a anglická varianta obsahuje tabulky jako obrázky; závěry mode 8 jsou omezené na dostupný indexovaný text výrobce a instalovaný builder.
Není tím potvrzen firmware test.

## 11. Konkrétní fyzické testy před aktivací

**V této relaci žádný test níže nebyl proveden.** Změny režimů, zátěže, CT, výpadky a restarty vyžadují samostatně autorizovanou zkoušku.
U každého uložit firmware, počáteční nativní profil, surové veličiny, časy a výsledek; samotný HA select nestačí.
Nezačínat úmyslným překročením přípojky: nejprve nižší bezpečný testovací strop a malé výkony, nominální 9800 W až v kontrolovaných podmínkách.

| ID | Nejasnost a scénář | Akceptační výsledek |
|---|---|---|
| T1 | Měřicí hranice, znaménka, fáze; nezávislý import/export před a po přesunu, přirozené změny Deye | Deye external total odpovídá PCC a součtu fází; SolaX po přesunu pouze zóně; schválené k a čerstvost |
| T2 | Max Sell Power v nativním prodeji s externím SolaX 0/4000/9800 W; kontrola registrů 143 vs 231 | Deye dodávka se sama zmenší na zbývající prostor, příklad 4000→max 5800 AC; zaznamenat, který nativní profil to opravdu zajišťuje |
| T3 | T2 v používaném Remote mode + 1109; odděleně SolaX mode 8 s pevným export limitem | Remote nepřekročí PCC strop, SolaX VPP neobchází svůj zónový limit; neprovádět zatím souběžný nucený obchod obou |
| T4 | Plný/charge-blocked/vypnutý Deye, vysoká SolaX PV; potom malé externí překročení bezpečného testovacího stropu | SolaX drží vlastní zónu bez absorpce Deye; při externím překročení určit mez autority Deye, nikoli předpokládat curtailment SolaXu |
| T5 | Ztráta/obnova CT komunikace, restart a změna work/remote mode obou zařízení | Zdokumentované chybové stavy, žádné obnovení nuceného obchodu, zachovaná potřebná lokální ochrana; pokud ne, kombinaci nepovolit |
| T6 | Rychlá změna PV/domu při prodeji Deye; záznam PCC rychlejší než přechod a HA polling | Změřit dead time, ustálení, nejvyšší překmit a přebytečné Wh, 15min energii i fáze; žádná domnělá „odezva 1 s“ |
| T7 | Oba proudy 0 při surplus i požadavku vybíjení, po remote→native, během SolaX SELL i AC_CHARGE, HA/network timeout a restart Deye | Nuly skutečně drží oba směry po celou operaci a přechody; nevrátí se nežádoucí default. Zvlášť porovnat 1109=0 bez proudových nul |
| T8 | Direct SolaX mode 8: start, aktualizace/reselect, Disabled, konec duration+timeout, HA pád, integrace/střídač restart | BAT znaménko/cíl potvrzeny; známý maximální konec nuceného výkonu a návrat do Self Use; Deye HOLD trvá až za tento konec |
| T9 | Deye mode 3 Power: polarita, SOC/BMS, 1101 obnova různými zápisy, odpojení komunikace/HA restart | Známý heartbeat a fallback. Žádná důvěra v samotný Enabled watchdog status nebo 1110 |
| T10 | Writer inventory + invalid/expired/replay request, částečně selhaný přechod | Jeden fyzický vlastník, žádný replay po restartu, nevznikne make před break; rollback neuvolní Deye za běžícího SolaXu |
| T11 | Smluvní 15min vyhodnocení a mezery v datech | Potvrzené okno/fázové sčítání/netting, měřená energie; neúplné okno jasně označené |

Latence proudových nul, EEPROM endurance, výrobní priorita nativních limitů a restartová persistence nejsou v tomto auditu změřené.
Restartový test dokáže persistenci chování, nikoli technologii paměti ani počet dovolených zápisů.
Doba čekání v HA musí odpovídat fyzické odezvě + poll jitteru; dnešní testovací 6s wait není při 30s readbacku doklad potvrzení.

## 12. Migrace a zjednodušení proti původnímu návrhu

1. Tento PR pouze doplňuje technickou dokumentaci; žádné nasazení ani změna produkce.
2. Doplnit read-only live snapshot a uzavřít T1. Výslovně vybrat konkrétní profily podle T2/T3/T7/T8/T9; bez nich obchod zůstane neaktivní.
3. Připravit další implementační PR: jediný HA package, čtyři skripty, dvě automatizace, vypnutý default. Testovat podstatné přechody a selhání, nekopírovat starý executor.
4. Před aktivací inventarizovat runtime writery a běžící akce. Archivovat staré fyzické ENERGY TRADING/V3 testy, kalendářní a cenové řízení; ukončit staré VPP autorepeat. Nic z toho se nyní neprovádí.
5. Fyzické přepojení podle samostatně ověřeného projektu; potom přepnout jedinou PCC autoritu na ověřené external CT. SolaX alias už nesmí chránit celý objekt.
6. Postupně SELF_USE → PV_AUTO → jednotlivé obchodní režimy; SolaX obchod až po prokázání celodobého HOLD. Jeden optimizer, žádný souběžný přímý writer.
7. Rollback nejprve ukončí nový obchod a potvrdí HW stav, pak lze obnovit jeden zvolený systém. Starý systém s předpokladem centrálního SolaX CT není po přesunu automaticky použitelný.

**Odstraněno z požadavků:** HA exportní korekční smyčka, anti-windup a univerzální allocator; automatická potřeba další site ochrany bez ohledu na výsledek nativních testů; předpoklad, že každý energy_v3 soubor je preview.
**Zachováno:** nativní limity, krátké hardwarové příkazy, jediný writer, skutečné readbacky, fyzické směrové interlocky a požadavek 9800 W / 10000 W za 15 min.
Z historie využít scénáře a entity mapy, nikoli kopírovat runtime `apps/energy_v2/executor*`, `allocator.py`, `charge_controller.py` ani `apps/energy_v3/controller.py`.

**Stav uzavření auditu:** dokumentace a instalované mapování umožňují zjednodušený návrh. Garance celkového PCC limitu v Remote Mode, polarita živého měření a restart/timeout akceptace zůstávají konkrétními otevřenými body; nelze je uzavřít pouze čtením integračního YAML.
