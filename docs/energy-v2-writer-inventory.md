# Energy V2 – inventář zapisujících cest do SolaX / DEYE

Datum auditu: 2026-07-26. Zdroj: Home Assistant MCP, čtení konfigurací automatizací, skriptů, služeb a stavů entit.

Nebyla provedena žádná změna konfigurace Home Assistantu. Nebyly vypnuty ani zapnuty žádné automatizace.

## Sledované fyzické aktuátory

### SolaX

- `select.solax_charger_use_mode`
- `select.solax_manual_mode_select`
- `number.solax_battery_discharge_max_current`
- `number.solax_battery_charge_max_current`
- `number.solax_remotecontrol_active_power`
- `number.solax_remotecontrol_autorepeat_duration`
- `select.solax_remotecontrol_power_control`
- `button.solax_remotecontrol_trigger`

### DEYE

- `select.deye_work_mode`
- `select.deye_time_of_use`
- `select.deye_ac_coupling`
- `switch.deye_export_surplus`
- `switch.deye_battery_grid_charging`
- `number.deye_grid_max_export_power`
- `number.deye_export_surplus_power`
- `number.deye_grid_max_import_power`
- `number.deye_battery_grid_charging_current`

## Přímé a nepřímé zapisující cesty

| Název | Entity ID | Stav | Trigger | Podmínky | Volané skripty | Měněné aktuátory | Nastavované hodnoty | Master ochrana | Může běžet mimo master | Riziko pro Energy V2 |
|---|---|---:|---|---|---|---|---|---|---|---|
| ENERGY TRADING – Původní řešení hlavní vypínač | `automation.energy_trading_puvodni_reseni_hlavni_vypinac` | `on` | změna `input_boolean.energy_trading_puvodni_reseni_povoleno` | žádné | žádné | automatizace starého systému | zapíná/vypíná vybrané automace | vlastní legacy master | ano, protože neřeší všechny staré automace | střední – přepíná jen část starého systému |
| ENERGY TRADING – Bezpečné povolení nového systému | `automation.energy_trading_bezpecne_povoleni_noveho_systemu` | `on` | `input_boolean.energy_trading_novy_system_povolen` → `on` | legacy off, timer idle, DEYE connected, DEYE Normal | `script.energy_trading_idle`, trigger plánovače | nepřímo DEYE/SolaX přes script | viz `script.energy_trading_idle` | `energy_trading_novy_system_povolen` | ne | nízké pro V2, pokud V2 kontroluje, že starý nový systém je off |
| ENERGY TRADING – Plánovač 15 minut | `automation.energy_trading_planovac_15_minut` | `on` | `/15 min`, ceny, SOC, hystereze | nový systém on, legacy off | žádné přímo; mění režim | `input_select.energy_trading_rezim` | `Idle`, `PV Charge`, `Grid Charge`, `Export`, `Export SolaX` | nový master | ne | vysoké, pokud by byl současně aktivní s V2 |
| ENERGY TRADING – Vykonavatel režimu | `automation.energy_trading_vykonavatel_rezimu` | `on` | změna `input_select.energy_trading_rezim` | nový systém on, legacy off | `script.energy_trading_idle`, `script.energy_trading_deye_export`, `script.energy_trading_deye_pv_charge`, `script.energy_trading_deye_grid_charge`, `script.energy_trading_solax_export` | nepřímo všechny hlavní aktuátory | dle vybraného skriptu | nový master | ne | vysoké, pokud by se spustil současně s V2 |
| ENERGY TRADING – Watchdog a anti-cycling | `automation.energy_trading_watchdog_a_anti_cycling` | `on` | `/15 s`, DEYE connection/state/battery state | nový systém on | `script.energy_trading_fault_safe` | nepřímo DEYE/SolaX přes fault safe | vypnutí exportu/grid charge, Self Use | nový master | ne | střední – může přepsat stav při současném testu V2 |
| Bezpečný klid | `script.energy_trading_idle` | script | volaný automacemi | žádné vlastní | žádné | `switch.deye_export_surplus`, `switch.deye_battery_grid_charging`, `select.deye_time_of_use`, `select.deye_ac_coupling`, `select.deye_work_mode`, `select.solax_charger_use_mode` | DEYE off/Disabled/Zero Export To CT, SolaX Self Use Mode | volající automace | ano, pokud ručně volán | vysoké – fyzicky nastavuje oba střídače |
| DEYE prodej | `script.energy_trading_deye_export` | script | volaný executor | nový on, legacy off, DEYE connected, Normal | `script.blokuje_nahrivani_bojleru_z_prebytku_pomoci_wattrouteru` | SolaX use/manual, DEYE grid charge/export/TOU/work mode/export limity | SolaX Manual/Stop; DEYE Export First, TOU Enabled, export on | podmínky ve scriptu | ruční spuštění projde jen při masterech | vysoké |
| DEYE nabíjení z FV | `script.energy_trading_deye_pv_charge` | script | volaný executor | nový on, legacy off, DEYE Normal | žádné | SolaX use/manual, DEYE export/grid charge/current/work mode/ac coupling | SolaX Self Use; DEYE Zero Export To CT, AC Coupling Grid, grid charge off | podmínky ve scriptu | ruční spuštění projde jen při masterech | vysoké – AC coupling |
| DEYE nabíjení ze sítě | `script.energy_trading_deye_grid_charge` | script | volaný executor | nový on, legacy off, DEYE Normal | žádné | SolaX use/manual, DEYE work/ac coupling/import limit/current/grid charge | SolaX Manual/Stop; DEYE Grid Charging on | podmínky ve scriptu | ruční spuštění projde jen při masterech | kritické – Grid Charge nesmí být součást V2 obchodování |
| Nouzové zastavení | `script.energy_trading_fault_safe` | script | volaný plánovačem/watchdogem | žádné vlastní | žádné | DEYE export/grid charge/TOU/ac coupling/work mode, SolaX use mode | DEYE off/Disabled/Zero Export; SolaX Self Use | volající automace | ano, ručně | střední – bezpečné, ale fyzicky zapisuje |
| SolaX prodej po vybití DEYE | `script.energy_trading_solax_export` | script | volaný executor | nový on, legacy off, SolaX SOC nad limitem | `script.blokuje_nahrivani_bojleru_z_prebytku_pomoci_wattrouteru` | DEYE stop, SolaX discharge current/use/manual | DEYE stop; SolaX 30 A, Manual/Force Discharge | podmínky ve scriptu | ruční spuštění projde jen při masterech | vysoké |
| Řízení SolaXu podle kalendáře – Nákup | `automation.rizeni_solaxu_podle_kalendare_nakup` | `off` | start/end `calendar.nakup_elektriny` | žádné | žádné | `select.solax_charger_use_mode`, `select.solax_manual_mode_select` | start: Manual Mode + Force Charge; end: Self Use Mode + Stop Charge and Discharge | legacy master jen nepřímo přes vypínač | ano, pokud ručně zapnuta | vysoké |
| Řízení SolaXu podle kalendáře – Prodej | `automation.rizeni_solaxu_podle_kalendare_prodej` | `off` | start/end `calendar.prodej_elektriny` | start: SolaX SOC > 30 | žádné | `select.solax_charger_use_mode`, `number.solax_battery_discharge_max_current`, `select.solax_manual_mode_select` | start: Manual Mode, 30 A, Force Discharge; end: Self Use Mode, Stop Charge and Discharge | legacy master jen nepřímo přes vypínač | ano, pokud ručně zapnuta | vysoké |
| Prodej elektřiny podle nejlepší ceny V1 | `automation.prodej_elektriny_v_urcity_cas_podle_nejlepsi_ceny` | `off` | `input_datetime.next_discharge_run_time` | SolaX SOC > 60, forecast > 14, osoba/čas | žádné | `select.solax_manual_mode_select`, `select.solax_charger_use_mode` | Force Discharge, Manual Mode, později `Self Use` | žádný energy master | ano | vysoké – staré, neplatná volba `Self Use`, dlouhé čekání |
| Prodej elektřiny podle nejlepší ceny V2 | `automation.prodej_elektriny_v_urcity_cas_podle_nejlepsi_ceny_v2` | `off` | `input_datetime.next_discharge_run_time` | SolaX SOC > 60, forecast > 14, osoba/čas | žádné | `select.solax_manual_mode_select`, `select.solax_charger_use_mode` | Force Discharge, Manual Mode, později `Self Use` | žádný energy master | ano | vysoké – staré, neplatná volba `Self Use`, dlouhé čekání |
| Prodat podle kalendáře start | `automation.prodat_podle_kalendare` | `off` | start `calendar.prodej_elektriny` | SolaX SOC > 30 | žádné | `select.solax_charger_use_mode`, `select.solax_manual_mode_select` | Manual Mode, Force Discharge | žádný energy master | ano | vysoké – duplicitní |
| Prodat podle kalendáře stop | `automation.prodej_podle_kalendare_stop` | `off` | end `calendar.prodej_elektriny` | žádné | žádné | `select.solax_charger_use_mode`, `select.solax_manual_mode_select` | Self Use Mode, Stop Charge and Discharge | žádný energy master | ano | střední – duplicitní |
| Nákup podle kalendáře start | `automation.nakup_podle_kalendare_start` | `off` | start `calendar.nakup_elektriny` | žádné | žádné | `select.solax_charger_use_mode`, `select.solax_manual_mode_select` | Manual Mode, Force Charge | žádný energy master | ano | vysoké – grid charge SolaX |
| Nákup podle kalendáře stop | `automation.prodat_podle_kalendare_stop` / ID `1765121282733` | `off` | end `calendar.nakup_elektriny` | žádné | žádné | `select.solax_charger_use_mode`, `select.solax_manual_mode_select` | Self Use Mode, Stop Charge and Discharge | žádný energy master | ano | střední – duplicitní |
| Deye: Řízení baterie dle ceny | `automation.deye_rizeni_baterie_dle_ceny_nabijeni_vybijeni` | `off` | cena, DEYE SOC, `/5 min` | cena/SOC template | žádné | `select.deye_work_mode`, `switch.deye_battery_grid_charging`, `number.deye_battery_grid_charging_current`, `number.deye_export_surplus_power` | nabíjení: grid charging on, proud 100; export: Export First, výkon 8000 | žádný energy master | ano | kritické – umí Grid Charge a export mimo V2 |
| Spustit vybíjení DEYE v konkrétní čas | `automation.spust_vybijeno_deye_v_konkretni_cas` | `off` | 18:30:01 | žádné | `script.nastaveni_fve_solax_stop_deye_export` | nepřímo SolaX/DEYE | SolaX Stop, DEYE Export | žádný energy master | ano | vysoké |
| Vypne vybíjení DEYE a zapne Self Use | `automation.vypne_vybijeni_deye_a_zapne_self_use_mod_na_solaxu` | `off` | BMS Voltage < 49.05 | žádné | `script.nastaveni_fve_solax_restore_deye_normal` | nepřímo SolaX/DEYE | SolaX Self Use, DEYE Zero Export | žádný energy master | ano | střední |
| FVE: SolaX zpět do Self-Use po vybití Deye | `automation.fve_solax_zpet_do_self_use_po_vybiti_deye` | `on` | `sensor.deye_battery` 16 → 15 | SolaX je `Manual Mode` | žádné | `select.solax_charger_use_mode` | Self Use Mode | žádný energy master | ano | kritické – aktivní konflikt |
| Zapnout nabíjení DEYE v určitý čas | `automation.zpnout_nabijeni_deye_v_ucity_cas` | `off` | 08:00:01 | žádné | `script.nastaveni_fve_solax_restore_deye_normal` | nepřímo SolaX/DEYE | restore normal | žádný energy master | ano | střední |
| Gridcontrol charge | `automation.gridcontrol_charge` | `off` | spot cena < `input_number.charge_price_threshold` | SolaX Self Use/Feedin, SOC pod target, časové okno | žádné | `number.solax_remotecontrol_active_power`, `select.solax_remotecontrol_power_control`, `number.solax_remotecontrol_autorepeat_duration`, `button.solax_remotecontrol_trigger` | 2500 W, Enabled Grid Control, autorepeat duration, trigger | žádný energy master | ano | kritické – síťové řízení SolaXu |
| Nastavení FVE: SolaX Stop & Deye Export | `script.nastaveni_fve_solax_stop_deye_export` | script | ručně nebo staré automace | žádné | žádné | `select.solax_charger_use_mode`, `select.solax_manual_mode_select`, `select.deye_ac_coupling`, `switch.deye_export_surplus`, `select.deye_time_of_use`, `select.deye_work_mode` | SolaX Manual/Stop; DEYE Disabled/export on/TOU Enabled/Export First | žádný master | ano | vysoké |
| Nastavení FVE: SolaX Restore & Deye Normal | `script.nastaveni_fve_solax_restore_deye_normal` | script | ručně nebo staré automace | žádné | žádné | `select.solax_charger_use_mode`, `select.deye_ac_coupling`, `switch.deye_export_surplus`, `select.deye_time_of_use`, `select.deye_work_mode` | SolaX Self Use; DEYE Grid/export off/TOU Disabled/Zero Export To CT | žádný master | ano | střední |

## Node-RED a jiné cesty

Node-RED add-on je v Home Assistantu přítomný podle `update.node_red_update`, ale MCP v této relaci neposkytuje čtení Node-RED flow. Proto nelze potvrdit, zda Node-RED nevolá `select.select_option`, `switch.turn_on/off`, `number.set_value`, `modbus.write_register` nebo služby integrací SolaX/DEYE.

Služby dostupné v HA zahrnují mimo jiné `modbus.write_register`, `solarman.write_single_register`, `solarman.write_holding_register`, `solax_modbus.stop_all/stop_hub`, `hassio.addon_*`, `automation.trigger`, `script.*`. Nebyly volány.

## Konfliktní automatizace pro Energy V2 konfiguraci

Do `apps/energy_v2.yaml` byly zařazeny pouze existující entity zjištěné přes MCP:

- `automation.fve_solax_zpet_do_self_use_po_vybiti_deye`
- `automation.rizeni_solaxu_podle_kalendare_nakup`
- `automation.rizeni_solaxu_podle_kalendare_prodej`
- `automation.deye_rizeni_baterie_dle_ceny_nabijeni_vybijeni`
- `automation.gridcontrol_charge`
- `automation.spust_vybijeno_deye_v_konkretni_cas`
- `automation.vypne_vybijeni_deye_a_zapne_self_use_mod_na_solaxu`
- `automation.zpnout_nabijeni_deye_v_ucity_cas`

Aktivní konflikt při auditu:

- `automation.fve_solax_zpet_do_self_use_po_vybiti_deye = on`

## Shrnutí rizik

1. Aktivní automatizace `automation.fve_solax_zpet_do_self_use_po_vybiti_deye` může měnit SolaX mimo master přepínač.
2. Několik vypnutých starých automatizací umí přímo řídit SolaX/DEYE a při ručním zapnutí by konflikt vznikl okamžitě.
3. Staré DEYE řízení a `Gridcontrol charge` umí síťové nabíjení nebo grid control; Energy V2 s nimi nesmí běžet.
4. Node-RED flow nebylo dostupné přes MCP, takže není možné vyloučit další zápisové cesty.

