# Catalog sanitisation — suspect songs to review

_Generated from sanitize_catalog.py. 13 explicit alt-versions + duration anomalies. These are NOT auto-changed — review and re-ingest the real originals as you see fit._


## A) Explicit alt-versions (remix / nightcore / sped-up / mashup — not the original recording)

| id | artist | title | markers | note |
|----|--------|-------|---------|------|
| 424 | Wizz Havinn;DJ Ess | New Wave - DJ Ess Remix | remix | replace with original |
| 617 | Godard;Ziahfyah | She Know What She Wanted - Jersey Club Remix Slowed | remix, slowed | replace with original |
| 658 | Ken Carson | Nightcore 2 | nightcore | replace with original |
| 666 | A$AP Rocky;Imogen Heap;Clams Casino | I Smoked Away My Brain (I'm God x Demons Mashup) (feat. Imogen Heap & Clams Casino) | mashup | replace with original |
| 795 | Contingency | molly - sped up | sped up | replace with original |
| 933 | Corey St.Rose;Flipp Dinero | MMM - Remix | remix | replace with original |
| 995 | Lil Uzi Vert;sped up nightcore;ARIZONATEARS | Watch This - ARIZONATEARS Pluggnb Remix | nightcore, remix, sped up | replace with original |
| 1134 | Russ Millions;YV;SwitchOTR;Buni;CH (GMD);Gazo;RoseReal | Reggae & Calypso (Russ Millions x Buni x YV x CH (GMD) x SwitchOTR x Gazo x RoseReal) - Remix | remix | replace with original |
| 1230 | Travis Scott;Future;Young Thug;M.I.A. | FRANCHISE (feat. Future, Young Thug & M.I.A.) - REMIX | remix | OFFICIAL remix — probably keep |
| 1254 | Global Dan;Rich The Kid;MadeinTYO | Off White - Remix | remix | replace with original |
| 1291 | Comethazine;Rich The Kid | Bands (Remix) (feat. Rich The Kid) | remix | replace with original |
| 1346 | Tay-K;21 Savage;Young Nudy | The Race - Remix | remix | replace with original |
| 1634 | ONEFOUR; Nemzzz; Segway | Spinnin (feat. Nemzzz) - Segway Remix | remix | replace with original |

## B) Duration anomalies (length ≠ canonical release)

| id | artist | title | catalog | canonical | Δ | verdict |
|----|--------|-------|---------|-----------|---|---------|
| 27 | Queen | Bohemian Rhapsody | 147s | 354s | 207s | LIKELY WRONG (snippet/too-long) |
| 44 | Basement Jaxx | Where's Your Head At | 285s | 180s | 105s | probably FINE (Deezer matched a remaster/remix) |
| 1299 | Travis Scott | goosebumps | 243s | 162s | 81s | probably FINE (Deezer matched a remaster/remix) |
| 1567 | Eminem | Houdini | 297s | 228s | 69s | LIKELY WRONG (snippet/too-long) |
| 56 | Lex Amarni | the kill | 80s | 121s | 41s | probably FINE (Deezer matched a remaster/remix) |
| 1322 | Leto | Double Bang 1 | 132s | 171s | 39s | probably FINE (Deezer matched a remaster/remix) |

## C) Manual note

- **id 1002 'Mobx – If Looks Could Kill'** — no canonical match on Deezer; odd attribution, worth a listen. (The Destroy Lonely 'if looks could kill', id 968, checked out as the correct recording — exact 3:14 duration match.)
