# Albert Heijn boodschappenlijst voor Home Assistant

Een Home Assistant custom integration die een to-do-lijst-entiteit
toevoegt die twee kanten op synchroniseert met je Albert Heijn
boodschappenlijst: items toevoegen/afvinken in Home Assistant komt in de
AH-app terecht, en andersom. Pure Python, geen externe service of
Docker-container nodig.

> [!NOTE]
> **Dit project is "vibe-coded"**: de code, documentatie en tests in deze
> repo zijn tot stand gekomen in een lopend gesprek met Claude (Anthropic),
> op basis van instructies en feedback van een menselijke gebruiker die
> zelf geen Home Assistant-integraties schrijft. Er is geen persoonlijke
> Python/Home Assistant-expertise aan te pas gekomen om de code
> regel-voor-regel te reviewen. De boel is getest tegen lokale mockservers
> (zie "Wat is getest" verderop) en werkt bij de oorspronkelijke gebruiker,
> maar behandel dit als hobbyproject-kwaliteit, niet als production-grade
> software. Bugs, edge cases en API-aannames die net iets anders liggen
> dan gedacht zijn te verwachten — issues en pull requests zijn welkom.

## Bron & dankbetuiging

Dit project bevat geen gekopieerde code van derden, maar had zonder het
volgende niet kunnen bestaan:

- **[appie-go](https://github.com/gwillem/appie-go)** door
  [@gwillem](https://github.com/gwillem) — een Go-library en CLI voor de
  Albert Heijn mobile API. De endpoints, headers, auth-flow en
  request/response-vormen die hier in Python zijn nagebouwd, zijn afgeleid
  uit het lezen van appie-go's broncode en de meegeleverde
  reverse-engineering-documentatie (`doc/albertheijn_api.md`). appie-go is
  zelf gelicenseerd onder AGPL-3.0; deze repo gebruikt dezelfde licentie
  (zie [LICENSE](LICENSE)) als blijk van respect voor die keuze, ook al is
  dit een onafhankelijke Python-implementatie en geen letterlijke port.

Dit is een niet-officiële, reverse-engineered integratie. Niet
gelieerd aan of onderschreven door Albert Heijn / Ahold Delhaize.
Gebruik op eigen risico.

```
custom_components/appie/
  api.py          Async AH-API-client
  config_flow.py  Config flow: plak de code uit de AH-inlogpagina hier
  coordinator.py  Vraaggestuurd ophalen + optioneel achtergrondinterval
  todo.py         To-do-lijst-entiteit "Boodschappenlijst"
  __init__.py     Setup + services (add_item, checkout, clear_list)
```

## Hoe dit werkt

`appie-go` is Go; om deze integratie in pure Python te bouwen (zodat er
geen los proces/container nodig is) is `api.py` een eigen implementatie,
geïnformeerd door het lezen van appie-go's broncode en een meegeleverde
documentatiefile met een handmatig samengestelde beschrijving van de AH
mobile API.

Er zitten twee losse, niet-verwante functies in de AH-app die allebei
"lijst" heten:

1. **De klassieke, enkelvoudige boodschappenlijst** — wat de meeste mensen
   bedoelen met "mijn boodschappenlijst". Deze zit in de `shoppinglist/v2`
   endpoints en heeft geen naam of ID nodig: er is er maar één per account.
2. **"Favoriete lijsten" (v3)** — een aparte, nieuwere functie waarmee je
   *meerdere, benoemde* lijstjes kunt hebben (bijv. voor recepten of
   verlanglijstjes). appie-go's Go-wrapper gebruikt uitsluitend déze
   v3-functie.

Deze integratie gebruikt uitsluitend de klassieke v2-lijst (zie
[CHANGELOG.md](CHANGELOG.md) voor hoe dat onderscheid ontdekt werd, na een
paar rondes debuggen met 404's).

Elke functie in `api.py` heeft een `(verified)` of `(inferred)` label:

- **(verified)** — endpoint, headers en JSON-payload komen letterlijk uit
  appie-go's eigen documentatie/code.
- **(inferred)** — voor "item afvinken" en "item verwijderen" bestaat er
  geen apart, gedocumenteerd endpoint in de v2-lijst-API. Beide zijn
  daarom geïmplementeerd door het item opnieuw te versturen via dezelfde
  PATCH die ook voor toevoegen gebruikt wordt: met `strikeThrough` voor
  afvinken, en met `quantity: 0` voor verwijderen (naar analogie van hoe
  AH's Order-API `quantity: 0` gebruikt om een artikel uit je winkelwagen
  te halen). Dit is een onderbouwde aanname, geen bevestigd gedrag van de
  v2-lijst specifiek.

## De login-flow

AH's inlogpagina eindigt altijd met een redirect naar het vaste custom
scheme `appie://login-exit?code=...` (zo onderschept de officiële iOS-app
het) — een gewone browser kan daar niets mee. Deze integratie lost dat
**handmatig** op via de Chrome DevTools ("Inspect element"), in de
volgende stappen die je ook in de config flow ziet:

1. Open de getoonde inlog-URL in je browser en log **nog niet** in.
2. Open eerst de DevTools: **F12**, of rechtermuisknop op de pagina →
   **Inspecteren**. Klik bovenin het DevTools-paneel op het tabblad
   **Network**. Zorg dat "Preserve log" aan staat (een vinkje links
   bovenin dat tabblad) — dat voorkomt dat het overzicht leegloopt tijdens
   het inloggen.
3. Log nu in met je AH-account.
4. Na een geslaagde login probeert de pagina te verwijzen naar
   `appie://login-exit?code=...`. Je browser kan dat scheme niet openen —
   dat is verwacht en niet erg. In de Network-tab verschijnt daar een
   aparte regel voor, meestal met status **(failed)** of in het rood, met
   een naam die begint met `login-exit`.
5. Klik met de **rechtermuisknop** op die regel → **Copy** →
   **Copy URL** (in Firefox: **Copy Link Location** / **Kopieer
   linklocatie**). Dat plakt de complete
   `appie://login-exit?code=xxxxxxxx...` op je klembord.
6. Plak dat (de hele URL, of alleen het stuk na `code=`) in het veld in
   Home Assistant en klik op Verzenden.

Gebruik je geen Chrome? Firefox, Edge en Safari hebben allemaal een
vergelijkbaar Network-paneel in hun ontwikkelaarstools (meestal ook via
F12), alleen de exacte menu-namen kunnen iets afwijken.

Home Assistant wisselt die code vervolgens in voor een sessie
(`access_token`/`refresh_token`), die daarna zelf ververst wordt. Verlopen
de codes te snel (ze zijn meestal maar kort geldig — rond de stappen dan
gewoon opnieuw af en plak een nieuwe code), of vind je de `login-exit`-
regel niet terug in de Network-tab, controleer dan of "Preserve log"
inderdaad aanstond en of je de DevTools al open had staan vóórdat je op
de laatste inlogknop klikte.

## Installatie

Kopieer `custom_components/appie` naar de `custom_components`-map van je
Home Assistant, herstart Home Assistant, en voeg de integratie toe via
**Instellingen → Apparaten & Diensten → Integratie toevoegen → "Albert
Heijn boodschappenlijst"**. Volg de vier stappen in het scherm (zie "De
login-flow" hierboven) en plak de code.

Via [HACS](https://hacs.xyz/): voeg deze repo toe als custom repository
(categorie "Integration").

## Wat je krijgt

Entiteit `todo.boodschappenlijst`, vraaggestuurd gesynchroniseerd (zie
"Wanneer wordt de lijst opgehaald?"). Werkt met de gewone to-do-kaart, het
to-do-paneel, `todo.*`-acties en spraakassistenten.

### Typen in de lijst

| Je typt | Wordt op je AH-lijst |
|---|---|
| `melk` | vrije tekst "melk" |
| `2x melk`, `2 x melk`, `2× melk`, `melk x2` | vrije tekst "melk", aantal 2 |
| `441199` (kaal getal) | AH-product 441199 (webshopId) |
| `3x 441199` | AH-product 441199, aantal 3 |

- **Vrije tekst is de standaard.** Wil je dat getypte tekst aan een
  AH-product gekoppeld wordt, zet dan *Getypte items koppelen aan
  AH-producten* aan in de opties. Alleen een zekere match wordt gekoppeld
  (exacte titel, titel die ermee begint, of titel met alle woorden);
  anders blijft het vrije tekst.
- Items met een aantal boven 1 verschijnen als `2x melk`. Bewerk je die
  tekst in HA (bijv. naar `3x melk` of `havermelk`), dan wordt het aantal
  of de naam ook op je AH-lijst aangepast. Een gekoppeld product dat je
  hernoemt naar tekst wordt vrije tekst.
- Afvinken, uitvinken, verwijderen en "afgevinkte items wissen" werken
  zoals je verwacht. Gekoppelde producten tonen `AH-product <id>` als
  omschrijving.
- Twee gelijknamige vrije-tekst-items krijgen in HA een uid met `#2`;
  omdat AH vrije tekst op de tekst zelf herkent, raakt een wijziging aan
  het ene item mogelijk ook het andere.

### Acties (services)

Alle acties hebben de lijst als doel (`target: entity_id:
todo.boodschappenlijst`).

| Actie | Wat het doet | Antwoord |
|---|---|---|
| `appie.add_item` | `name` en/of `product_id`, `quantity` (1–99), `link_product` (zoeken en koppelen) | wat er is toegevoegd |
| `appie.search_products` | zoekt in het assortiment (`query`, `limit` ≤ 25) | product-ID, titel, merk, inhoud, prijs, bonus |
| `appie.checkout` | zet niet-afgevinkte, gekoppelde items in je actieve AH-bestelling; aantallen die al in je mand zitten worden **opgehoogd**, niet overschreven | aantal producten, overgeslagen vrije-tekst-items |
| `appie.clear_list` | haalt alles van de lijst | – |

Voorbeeld: product-ID opzoeken en toevoegen.

```yaml
- action: appie.search_products
  target:
    entity_id: todo.boodschappenlijst
  data:
    query: vaatwastabletten
  response_variable: zoek
- action: appie.add_item
  target:
    entity_id: todo.boodschappenlijst
  data:
    product_id: "{{ zoek['todo.boodschappenlijst'].products[0].product_id }}"
    quantity: 1
```

### Opties

*Instellingen → Apparaten & diensten → Albert Heijn → Configureren*:
achtergrond verversen (minuten, `0` = uit) en producten koppelen bij typen.

### Beveiliging & privacy

- Je AH-wachtwoord komt nooit in Home Assistant: je logt in op
  `login.ah.nl` in je eigen browser en plakt alleen de eenmalige code. De
  code wordt gecontroleerd op geldige tekens voordat hij verstuurd wordt.
- Tokens staan in de config entry (zoals bij elke HA-integratie) en worden
  **nooit gelogd**, ook niet op debug-niveau; hetzelfde geldt voor
  bestelgegevens (die bevatten je adres).
- **Diagnostics** (downloadbaar via de integratie) bevatten geen tokens,
  geen account-ID en geen lijstinhoud, alleen aantallen.
- Verlopen sessie → HA vraagt om opnieuw in te loggen (melding onder
  Instellingen). Bij een 401 wordt eerst één keer ververst; gelijktijdige
  verzoeken verversen nooit dubbel.
- Verwijder je de integratie, dan wordt de sessie bij AH uitgelogd
  (refresh-token ingetrokken).
- De integratie praat uitsluitend met `api.ah.nl` (en jij met
  `login.ah.nl`) en installeert geen extra Python-packages.

## Voorbeeldautomations

In [`examples/automations/`](examples/automations/) staan twee kant-en-
klare automations voor een concreet gebruiksscenario: automatisch
vaatwasblokjes bijhouden en bijbestellen. Kopieer de YAML in Home
Assistant (Instellingen → Automatiseringen → rechtsboven ⋮ → Automatisering
importeren/bewerken in YAML, of plak de inhoud in `automations.yaml`) en
pas de entity_id's en het product-ID aan naar jouw situatie.

1. **[`vaatwasblokjes_naar_boodschappenlijst.yaml`](examples/automations/vaatwasblokjes_naar_boodschappenlijst.yaml)**
   — telt een `input_number`-teller één af zodra je vaatwasser gaat
   draaien. Staat de teller op 10, dan wordt het product direct
   toegevoegd aan `todo.boodschappenlijst` via het echte AH webshopId
   (een kaal getal als item-tekst wordt door deze integratie herkend als
   product-ID, zie "Wat je krijgt" hierboven).

   *Zoek zelf het webshopId van jouw vaatwasblokjes op*: roep in
   Ontwikkelaarshulpmiddelen → Acties eenmalig `appie.search_products` aan
   (bijv. `query: vaatwastabletten`) en neem het `product_id` over — of
   zoek het product op ah.nl en haal het nummer uit de URL.

2. **[`vaatwasblokjes_voorraad_bijwerken.yaml`](examples/automations/vaatwasblokjes_voorraad_bijwerken.yaml)**
   — reageert op `todo.item_completed` (afvinken, zowel in HA als in de
   AH-app zelf; dat laatste pas bij de volgende refresh, zie "Wanneer wordt
   de lijst opgehaald?") en telt
   de voorraad-teller weer op met de pakgrootte, zodra het afgevinkte item
   matcht op `product:<jouw-webshopId>`.

   Let op: de `todo.item_completed`-trigger geeft in de praktijk geen
   bruikbare `item_ids` als sjabloonvariabele terug (ondanks wat Home
   Assistants eigen documentatie daarover suggereert — alleen
   `entity_id`/`from_state`/`to_state` zijn daadwerkelijk gedocumenteerd).
   Dit voorbeeld vraagt daarom de afgevinkte items zelf op via
   `todo.get_items` en filtert op `uid`, wat betrouwbaar werkt.

## Wat is getest

**Fork (v0.3+):** `tests/` bevat een testsuite met
[pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
die de integratie in een echte Home Assistant-testomgeving draait tegen
een gemockte AH-API: login/reauth, token-refresh (ook gelijktijdig),
vraaggestuurd verversen, typen met aantallen, hernoemen, afvinken,
verwijderen, zoeken, `checkout` en diagnostics. Draaien:
`pip install pytest-homeassistant-custom-component && pytest`.

**Oorspronkelijk:** zonder een echt AH-account kon dit niet end-to-end getest worden. Wel is
`api.py` losstaand gedraaid tegen een lokale mockserver die `api.ah.nl`
nabootst — inclusief een mockserver die de v2-lijst als in-memory state
bijhoudt, zodat toevoegen/afvinken/verwijderen/legen ook echt op elkaar
inwerken zoals bij een live account. Dat dekt: tokens ophalen/verversen
(inclusief het inwisselen van een geplakte code), de v2-lijst ophalen en
parsen, product toevoegen (zoeken én vrije tekst), afvinken/uitvinken met
behoud van aantal, verwijderen, zoeken, omzetten naar bestelling, lijst
legen, en foutafhandeling bij een HTTP 4xx. Het regex-patroon dat de code
uit een geplakte `appie://login-exit?code=...`-URL haalt is los getest met
meerdere varianten. De Python-bestanden zijn syntactisch gecontroleerd; de
volledige Home Assistant-omgeving (met een écht AH-account) kon niet
gedraaid worden tijdens ontwikkeling — dit is dus getest door de
oorspronkelijke gebruiker op een echte installatie, niet door de auteur
van deze code.

## Bekende risico's

- Dit gebruikt een niet-officiële, reverse-engineered API. AH kan endpoints
  of de loginflow op elk moment wijzigen.
- Afvinken werkt in de praktijk; verwijderen, aantal wijzigen en hernoemen
  zijn **(inferred)** (upsert met `quantity`/`quantity: 0`) — zie hierboven.
- De integratie doet zich voor als de Appie-iOS-app (zoals appie-go).
- De inlogcode van AH is vermoedelijk kortlevend; als het inwisselen
  mislukt met "cannot_connect", rond de loginstappen dan opnieuw af en
  plak een verse code.

Zie [CHANGELOG.md](CHANGELOG.md) voor de volledige geschiedenis van
gevonden bugs en fixes.

## Wanneer wordt de lijst opgehaald?

Niet meer elke 60 seconden, maar op het moment dat het nodig is:

- **Bij openen**: zodra een to-do-kaart of het to-do-paneel de lijst toont
  (web of app), wordt hij opgehaald — tenzij de laatste ophaalactie minder
  dan 30 seconden oud is.
- **Zolang hij open staat**: elke minuut, zodat afvinken in de AH-app
  (bijv. in de winkel) zichtbaar wordt. Sluit je de kaart, dan stopt dit.
- **Na een wijziging vanuit HA** (toevoegen, afvinken, verwijderen,
  services).
- **Achtergrond**: instelbaar via *Instellingen → Apparaten & diensten →
  Albert Heijn → Configureren* (minuten, standaard 60, `0` = uit). Dit is
  wat automations en spraakassistenten (`todo.get_items`) zien als niemand
  de lijst open heeft. Wil je dat een automation snel reageert op afvinken
  in de AH-app, zet dit dan lager (bijv. 5).

## Debug-logging aanzetten

Standaard logt Home Assistant van een custom integration alleen fouten die
expliciet als "error" gelogd worden — niet elke losse API-aanroep. Zet
debug-logging aan om de volledige request/response-uitwisseling met AH te
zien:

- **Via de UI**: Instellingen → Systeem → Logs → rechtsboven "Logboeken
  configureren" → zoek `custom_components.appie` → zet op "Debug".
- **Via `configuration.yaml`**:
  ```yaml
  logger:
    default: warning
    logs:
      custom_components.appie: debug
  ```
  Herstart daarna Home Assistant (of herlaad de logger-configuratie) en
  herlaad de integratie om nieuwe regels te forceren.

Elke regel ziet er dan zo uit:
```
DEBUG (MainThread) [custom_components.appie.api] AH API GET /mobile-services/shoppinglist/v2/items -> HTTP 200: {...}
```

Inlogcodes en tokens (alles onder `/mobile-auth/`) worden nooit gelogd,
ook niet op debug-niveau.

Een item zonder bruikbare naam/productID wordt daarnaast altijd — ook
zónder debug-logging — als **waarschuwing** gelogd met de ruwe AH-data.

## Licentie

[AGPL-3.0](LICENSE), zie "Bron & dankbetuiging" hierboven voor waarom.

## Bijdragen

Issues en pull requests zijn welkom — zeker gezien de "vibe-coded"
herkomst kan een menselijke review op punten die hierboven als
`(inferred)` gemarkeerd staan, geen kwaad.
