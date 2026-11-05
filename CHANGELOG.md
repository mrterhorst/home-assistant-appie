# Changelog

- **v0.3.0 (fork mrterhorst)**
  - **Vraaggestuurd ophalen** in plaats van elke 60 s pollen: bij openen
    van de lijst (max. 1× per 30 s), elke minuut zolang hij open staat, na
    eigen wijzigingen, plus een instelbaar achtergrondinterval (standaard
    60 min, `0` = uit) via de opties.
  - **Herlogin (reauth)**: een 401 leidt eerst tot één token-refresh en
    een nieuwe poging; weigert AH de refresh-token, dan vraagt HA om
    opnieuw in te loggen. Tokens worden ruim vóór het verlopen ververst.
    Token-updates herladen de integratie niet meer.
  - **Geen secrets in de log**: request/response van `/mobile-auth/`
    (code, access- en refresh-token) wordt nooit gelogd.
  - **Unieke uids**: gelijknamige vrije-tekst-items krijgen `#2`, `#3`;
    afvinken/verwijderen zoekt het item op via een verse lijst in plaats
    van de uid te parsen.
  - Fouten verschijnen als nette `HomeAssistantError`; `iot_class` is nu
    `cloud_polling`; account-ID als unique_id (geen dubbele entries).
  - Tests (`tests/`, pytest-homeassistant-custom-component) met
    gemockte AH-API.

- **Voorbeeldautomations toegevoegd** — `examples/automations/` bevat nu
  een uitgewerkt vaatwasblokjes-scenario: automatisch aftellen bij het
  starten van de vaatwasser, toevoegen aan de lijst via product-ID zodra
  de teller op 10 staat, en de voorraad weer bijtellen zodra het item
  wordt afgevinkt (via `todo.get_items` + `uid`-filter, omdat
  `todo.item_completed`'s trigger-data geen bruikbare `item_ids`
  teruggeeft). Zie de README-sectie "Voorbeeldautomations". Geen
  wijzigingen aan de integratie zelf.
- **v0.10** — Je kunt nu een product direct via een echt AH-webshopId
  toevoegen, zonder te zoeken:
  - Typ een **kaal getal** (bijv. "441199") in de to-do-lijst → wordt
    herkend als product-ID en direct correct gekoppeld toegevoegd. Daarna
    wordt de volledige lijst opnieuw opgehaald, zodat de echte productnaam
    meteen zichtbaar is (via de bestaande live-lookup-fallback uit v0.8).
  - De `appie.add_item`-service heeft nu ook een `product_id`-veld, voor
    gebruik in automations zonder de to-do-lijst.
- **v0.9** — De "Product #0"/afvink-sync-bug uit v0.8 is nu **bevestigd
  opgelost** (niet langer een gok): een gebruiker deelde de exacte ruwe
  AH-data via de waarschuwing-log. Voor items die je vanuit de AH-app zelf
  aan een product koppelt, zit de productinfo genest onder
  `productDetails.product` (met `webshopId`/`title`), in plaats van in de
  velden die voor rechtstreeks-toegevoegde producten gebruikt worden. De
  afvink-status (`strikedthrough`) bleek altijd al gewoon top-level te
  staan — dat werkte dus al goed zodra de naam/ID wél herkend werden. Dit
  is nu met de exacte real-world data uit die log getest en bevestigd
  correct.
- **v0.8** — Items die je **vanuit de AH-app zelf** aan een echt product
  koppelt (nadat ze als vrije tekst via HA waren toegevoegd) kwamen
  daarna als "Product #0" terug in HA, en afvinken in de app werd niet
  gesynchroniseerd. Dit wijst erop dat AH's antwoord voor zulke items een
  andere vorm heeft dan voor items die al vanaf het begin product-
  gekoppeld zijn toegevoegd (nog niet met zekerheid vastgesteld zonder de
  ruwe data te zien). Twee verdedigende verbeteringen, nog steeds
  best-effort:
  - `get_shopping_list_items` kijkt nu ook naar een eventueel genest
    `product`-object voor naam/ID/afvink-status, niet alleen naar
    top-level velden.
  - Is er wél een `productId` bekend maar geen naam, dan wordt de
    producttitel alsnog live opgehaald via het (bevestigde)
    product-detail-endpoint, in plaats van "Product #&lt;id&gt;" te tonen.
  - Blijft een item toch nog onherkenbaar, dan verschijnt nog steeds de
    waarschuwing met de ruwe AH-data in de log (zie "Debug-logging" in de
    README) — dat is de snelste weg naar een definitieve fix.
- **v0.7** — Items worden nu **standaard als vrije tekst** toegevoegd, in
  zowel de to-do-lijst als de `appie.add_item`-service. Dat is
  betrouwbaarder dan productmatching via zoeken: vrije tekst gebruikt
  exact de veldstructuur uit de documentatie die al bevestigd klopt (geen
  zoek-mismatches zoals "Bolletje" → "Bolognese chips", geen onbekende
  velden zoals bij "Product #0"). Wil je alsnog een echte productkoppeling
  (bijv. nodig voor `appie.checkout`), zet dan `free_text: false` in de
  service-aanroep — de verbeterde zoek-matching uit v0.6 blijft
  beschikbaar voor wie dat wil.
- **v0.6** — Twee fixes:
  - **Verkeerd product gematcht bij toevoegen** (bijv. "Bolletje" werd
    "Bolognese chips"). Bleek te komen doordat we AH's zoek-endpoint met
    `size=1` aanriepen — met maar één gevraagd resultaat is AH's
    "relevantste" resultaat kennelijk niet betrouwbaar. We vragen nu 10
    resultaten op en kiezen zelf de beste match (exacte titel-match, dan
    titel die met de zoekterm begint, dan titel die alle zoekwoorden
    bevat). Vindt niets redelijks, dan valt het terug op vrije tekst in
    plaats van een gok te wagen.
  - **"Product #0" in Home Assistant**: een item met zowel een lege naam
    als `product_id: 0` geeft aan dat AH's antwoord voor dat specifieke
    item geen bruikbare `description`/`productId` bevatte. Dit wordt nu
    ook zichtbaar gelogd als **waarschuwing** (dus zonder dat je
    debug-logging hoeft aan te zetten) met de volledige ruwe data van dat
    item.
- **v0.5** — Grote fix: de integratie gebruikte per ongeluk de "favoriete
  lijsten" (v3)-functie (meerdere, benoemde lijstjes) in plaats van de
  klassieke, enkelvoudige boodschappenlijst (v2) — die twee zijn losse
  functies in de AH-app. `api.py` is herschreven om uitsluitend
  `/mobile-services/shoppinglist/v2/items` te gebruiken (hetzelfde
  endpoint dat toevoegen al die tijd al gebruikte, en dat wél werkte). Dit
  loste de aanhoudende `HTTP 404` op. Afvinken en verwijderen zijn nu
  geïmplementeerd via een PATCH-hergebruik-truc i.p.v. de niet-werkende
  v3 GraphQL-mutatie. Alles is end-to-end getest tegen een mockserver die
  de lijst als levende state bijhoudt.
- **v0.4** — Een `HTTP 404` op het lijst-endpoint wordt nu opgevangen als
  "nog geen boodschappenlijst aangemaakt" (i.p.v. als harde fout). Verder
  wordt élke AH-API-aanroep nu op debug-niveau gelogd (methode, pad,
  status, response-body).
- **v0.3** — `productId` in `/mobile-services/lists/v3/lists` kwam van een
  live zoekopdracht ("melk") in plaats van een hardcoded `1` (later in
  v0.5 bleek dit hele endpoint niet de juiste aanpak; zie boven).
- **v0.2** — Login werd handmatig (plak de `appie://login-exit?code=...`
  URL uit Chrome DevTools) in plaats van via een lokale
  reverse-proxy-server, die in de praktijk niet betrouwbaar bleek.
- **v0.1** — Eerste pure-Python versie, zonder losse Go-`appie-bridge`.
