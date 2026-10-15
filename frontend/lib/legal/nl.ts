import type { Legal } from "./index.ts";
import { ADDRESS, EMAIL, ENTITY, REGISTRATION } from "./owner.ts";

export const nl: Legal = {
  terms: {
    title: "Gebruiksvoorwaarden",
    updated: "Laatst bijgewerkt: [OWNER: datum waarop deze voorwaarden ingaan]",
    sections: [
      {
        heading: "Over deze voorwaarden",
        body: [
          `ziftbook is een online boekingsdienst van ${ENTITY} ("wij", "ons"). Deze voorwaarden gelden als je via ziftbook een afspraak boekt, en als je een ziftbook-account voor je bedrijf gebruikt.`,
          "Hoe we met persoonsgegevens omgaan, lees je in onze privacyverklaring.",
        ],
      },
      {
        heading: "Een afspraak boeken",
        body: [
          [
            "Je afspraak is een overeenkomst tussen jou en het bedrijf waar je boekt. Wij leveren het boekingssysteem; wij zijn geen partij bij die overeenkomst.",
            "Het bedrijf bepaalt zijn diensten, prijzen, openingstijden en zijn regels voor annuleren en wijzigen. Die zie je voordat je boekt.",
            "Boeken via ziftbook is gratis voor jou. Je betaalt het bedrijf rechtstreeks voor zijn diensten.",
            "Sommige bedrijven bevestigen elke boeking met de hand. Dan is je tijd pas geboekt als het bedrijf bevestigt, en we mailen je hoe dan ook.",
            "Je kunt een boeking wijzigen of annuleren met de link in je e-mail, binnen de regels van het bedrijf. Voor al het andere over de afspraak neem je contact op met het bedrijf.",
          ],
        ],
      },
      {
        heading: "Gebruik ziftbook eerlijk",
        body: [
          "Geef je eigen, juiste gegevens op en boek alleen tijden die je echt wilt gebruiken. Het is niet toegestaan om:",
          [
            "te boeken met de gegevens van iemand anders zonder diens toestemming;",
            "automatisch tijden te boeken of op te vragen, of inhoud van ziftbook in bulk te kopiëren;",
            "onze beveiliging of limieten te omzeilen, of de dienst te verstoren.",
          ],
          "We kunnen boekingen en accounts die deze regels breken weigeren of verwijderen.",
        ],
      },
      {
        heading: "Accounts voor bedrijven",
        body: [
          [
            "Je bent verantwoordelijk voor je account en voor de mensen die je in je team uitnodigt. Houd je wachtwoord voor jezelf.",
            "Houd de gegevens over je bedrijf, je diensten, prijzen en boekingsregels juist.",
            "De relatie met je klanten is van jou: jij staat in voor je diensten en voor het naleven van de wet, ook de regels voor het sturen van aanbiedingen en nieuws.",
            "Eventuele prijzen voor bedrijven spreken we apart met je af.",
          ],
        ],
      },
      {
        heading: "De persoonsgegevens van je klanten",
        body: [
          "Voor de persoonsgegevens van je klanten (hun namen, contactgegevens, boekingen, notities, en de gegevens die daarbij worden vastgelegd) is je bedrijf de verwerkingsverantwoordelijke en zijn wij je verwerker. Deze voorwaarden zijn onze afspraak over die verwerking. Wij:",
          [
            "verwerken de gegevens alleen om ziftbook aan je te leveren, volgens je instructies zoals vastgelegd in deze voorwaarden en in hoe je ziftbook gebruikt, inclusief de doorgiften die in onze privacyverklaring staan;",
            "zorgen dat iedereen die ermee werkt tot geheimhouding verplicht is;",
            "beveiligen de gegevens, zoals beschreven in onze privacyverklaring;",
            "gebruiken alleen de dienstverleners uit onze privacyverklaring, en laten het je weten voordat we er een toevoegen of vervangen, zodat je bezwaar kunt maken;",
            "leggen dezelfde verplichtingen op aan die dienstverleners, en blijven zelf verantwoordelijk voor hen;",
            "laten het je weten als we denken dat een instructie in strijd is met de privacywetgeving;",
            "helpen je je klanten te antwoorden als zij hun privacyrechten gebruiken, en bij vragen over beveiliging en gegevensbescherming;",
            "laten het je zonder onnodige vertraging weten als een datalek de gegevens van je klanten raakt;",
            "geven je bij het opheffen van je account op verzoek een kopie van de gegevens, en verwijderen ze daarna, tenzij de wet ons verplicht ze te bewaren;",
            "geven je de informatie die je nodig hebt om aan te tonen dat deze verwerking aan de wet voldoet, en laten jou of een auditor van jouw keuze dat controleren, na een redelijke aankondiging.",
          ],
        ],
      },
      {
        heading: "De dienst",
        body: [
          "We doen ons best om ziftbook beschikbaar en veilig te houden, maar we kunnen niet beloven dat het altijd zonder onderbreking of fouten werkt. We kunnen functies wijzigen of verbeteren. Wijzigingen die invloed hebben op hoe bedrijven ziftbook gebruiken, laten we hun vooraf weten.",
        ],
      },
      {
        heading: "Aansprakelijkheid",
        body: [
          "Wij zijn niet verantwoordelijk voor de afspraak zelf of voor de diensten van het bedrijf. Voor zover de wet het toestaat, zijn we niet aansprakelijk voor indirecte schade, zoals gemiste inkomsten. Niets in deze voorwaarden beperkt aansprakelijkheid die de wet niet laat beperken, en niets erin tast je rechten als consument aan.",
        ],
      },
      {
        heading: "Stoppen",
        body: [
          "Je kunt op elk moment stoppen met ziftbook. Een bedrijf kan zijn account opheffen door ons te mailen. We kunnen een account dat deze voorwaarden breekt opschorten of opheffen, en we vertellen je waarom.",
        ],
      },
      {
        heading: "Wijzigingen in deze voorwaarden",
        body: [
          "Als we deze voorwaarden wijzigen, passen we de datum bovenaan aan. Is een wijziging belangrijk voor bedrijven, dan mailen we ze voordat die ingaat.",
        ],
      },
      {
        heading: "Recht en geschillen",
        body: [
          "Op deze voorwaarden is [OWNER: toepasselijk recht, bijv. Nederlands recht] van toepassing, en geschillen gaan naar [OWNER: bevoegde rechter]. Ben je consument, dan houd je de bescherming van het dwingende recht van het land waar je woont.",
        ],
      },
      {
        heading: "Contact",
        body: [`${ENTITY}, ${ADDRESS}. Registratienummer: ${REGISTRATION}. E-mail: ${EMAIL}.`],
      },
    ],
  },
  privacy: {
    title: "Privacyverklaring",
    updated: "Laatst bijgewerkt: [OWNER: datum waarop deze verklaring ingaat]",
    sections: [
      {
        heading: "Wie we zijn",
        body: [
          `ziftbook is een online boekingsdienst van ${ENTITY}, ${ADDRESS} (registratienummer ${REGISTRATION}). Heb je een vraag over je persoonsgegevens, mail dan naar ${EMAIL}.`,
        ],
      },
      {
        heading: "Wie bepaalt wat er met je gegevens gebeurt",
        body: [
          [
            "Als je via ziftbook boekt bij een bedrijf, bepaalt dat bedrijf wat er met je gegevens gebeurt: het is de verwerkingsverantwoordelijke. Wij bewaren en verwerken ze namens het bedrijf, als verwerker.",
            "Voor mensen met een ziftbook-account (eigenaren van bedrijven en hun teams) zijn wij verwerkingsverantwoordelijke voor de accountgegevens.",
            "Voor de gegevens die we bewaren om de dienst te beschermen (controles op misbruik en serverlogs) zijn wij verwerkingsverantwoordelijke.",
          ],
        ],
      },
      {
        heading: "Als je een afspraak boekt",
        body: [
          "We verzamelen:",
          [
            "je naam, e-mailadres en telefoonnummer;",
            "de boeking: de dienst, de tijd, bij wie, de prijs, en of die bevestigd, gewijzigd of geannuleerd is;",
            "de taal die je gebruikte;",
            "je IP-adres en browsertype, opgeslagen bij elke boeking, wijziging en annulering, als vastlegging van wie wat deed;",
            "als het bedrijf vraagt of je zijn aanbiedingen of nieuws wilt ontvangen: je antwoord, de precieze tekst die je zag, het tijdstip, en je IP-adres en browser, als bewijs van je keuze;",
            "notities die het bedrijf over je bijhoudt, als het die toevoegt.",
          ],
          "We gebruiken dit om je boeking te maken en te beheren, om je e-mails erover te sturen (zoals een link om je boeking te bevestigen, een bevestiging of het antwoord van het bedrijf, een herinnering ongeveer 24 uur van tevoren, en berichten als de boeking wijzigt of geannuleerd wordt), en zodat het bedrijf contact met je kan opnemen over je afspraak. De rechtsgrond is de overeenkomst die je met het bedrijf sluit, het gerechtvaardigd belang van het bedrijf om boekingen vast te leggen en misbruik te voorkomen, en je toestemming voor aanbiedingen en nieuws, die je altijd kunt intrekken.",
          "In ziftbook ziet alleen het bedrijf waar je boekte (de eigenaar en het team) je gegevens. Onze medewerkers bekijken ze alleen als dat nodig is om de dienst te laten werken of te ondersteunen. Elk bedrijf heeft zijn eigen, aparte klantenlijst, en bedrijven kunnen elkaars klanten niet zien.",
          "Ben je ingelogd op een ziftbook-account als je boekt, dan vullen we je gegevens in vanuit je account en koppelen we de boeking eraan.",
        ],
      },
      {
        heading: "Als je een ziftbook-account hebt",
        body: [
          "Van eigenaren van bedrijven en teamleden verzamelen we:",
          [
            "je e-mailadres, naam en taal, en je wachtwoord, dat we alleen als veilige eenrichtingshash opslaan;",
            "de naam en het land van je bedrijf, je rol, de naam die je klanten zien, je werktijden en afwezigheid;",
            "de uitnodigingen die je verstuurt, met de e-mailadressen die je uitnodigt;",
            "je inlogsessies, met IP-adres en browsertype;",
            "een overzicht van wijzigingen in het account van je bedrijf: wie wat deed en wanneer, met IP-adres en browsertype.",
          ],
          "We gebruiken dit om je account te leveren, het te beveiligen, en je bedrijf te laten zien wie wat veranderde. De rechtsgrond is onze overeenkomst met jou en ons gerechtvaardigd belang om de dienst veilig te houden.",
        ],
      },
      {
        heading: "Cookies",
        body: [
          "We gebruiken alleen cookies die ziftbook nodig heeft om te werken, nooit voor advertenties of analyse:",
          [
            "een inlogcookie, als je inlogt op een ziftbook-account. Die stopt als je uitlogt, na 7 dagen zonder gebruik, of uiterlijk na 30 dagen;",
            "een boekingslinkcookie, als je de link naar je boeking in een e-mail opent. Die blijft één uur geldig;",
            "een inlogcookie voor medewerkers, alleen voor de eigen beheerders van ziftbook. Die blijft hooguit 8 uur geldig.",
          ],
          "Je taal staat in het webadres, niet in een cookie. De teamagenda onthoudt de weergave die je koos in je browser. Cloudflare, dat onze site beschermt, kan een eigen beveiligingscookie plaatsen.",
        ],
      },
      {
        heading: "Bescherming tegen misbruik",
        body: [
          "Om geautomatiseerde boekingen en misbruik te stoppen, gebruikt de boekingspagina Cloudflare Turnstile, dat controleert of er een mens boekt. Voor die controle stuurt je browser gegevens zoals je IP-adres naar Cloudflare. We tellen ook pogingen per IP-adres en per e-mailadres, en bewaren het e-mailadres voor die telling alleen als eenrichtingshash.",
        ],
      },
      {
        heading: "E-mails",
        body: [
          "We versturen e-mails via Mailgun, vanuit de EU-regio. We houden niet bij of je op links in onze e-mails klikt.",
        ],
      },
      {
        heading: "Dienstverleners die we gebruiken",
        body: [
          [
            "Hetzner Online: onze servers, in Duitsland.",
            "Amazon Web Services: versleutelde nachtelijke back-ups van onze database, in de EU.",
            "Cloudflare: het netwerk vóór onze site, en de Turnstile-controle.",
            "Mailgun: het versturen van e-mails, vanuit de EU-regio.",
            "Grafana Labs: technische metingen, zoals hoe lang verzoeken duren, in de EU. Daarin staan geen namen, e-mailadressen of telefoonnummers.",
          ],
          "Sommige van deze bedrijven zijn gevestigd in de Verenigde Staten. Waar gegevens de EU kunnen verlaten, gebruiken we de waarborgen die de AVG vereist, zoals de EU-modelcontractbepalingen of het EU-VS-gegevensprivacykader.",
        ],
      },
      {
        heading: "Hoe lang we gegevens bewaren",
        body: [
          [
            "Boekingen en klantgegevens: zolang het bedrijf ziftbook gebruikt, tenzij jij of het bedrijf ons vraagt ze eerder te verwijderen. Heft een bedrijf zijn account op, dan verwijderen we zijn gegevens binnen 30 dagen.",
            "Accountgegevens: zolang het account bestaat. We verwijderen ze binnen 30 dagen nadat het account is opgeheven.",
            "Serverlogs: tot 7 dagen. Ze bevatten alleen wat we nodig hebben om de dienst te laten werken, vooral id's in plaats van namen, en nooit de inhoud van je berichten.",
            "Back-ups: automatisch verwijderd binnen twee weken.",
          ],
          "We bewaren iets alleen langer als de wet dat vereist.",
        ],
      },
      {
        heading: "Je rechten",
        body: [
          "Je kunt vragen om de persoonsgegevens over jou in te zien, te corrigeren, te verwijderen, het gebruik ervan te beperken, bezwaar te maken tegen het gebruik, en ze te ontvangen in een vorm die je elders kunt gebruiken. Gaf je toestemming, dan kun je die altijd intrekken.",
          `Voor een boeking behandelt het bedrijf waar je boekte je verzoek, dus neem eerst contact op met het bedrijf. Je kunt ons ook mailen op ${EMAIL}, dan helpen we je of sturen we je verzoek door.`,
          "Denk je dat er verkeerd met je gegevens wordt omgegaan, dan kun je een klacht indienen bij de toezichthouder voor gegevensbescherming in het EU-land waar je woont of werkt.",
        ],
      },
      {
        heading: "Beveiliging",
        body: [
          "Alle verbindingen met ziftbook zijn versleuteld. Wachtwoorden slaan we alleen als veilige hash op. De database houdt de gegevens van elk bedrijf gescheiden, zodat het ene bedrijf die van het andere niet kan lezen. Back-ups zijn versleuteld.",
        ],
      },
      {
        heading: "Wijzigingen in deze verklaring",
        body: [
          "Als we deze verklaring wijzigen, passen we de datum bovenaan aan. Is een wijziging belangrijk voor accounthouders, dan mailen we ze voordat die ingaat.",
        ],
      },
    ],
  },
};
