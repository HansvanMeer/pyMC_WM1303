# Handover — v2.5.8 Topology (Pad 1 + Pad 2)

> Status op 2026-06-14 20:34 CEST. Vorige chat liep tegen context-limiet. Niets gecommit/gepusht. VERSION blijft 2.5.8. pi01 ongemoeid. Testdevice = pi03 (192.168.101.80, pi:pluis).

## Doel
Neighbours/Topology-tab v2.5.8 met:
- **Pad 1** — concentrische hop-ringen rond YOU, count-badges, hide-relayed toggle, relay-table, labels alleen op direct-heard (overlap opgelost).
- **Pad 2** — mesh-graph mode: reconstrueer YOU → hop → … → bron uit `path` BLOB, locatie-heuristiek voor 1-byte-hash disambiguatie, ghost-nodes, click-info venster met alternatieve kandidaten (zelfde hash-byte) onder elkaar.

## Protocol-feiten (geverifieerd)
- `packet.path` = `bytearray`, max 64 bytes. Hop-hash = **`pubkey[0]`** (1e byte Ed25519 pubkey). Geen SHA256.
- `packet.path_len` byte: bits 0-5 = hop-count (0-63), bits 6-7 = bytes/hop (1-3). Praktijk = 1 byte/hop.
- 1-byte hash = 256 waarden, 859 buren → ~3.4 kandidaten/bucket → disambiguatie essentieel.
- Locatie-data beschikbaar: 98% van repeaters heeft lat/lon → mét-locaties strategie.

## ✅ Geverifieerd klaar (pi03)
- Migration 13 `add_path_blob_to_adverts`: kolommen `path` (BLOB) + `path_len_encoded` (INTEGER) in `adverts`. Write-path bewezen.
- `advert.py`: bug fix `path_len` via `get_path_hash_count()` (was `len(packet.path)`), + `path` + `path_len_encoded` in advert_record.
- `wm1303_api.py`: bytes→hex fix toegepast; veld hernoemd `path_hex`→`path` (frontend leest `n.path`); defensieve bytes→hex sanitizer vóór `neighbours.append`. Python-syntax OK (`ast.parse`).
- Frontend `wm1303.html`: Pad 1 + Pad 2 volledig gebouwd, JS-syntax OK (`node --check`), gedeployed (md5 live=overlay).
- Service `pymc-repeater.service` (mét streepje) = active/running.
- Backup live-html: `/tmp/wm1303_backup_20260614_201958.html` (op pi03).

## ⚠️ OPEN — moet nog gebeuren
1. **Auth-flow bevestigen**: SPA-login werkt via het form; de directe API-login-URL die geprobeerd werd gaf 'Unauthorized'. Vind het juiste login-endpoint/credential-pad. Credentials alleen runtime via env, NIET opslaan in project/git.
2. **Curl-verify** neighbours-endpoint: moet 200 geven + `path` als hex-string.
3. **Tweede 500/404 endpoint fixen** dat de SPA-init blokkeert. Kandidaten: `/stats`, en de pre-existing bug `adverts_by_contact_type() got unexpected kwarg 'offset'` (404). Dit blokkeert vermoedelijk de Topology-tab vóór render.
4. **Screenshots maken** (5): rings-default, rings-zoomed, force-hop, force-channel, mesh. Script staat klaar: `/tmp/shot/shot.js` (op pi03) met login-flow + 5 shots.
5. **Project-overlay sync**: `/a0/usr/projects/pyMC_WM1303` is NOG NIET bijgewerkt met de backend-fix — alleen pi03 (live + overlay) is bij. Kopieer de gewijzigde `sqlite_handler.py`, `advert.py`, `wm1303_api.py`, `wm1303.html` naar de project-overlay (GEEN commit/push).

## Eerste actie verse chat
Stap 1 (auth-flow) + Stap 2 (curl-verify neighbours 200 + hex path). Dan Stap 3 indien nog 500. Dan Stap 4 screenshots → visuele review. Daarna Stap 5 (project-overlay sync). Pas na review + expliciete go van gebruiker: commit/push.

## Randvoorwaarden
- **Niets committen/pushen, niets naar GitHub** zonder expliciete go.
- VERSION blijft 2.5.8. pi01 ongemoeid. Alleen pi03.
- RX/TX-prioriteit: backend-writes via async-thread (`asyncio.to_thread(self.storage.record_advert, ...)`) — niet wijzigen.
- Geen achtergrondprocessen laten draaien.
