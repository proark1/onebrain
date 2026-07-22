# Buchhaltungsmodul & Beleg-Erfassung durch die Finance-Managerin

> **Status:** Planung — vor Umsetzung. Design-only, es ist noch nichts implementiert.
> **Datum:** 2026-07-22
> **Sprache des Dokuments:** Deutsch (die Plattform wird zweisprachig, siehe §9).

Dieses Dokument ist der Implementierungsplan für ein modulares Buchhaltungsmodul,
das Buchhaltungsarbeit **abnimmt und automatisiert**: Rechnungen kommen automatisch
herein (Upload **oder** E-Mail-Postfach), werden per Vision-Modell erfasst, **auf
Dubletten/Plausibilität geprüft** und **vor-kontiert** (Buchungsvorschlag mit
SKR-Konto + Steuerschlüssel); der Mensch **bestätigt nur noch** statt zu tippen
(Review-by-Exception). Daraus entstehen strukturierte, private Datensätze, eine
Übersicht inkl. USt-Voranmeldung-Kennzahlen, Antworten auf Fragen und ein Export in
die Steuerwelt (DATEV/lexoffice/sevDesk) — plus die dafür nötige app-weite
Mehrsprachigkeit. Es basiert auf einer Analyse des bestehenden Codes
(AI-Employees-Runtime, Drive, LLM/Vision, Intake/Capture, Konsole, MC-Provisioning).

Legende der Zustände im Text: **[vorhanden]** = existiert und wird wiederverwendet ·
**[neu]** = neu zu bauen · **[Kompromiss]** = bewusste Abwägung / zu verifizieren.

---

## §1 · Auf einen Blick

Kein neuer KI-Mitarbeiter: Die bestehende **Finance Managerin** (`finance_manager`,
Sophie Laurent) bekommt die Beleg-Fähigkeit. Das umgeht die harte 16-Personen-Sperre
der Roster-Definition (`app/ai_employees/contracts.py`, `_validate_default_organization`
erzwingt exakt 16 Mitarbeiter inkl. Länder-/Pronomen-Balance) vollständig. Das
Buchhaltungsmodul selbst wird ein **optionales Produkt ohne eigenen Container** —
dieselbe Bauart wie das KPI-Dashboard — das Mission Control pro Kunde ein- oder
ausschaltet.

**Wiederverwendet [vorhanden]:** das komplette Pro-Kunde-Modulsystem · Drive (Upload,
Malware-Quarantäne, Kategorien als Zugriffsgruppen, Vertraulichkeitsstufen, RLS) · der
ungenutzte JSON-Schema-Extraktionspfad + multimodale Modell-Transport im Backend · die
Freigabe-/Work-Product-Maschinerie der KI-Mitarbeiter.

**Wirklich neu [neu]:** ein Speicherort für strukturierte Belegdaten · ein Vision-Extraktor
(austauschbares Modell) mit §14-UStG-Schema für Ein- und Ausgang · ein **E-Mail-Eingang**
(Rechnungs-Postfach je Space, §4) · **Validierung + Dublettenerkennung** und ein
**Buchungsvorschlag** (SKR03/04-Konto + Steuerschlüssel), damit der Mensch nur noch bestätigt ·
**USt-Voranmeldung-Kennzahlen** aus den bestätigten Belegen · Bild-Anhang im Chat + ein governed
Abfrage-Tool für die Finance-Managerin · Modul-Oberfläche + Kategorie-Wähler im Upload ·
**Export-Connectoren** zu DATEV/lexoffice/sevDesk (§10) · die app-weite i18n-Schicht (§9).

**Automatisierungs-Fokus.** Die vier Bausteine, die aus reiner Erfassung echte Arbeitsersparnis
machen, sind fester Bestandteil des Plans: automatischer **E-Mail-Eingang**, **Buchungsvorschlag**,
**Dubletten-/Plausibilitätsprüfung** (Review-by-Exception) und **USt-Voranmeldung-Kennzahlen**
(Entscheidungen in §2, Technik in §4/§5, Einordnung im Phasenplan §8).

---

## §2 · Festgelegte Entscheidungen

| Thema | Entscheidung |
|---|---|
| **Mitarbeiter** | Die bestehende Finance Managerin erhält die Funktion — kein neuer „Buchhalter". DE-Wissen über ihre editierbare Persona + das Modul-Schema. |
| **Extraktion** | **Reines Vision-Modell, kein OCR.** Default `gemini-2.5-flash` (multimodal). Modell austauschbar **global** oder über ein Setting **nur für die Rechnungs-Bilderkennung**. Anbieter operator-wählbar (Standard oder EU-souverän). |
| **Belegtypen** | **Eingangs- und Ausgangsrechnungen** von Anfang an (Feld `direction`); Übersicht deckt Vorsteuer (Eingang) + Umsatzsteuer (Ausgang). |
| **Beleg-Eingang** | Zwei manuelle Wege (Drive-Upload, Chat) **plus automatischer E-Mail-Eingang**: ein Rechnungs-Postfach je Space, Anhang → automatische Erfassung (§4). Der eigentliche „Arbeit-abnehmen"-Hebel, weil Rechnungen überwiegend per Mail kommen. |
| **Fragen & Antworten** | **Beides:** strukturiertes Read-only-Tool für exakte Zahlen (SQL) **und** Volltext-RAG für Dokumentensuche (mit DPIA-Warnung). |
| **Verbuchung** | **Immer bestätigen.** Extraktion erzeugt einen `pending`-Entwurf; erst nach menschlicher Bestätigung (mit Korrektur) wird er `confirmed` und zählt in der Übersicht. |
| **Buchungsvorschlag** | Die Erfassung liefert nicht nur Felder, sondern einen **Kontierungs-Vorschlag** (SKR03/04-Sachkonto + Steuerschlüssel, ggf. Kostenstelle). Der Mensch bestätigt/korrigiert per Klick — aus „erfassen" wird „abnehmen". |
| **Prüfung** | Automatische **Dubletten-** + **Plausibilitäts-/Pflichtfeldprüfung** (Rechenprobe Netto+USt=Brutto, §14-Vollständigkeit, USt-IdNr, Rechnungsnr.-Eindeutigkeit). Nur Auffälliges landet im Review — **Review-by-Exception**. |
| **USt-Voranmeldung** | Aus den `confirmed`-Belegen werden **UStVA-Kennzahlen** (81/66/…) + prüfbare Zusammenstellung erzeugt. Die **ELSTER-Abgabe bleibt außen vor** (regulierte Aktion). |
| **DPIA** | **Sperrt nicht** — persistente Warnung + Audit-Eintrag, aber kein harter Block. |
| **Modularität** | Optionales Produkt, das der Operator pro Kunde in Mission Control an-/abwählt; für Bestandskunden automatisch aus. Gilt auch für Sophies Funktion. |
| **Sprache** | Deutsch primär, Englisch verfügbar; wählbar bei der Kundenanlage (§9). |
| **Schnittstellen** | **Top 3 (DE-first):** DATEV (Format-Export für den Steuerberater), lexoffice/Lexware Office und sevDesk (API-Push). Pluggable Connector, Export/Push zuerst, bidirektionaler Sync später (§10). |

Standardannahme für die noch offenen i18n-Detailfragen (revidierbar, siehe §11): leichte
Eigenlösung, UI-first, Kunden-Default + Nutzer-Umschaltung.

---

## §3 · Modularität — fast alles existiert schon

OneBrain hat bereits ein dreischichtiges Pro-Kunde-Modulsystem. Entscheidend:
`kpi_dashboard` und `ai_employees` sind optionale Produkte **ganz ohne eigenen Container**
(`modules=()` in `app/provisioning/bundles.py`) — sie laufen in `onebrain-api` und werden
nur über eine `AppInstallation` freigeschaltet. Genau diese Vorlage nutzt Buchhaltung.

- **L1 Produktauswahl [vorhanden]** — Platform-DB, pro Konto: `selected_module_ids`
  (`app/controlplane/base.py`) + `AppInstallation`. Maßgebliche Wahrheit.
- **L2 Container [n. z.]** — Compose-Profiles pro Box; entfällt, da Buchhaltung in
  `onebrain-api` läuft.
- **L3 Laufzeit-Gate [neu, klein]** — Router prüft die `AppInstallation` und liefert sonst
  403 „nicht aktiviert". Muster: `app/ai_employees/access.py`.

**Konkret zu tun:**

1. **Katalog** — `app/provisioning/bundles.py`: `"buchhaltung"` in `OPTIONAL_MODULE_IDS`,
   ein `ProvisioningModule(… modules=())`, ein `BUCHHALTUNG_APP` + Purposes (Vorlage:
   `KPI_APP`/`KPI_PURPOSES`).
2. **App-Registry + Install-Validierung** — `"buchhaltung"` in `app/platform/base.py` `APP_IDS`
   + Purposes (`accounting_read`/`accounting_ingest`/`accounting_configure`/`accounting_export`).
   `validate_installation` prüft `APP_IDS`+`PURPOSES`, also müssen die Platform-Install-API und
   die Frontend-Liste (`onebrain-web/src/components/spaces-panel.tsx`) die neue app_id + Purposes
   ebenfalls kennen — sonst schlägt das manuelle Aktivieren über das Spaces-Panel fehl.
3. **Gate-Router** — neuer `app/routers/accounting.py`, gemountet neben `kpis`/`ai_employees`
   in `app/main.py`, 403 ohne Installation.
4. **MC-Auswahl** — schon verdrahtet: die Provisioning-Anfrage trägt `module_ids`, es gibt
   einen Katalog-Endpoint (`GET /api/provisioning/modules`) und die Auswahl-UI im Next.js
   `operator-panel.tsx` (Provisioning-Tab, datengetrieben aus dem Katalog → Haken erscheint
   automatisch). **Pflichtänderungen:** (a) `CustomerProvisionCreate.module_ids` `max_length`
   4 → 5 (`app/routers/provisioning.py`); (b) die neue app_id auch im **Dev-Gate-Modulset**
   aufnehmen, damit die Buchhaltung am Development-Gate ebenso verfügbar und testbar ist wie bei
   Kunden — ein bloßer `max_length`-Bump lässt das Dev-Gate sonst außen vor.

**Aus für Bestandskunden = automatisch:** Wer `buchhaltung` nicht in `selected_module_ids`
hat, bekommt keine `AppInstallation` → das Gate liefert 403. Keine Migration schaltet
jemanden ein; kein box.env-/Compose-/Secret-/Release-Eingriff nötig.

---

## §4 · Drei Eingangs-Wege, ein Extraktionspfad

Alle Eingänge münden in dieselbe Regel: **„eine als `buchhaltung` kategorisierte,
malware-saubere Datei existiert" → Extraktion anstoßen.** Zwei Wege sind manuell, der
dritte (E-Mail) macht den Beleg-Eingang **automatisch** — der eigentliche Arbeitsersparnis-Hebel.

- **Weg A — Drive direkt:** Upload mit Kategorie „Buchhaltung" wählen. Eine `category` im
  Drive ist eine `AccessGroup(kind="department")` (`app/platform/base.py`), analog zum
  bestehenden abgeschotteten `captured_input`-Compartment. Der Upload-Dialog bekommt dafür
  einen **Kategorie-Wähler** (heute wird die Kategorie nur vom Zielordner geerbt).
- **Weg B — im Chat mit Sophie:** der Chat-Composer lädt den Anhang **über Drive** hoch
  (nutzt Malware-Quarantäne + Speicher), Kategorie automatisch `buchhaltung`, und
  referenziert dann die `drive_file_id` im Turn.
- **Weg C — E-Mail-Postfach [neu; Intake/Capture vorhanden, Mail-Kanal neu]:** ein Rechnungs-
  Postfach bzw. eine Weiterleitungsadresse je Space (z. B. `belege@<kunde>…`). Eingehende Mail →
  Anhänge (PDF/Bild) werden **abgetrennt und über denselben Drive-Pfad** abgelegt
  (Malware-Quarantäne + Speicher), Kategorie automatisch `buchhaltung` → dieselbe
  Extraktionsregel greift. Das ist der Weg, der den Beleg-Eingang **ohne Handgriff**
  automatisiert. Wiederverwendet wird der bestehende **Intake/Capture-Pfad**
  (`app/routers/service.py` `capture` → abgeschottetes `INTERNAL/captured_input`-Compartment,
  Space-Routing); **neu** ist der eingehende **Mail-Kanal selbst** (heutige Kanäle:
  `communication-widget/-voice/-api` — kein Mail-in) inkl. Absender→Space-Zuordnung sowie
  Spam-/Schleifen-Schutz. Der Mail-Empfangsweg der Box ist eine offene Infra-Frage (§11).

**Voraussetzung für alle Wege:** die `buchhaltung`-Zugriffsgruppe muss im Space existieren und
der hochladende Nutzer (bzw. der Postfach-Serviceprincipal) Mitglied sein — sonst schlägt die
Ablage/Extraktion fehl. Der Installations-/Phase-0-Bootstrap muss die Gruppe **und** die
Mitgliedschaften der Finanz-Nutzer mit anlegen, nicht nur die `AppInstallation`.

**Malware-Quarantäne bleibt zwingend** (`app/drive/…`): extrahiert wird erst nach sauberem
Scan — Downloads und Index sind bis dahin gesperrt (HTTP 423). Der App-Layer kann kein
„sauber"-Urteil fälschen (fenced DB-Funktion).

---

## §5 · Architektur & Extraktion

- **Neues Backend-Paket `app/accounting/` [neu]** — Service, Store (Memory + Postgres, wie
  überall), Extraktor, Contracts. Dazu **zwei neue RLS-Tabellen** nach dem Muster der
  Drive-Migrationen (`migrations/versions/0033_onebrain_drive.py`, gleiche
  tenant/account/space-Scope-Policy):
  - `accounting_documents` — ein Datensatz je Rechnung: **Richtung Eingang/Ausgang**,
    Aussteller/Lieferant, Rechnungsnummer, Rechnungs-/Leistungsdatum, Netto/USt/Brutto **je
    Steuersatz**, **Buchungsvorschlag** (vorgeschlagenes + bestätigtes SKR03/04-Sachkonto,
    Steuerschlüssel, optional Kostenstelle), **Dubletten-Schlüssel** + **Prüf-Flags**
    (Rechenprobe, §14-Vollständigkeit, USt-IdNr), Status (`pending`/`confirmed`), Konfidenz,
    Verweis auf `drive_file_id` + Revision.
  - `accounting_line_items` — die Positionen.

  Das ist der heute fehlende Speicherort — Drive-Revisionen halten nur Blob-Zeiger, Chunk-Meta
  sind Zugriffslabels. Die Migration muss außerdem `REQUIRED_ALEMBIC_REVISION` in
  `app/db/schema.py` auf die neue Revision bumpen, sonst schlägt der CI-Migrations-Gate fehl.

- **Pluggable `InvoiceExtractor` — reines Vision-Modell [neu, Backend-Pfad vorhanden].**
  Schema nach **§14 UStG-Pflichtangaben**, für Ein- & Ausgang: Aussteller/Lieferant &
  Empfänger, Steuernummer/USt-IdNr, Rechnungs- & Leistungsdatum, Rechnungsnummer, Positionen,
  Nettobeträge je Steuersatz (0/7/19 %), USt-Betrag, Brutto, Skonto/Zahlungsziel; Flags für
  Kleinbetragsrechnung (<250 €), Reverse-Charge, innergemeinschaftlich. Jurisdiktionsfeld
  `DE` → erweiterbar.
  - Der JSON-Schema-Ausgabepfad (`response_format: json_schema`) ist im LiteLLM-Agent-Backend
    (`app/ai_employees/backends/litellm.py`) schon verdrahtet, nur ungenutzt — wir aktivieren
    ihn zusammen mit einem **multimodalen Message-Builder** (Bild → Bildblock; heute sind alle
    Message-Builder text-only, `dict[str, str]`).
  - **PDF-Belege:** werden vor dem Modell **seitenweise zu Bildern gerastert** (PyMuPDF ist
    bereits als Abhängigkeit vorhanden) und dann als Bildblöcke übergeben — so deckt der
    reine-Vision-Pfad auch das versprochene PDF-Hochladen ab.
  - **Kein OCR — das Bild geht direkt ans Modell.** Default `gemini-2.5-flash`. Modell
    austauschbar global oder über ein Setting nur für die Rechnungs-Bilderkennung.
    **Wichtig:** Der Extraktor ist ein neuer Aufrufpfad und muss die Sovereign-Routing-Regel
    (`app/llm/tiered.py`, `sovereign_min_tier`) für „confidential"-Belege **selbst erzwingen**
    bzw. fail-closed sein, wenn kein zugelassenes Modell verfügbar ist — ein bloßer Verweis auf
    tiered.py steuert die Datenresidenz nicht automatisch. Konsequenz: ein bilderkennungsfähiges
    Modell muss konfiguriert sein; das gewählte Modell muss in `app/llm/pricing.py` eingetragen
    werden, sonst Kostenwert `None`.

- **Buchungsvorschlag / Kontierung [neu] — die Kern-Automatisierung.** Über die reine
  Felderfassung hinaus schlägt das Modul die **Buchung** vor: eine regelbasierte Zuordnung (v1)
  keyed auf Belegart/Richtung/Steuersatz/Lieferant → **SKR03/04-Sachkonto + Steuerschlüssel**
  (ggf. Kostenstelle), mit Konfidenz. Der Vorschlag ist im Bestätigungsschritt (§2) editierbar;
  Korrekturen speisen später eine lernende Zuordnung. Ohne diesen Baustein ist das Modul ein
  Datenerfasser, mit ihm nimmt es das Buchen ab. Liefert zugleich die Sachkonten, die der
  DATEV-Export (§10) braucht.
- **Validierung & Dublettenerkennung [neu].** Vor dem Review läuft eine automatische Prüfung:
  **Dubletten** über Datei-Hash + unscharfen Schlüssel (Aussteller + Rechnungsnummer + Betrag +
  Datum), **Rechenprobe** (Summe Netto je Satz + USt = Brutto), **§14-Pflichtfelder** vollständig,
  **USt-IdNr**-Format, **Rechnungsnr.-Eindeutigkeit** je Aussteller. Treffer werden markiert, nicht
  verworfen; nur Auffälliges braucht menschliche Augen (**Review-by-Exception**). Das macht den
  Bestätigungsschritt schnell und verhindert Doppelbuchungen — wichtig, sobald E-Mail-Eingang und
  ein Foto denselben Beleg liefern.
- **Übersicht = strukturierte Abfragen [neu].** Die Modul-Übersicht kommt aus SQL auf die
  Belegtabellen — USt je Quartal (Vorsteuer aus Eingang, Umsatzsteuer aus Ausgang),
  Netto/Brutto je Monat, Top-Lieferanten und -Kunden, offene Posten, letzte Belege. Genauer
  und billiger als RAG für Zahlen. Nur `confirmed`-Datensätze zählen. **Darauf baut die
  USt-Voranmeldung auf:** aus denselben Belegen werden die **UStVA-Kennzahlen** (z. B. 81/66) +
  eine prüfbare Zusammenstellung erzeugt — die wiederkehrende Monats-/Quartalsarbeit gebündelt;
  die ELSTER-Abgabe bleibt bewusst außen vor (§7/§11).

---

## §6 · Finance-Managerin: zwei Laufzeit-Erweiterungen

Beide sind im Code „vorverdrahtet" — die Bausteine liegen bereit und sind nur deaktiviert.

1. **Bild-Anhang im Chat [neu, Transport vorhanden].** Der Turn-Contract
   (`AiEmployeeTurnCreate`) ist heute reiner Text (`additionalProperties: false`). Wir
   erweitern ihn um eine `drive_file_id`-Referenz und bauen den multimodalen Message-Builder.
   Der LiteLLM-Transport trägt Bildblöcke bereits; das Standardmodell ist multimodal. Der neue
   `drive_file_id`-Anhang muss in den **Idempotency-Key-Hash** des Turns einfließen, sonst gibt
   ein Retry mit anderem Anhang (aber gleichem Key) das alte Ergebnis zurück.

2. **Governed Abfrage-Tool [neu, Tool-Pfad vorhanden].** Die Runtime
   (`app/ai_employees/runtime.py`) deaktiviert Tools ausdrücklich nur „until a governed
   capability is bound". Wir binden genau **ein** read-only Tool, das die Belegtabellen
   **im Zugriffsrahmen** abfragt („wie viel USt in Q2?"). Zahlen exakt aus SQL, nicht aus RAG.

Allgemeine Buchhaltungsfragen beantwortet sie schon heute über ihre Persona. Für
Dokumentensuche kommt zusätzlich das Volltext-RAG hinzu.

**Auch die Mitarbeiter-Funktion ist modular:** beide Erweiterungen sind an den Modul-Schalter
gekoppelt. Ist das Buchhaltungsmodul für einen Kunden nicht installiert, bekommt Sophie dort
weder Bild-Upload noch Abfrage-Tool — sie bleibt die normale Finance-Managerin.

---

## §7 · Datenschutz, GoBD & die DPIA-Warnung

- **Privat, nicht öffentlich — strukturell.** Belege werden mit `classification="confidential"`
  + Kategorie `buchhaltung` (Zugriffsgruppe) abgelegt: nur Gruppenmitglieder + Admins sehen
  sie je wieder; Aufweiten ist admin-gesperrt (`_policy_widens`). Strukturierte Daten in
  RLS-Tabellen mit harter Mandanten-Isolation. Downloads sind bereits gehärtet (attachment,
  nosniff, no-store).
- **Datenschutz-Hebel bei reinem Vision:** Da es keinen OCR-Fallback gibt, verlässt jedes
  Beleg-Bild die Box Richtung Modell. Der Compliance-Hebel ist damit die **Modellwahl**
  (EU-souverän) + „confidential"-Klassifizierung + DPIA-Warnung.
- **DPIA warnt, sperrt nicht.** Persistentes Banner „DPIA für Buchhaltung noch offen" +
  Audit-Eintrag, aber kein harter Block. **Achtung — eine reine UI-Warnung reicht nicht:** die
  Drive-Publikation indexiert heute nur bei `drive_policy_mode=storage_and_indexing`. Für die
  „warnen statt sperren"-Zusage muss der Accounting-Indexpfad diesen Gate **modul-scoped für die
  `buchhaltung`-Kategorie überschreiben** (mit Audit-Eintrag) — sonst passiert trotz Warnung
  keine Indexierung. Reversibel und nachvollziehbar.
- **GoBD ↔ DSGVO-Spannung [Kompromiss, kein Blocker].** Rechnungen/Buchungsbelege verlangen
  unveränderbare Aufbewahrung — **seit 2025 8 Jahre** (durch das Vierte Bürokratieentlastungs-
  gesetz von 10 auf 8 gesenkt, §147 AO; andere Unterlagen wie Bücher/Jahresabschlüsse bleiben
  10 Jahre), die DSGVO das Löschen. Lösung: die Aufbewahrungsfrist als konfigurierbaren Wert je
  Belegart modellieren und Buchhaltungsdatensätze an das vorhandene Legal-Hold-Gate hängen,
  damit eine Löschung Steuerbelege nicht vorzeitig bricht.

---

## §8 · Phasenplan

| Phase | Inhalt | Ergebnis | Aufwand |
|---|---|---|---|
| **0** | Modul-Gerüst end-to-end: Katalog + `APP_IDS` + Gate-Router + leere Tabellen + Nav/Panel-Skelett + MC-Checkbox | Beweist Modularität, aus per Default | klein |
| **i18n** | App-weites Fundament: `default_locale` bei Provisioning + Box-Flow, Frontend-i18n-Schicht (de/en), Shell & Navigation übersetzt — vor der Buchhaltungs-UI | Basis für zweisprachige Oberflächen | mittel |
| **1** | Drive-Kategorie-Wähler + Extraktionspfad (entkoppelt vom Index-Job) + **Validierung/Dublettenerkennung** + **Buchungsvorschlag** (SKR03/04 + Steuerschlüssel) → strukturierte Datensätze + Bestätigung als **Review-by-Exception** + Übersichts-Dashboard | Belege rein, **geprüft**, **vor-kontiert**, verbucht, sichtbar | groß |
| **2** | **E-Mail-Eingang**: Rechnungs-Postfach je Space → Anhang über den Drive-/Intake-Pfad → Extraktion → `pending` | Belege kommen **automatisch** rein | mittel |
| **3** | Chat-Anhang (multimodaler Turn) + governed Q&A-Tool + Volltext-RAG (mit DPIA-Warnung) | Sophie erfasst & beantwortet | groß |
| **4** | **USt-Voranmeldung-Kennzahlen** (81/66/…) + erweiterte Auswertung aus den bestätigten Belegen | wiederkehrende USt-Arbeit gebündelt | mittel |
| **5** | **Buchhaltungs-Tool-Schnittstellen** (§10): DATEV-Format-Export → lexoffice/sevDesk-API-Push; dazu weitere Vision-Anbieter | Export in die Steuer-/Buchhaltungswelt | groß |
| **6** | Bidirektionaler Sync, DATEVconnect/Unternehmen online, weitere Länder/Jurisdiktionen | Ausbau | später |

**Phase 1 bündelt bewusst Erfassung + Prüfung + Buchungsvorschlag** — erst das zusammen macht aus
Datenerfassung echte Arbeitsersparnis (Mensch bestätigt statt tippt). Die Phasen **3–5 ernten alle
die Phase-1-Daten** und sind nach Priorität umstellbar (z. B. DATEV-Export vor Chat/RAG, wenn der
Steuerberater-Handoff zuerst zählt).

Ehrlich eingeordnet: Gesamt eher **mehrere Monate solo** (vergleichbar mit dem Drive-Aufbau).
Phase 0, i18n und 1 liefern aber schon sichtbaren Wert; der **E-Mail-Eingang (Phase 2)** ist der
Punkt, ab dem sich die Zeitersparnis im Alltag bemerkbar macht.

---

## §9 · Mehrsprachigkeit (app-weit)

**Deutsch als Hauptsprache, Englisch verfügbar** — die Sprache wird **bei der Kundenanlage
gewählt**, im selben MC-Schritt wie die Modul-Auswahl. Das ist eine Plattform-Fähigkeit, kein
Modul-Detail: die Konsole hat heute **kein** i18n (`onebrain-web/src/app/layout.tsx` setzt
`lang="en"` fest, englische Literale in ~12 Panels; keine i18n-Bibliothek). Darum ein eigener
Fundament-Baustein, der vor der Buchhaltungs-UI kommt, damit die gleich zweisprachig entsteht.

- **Sprache als Provisioning-Einstellung [neu, klein].** Ein `default_locale`-Feld
  (`de`/`en`, Default `de`) an der Provisioning-Anfrage — fließt denselben Weg wie
  `module_ids`: Bootstrap-Deskriptor (`app/provisioning/customer_bootstrap.py`) → Box-Config →
  Reconcile **persistiert `default_locale` am Konto im Platform-Datenmodell** (neue Spalte;
  dauerhafte, abfragbare Quelle für die Konsole — nicht nur ein Box-Env-Wert, der beim nächsten
  Render verloren ginge). In der MC-Konsole ein kleines Sprach-Dropdown neben den Modul-Haken
  (`operator-panel.tsx`).
- **Leichte i18n-Schicht im Frontend [neu, Aufwand].** Kataloge `de.ts`/`en.ts`, ein
  `LocaleProvider` + `t()`-Hook, `<html lang>` dynamisch, locale-bewusstes `Intl` (Datum,
  Zahlen, EUR — heute meist `undefined`/hartes `"en"`). Der Löwenanteil ist das mechanische
  Herauslösen der englischen Texte aus ~12 Panels + Drive — phasenweise: Shell/Navigation +
  Buchhaltung zuerst, Rest schrittweise.

**Chat ist schon abgedeckt:** die KI-Mitarbeiter antworten bereits „in der Sprache des
Nutzers" (`app/ai_employees/prompting.py`) — Sophie antwortet auf Deutsch, wenn man sie auf
Deutsch anspricht. Offen ist vor allem die **Oberfläche** und optional Backend-Meldungen.

---

## §10 · Buchhaltungs-Tool-Schnittstellen (Top 3)

Das Modul erzeugt strukturierte Buchungen — die müssen in das System, mit dem der Kunde bzw.
sein Steuerberater tatsächlich arbeitet. Für den DE-Markt (erster Experte = Deutschland) sind
die drei wichtigsten Ziele (Standardannahme, siehe §11):

1. **DATEV [Pflicht] — export-first.** Der Standard der Steuerberater. Primär ein
   **DATEV-Format-Export** (ASCII/EXTF-„Buchungsstapel"-CSV mit Header-Zeile, Konten nach
   **SKR03/SKR04**, Berater-/Mandanten-/Wirtschaftsjahr-Angaben), den der Mandant seinem
   Steuerberater übergibt. Später optional tiefer: **DATEVconnect** (lokale REST-API) bzw.
   **DATEV Unternehmen online** (Beleg- + Buchungsdaten in die Cloud).
2. **lexoffice / Lexware Office — API-Push.** Großes Cloud-Buchhaltungstool für dt. KMU,
   offene **REST-API**; Belege/Ausgaben (Vouchers) aus den extrahierten Rechnungen anlegen.
3. **sevDesk — API-Push.** Weit verbreitetes Cloud-Tool für KMU/Selbstständige, **REST-API**
   für Belege/Ausgaben.

**Architektur — ein pluggable Connector, kein Sonderweg.** Ein `AccountingExportConnector`-
Interface nach dem Muster des bestehenden Connector-Frameworks (`app/ai_employees/connectors/`,
Google Calendar als Referenz-Implementierung). Zwei Ausprägungen:

- **Datei-Export** (DATEV-Format): erzeugt eine valide DATEV-CSV zum Download — kein
  Fremdsystem-Zugriff nötig.
- **API-Push** (lexoffice/sevDesk): legt Belege per REST an; Zugangsdaten (API-Key/OAuth) im
  vorhandenen **verschlüsselten Connector-Secret-Store** (`connectors/secrets.py`,
  `secret_encryption_key`) — nie in Prompts, Exports oder API-Antworten (bestehende Invariante).

**Kontierung (SKR03/SKR04).** Ein Export braucht Sachkonten — die liefert bereits der
**Buchungsvorschlag** aus §5 (regelbasierte Zuordnung je Belegart/Richtung/Steuersatz/Lieferant +
Korrektur im Bestätigungsschritt, §2; lernende/vollautomatische Kontierung später). Der Export
konsumiert also die bestätigten Konten, statt eine eigene Zuordnung zu erfinden.

**Freigabe + Purpose + Modularität.** Export/Push bewegt vertrauliche Buchungsdaten **aus
OneBrain heraus** → dafür ein eigener, separat freigebbarer Purpose **`accounting_export`**
(nicht in `…_read`/`…_ingest`/`…_configure` gebündelt). Ein Push in ein externes System ist eine
**externe Aktion** und läuft über die vorhandene Freigabe-/Action-Pipeline (nicht autonom, vgl.
`AI_EMPLOYEE_EXTERNAL_ACTION_TYPES`); der DATEV-Datei-Export ist ein nutzerausgelöster Download.
Die Connectoren gehören zum Buchhaltungsmodul → automatisch modular, und **Connector-Konfiguration
und -Bindings sind space-scoped** (pro Workspace — konsistent mit `enabled_space_ids` der
`AppInstallation` und den bestehenden Connector-Bindings), nicht pauschal pro Kunde.

**Reihenfolge:** DATEV-Format-Export zuerst (höchster Nutzen, kein Fremd-API-Risiko), dann
lexoffice- + sevDesk-API-Push. Bidirektionaler Sync (Rücklesen/Abgleich) ist bewusst später
(Phase 6).

---

## §11 · Offen / zu verifizieren

**Entschieden (Runde 2):** kein OCR — reines Vision-Modell, Default Gemini, austauschbar
global oder speziell für die Rechnungs-Bilderkennung, Anbieter operator-wählbar · Eingangs-
und Ausgangsrechnungen von Anfang an.

**Offene i18n-Unter-Entscheidungen (Standardannahme in Klammern, revidierbar):**

- Technik: leichte Eigenlösung *(empfohlen)* vs. Bibliothek (next-intl).
- Umfang: nur Oberfläche zuerst *(empfohlen)* vs. auch Backend-Meldungen/E-Mails/Exporte.
- Sprachwahl: Kunden-Default + Nutzer-Umschaltung *(empfohlen)* vs. nur Kunden-Default.

**Offene Schnittstellen-Entscheidungen (Standardannahme, revidierbar):**

- Tool-Set: DATEV + lexoffice + sevDesk *(empfohlen)*; 3. Platz auf ein internationales Tool
  (Xero/QuickBooks) tauschbar, oder ein viertes ergänzen.
- Tiefe: Export/Push zuerst *(empfohlen)* vs. bidirektionaler Sync (deutlich größer).

**Offene Eingang- & Automatisierungs-Entscheidungen (Standardannahme, revidierbar):**

- **E-Mail-Eingang — Infra:** Die Box hat heute **keinen** eingehenden Mail-Weg (Kanäle:
  widget/voice/api). Offen: eigener MX/Mailempfang je Box vs. zentrale Relay-/Inbound-Webhook-
  Adresse *(empfohlen: ein Inbound-Relay, das je Space an den Capture-Endpoint zustellt)* — plus
  Postfach-Adressschema, Absender→Space-Zuordnung, Spam-/Schleifen-Schutz.
- **Buchungsvorschlag:** regelbasiert *(empfohlen für v1)* vs. lernend/ML; Default-Kontenrahmen
  **SKR03** *(Standard vieler DE-KMU)* vs. SKR04 — je Kunde konfigurierbar.
- **USt-Voranmeldung:** nur **Kennzahlen + prüfbarer Report** *(empfohlen)*; die **ELSTER-
  Direktabgabe bleibt außen vor** (regulierte Aktion) — höchstens ein ELSTER-fähiger Export später.

**Umsetzungs-Notizen:**

- **Vision-Modell erforderlich:** Ohne OCR-Fallback muss auf der Box ein
  bilderkennungsfähiges Modell konfiguriert sein. Fehlt es, ist die Extraktion sichtbar
  deaktiviert (klare Meldung) statt still zu scheitern.
- **Deutsche/englische Oberfläche:** siehe §9 (Workstream, nicht nur eine Notiz).
- **Preis-Tabelle:** Das gewählte Vision-Modell muss in `app/llm/pricing.py`, sonst Kostenwert
  `None`. Vision-Calls kosten mehr Tokens als Text.
- **MC-Oberfläche [geklärt]:** Die Modul-Auswahl liegt bereits in der Next.js-Konsole
  (`operator-panel.tsx`, Provisioning-Tab) und ist datengetrieben — der Buchhaltungs-Haken
  erscheint automatisch, sobald der Backend-Katalog den Eintrag hat.

---

## Nächste Schritte

Auf Zuruf (noch nichts implementiert): **Phase 0** als schmaler, standardmäßig
ausgeschalteter Modul-Rahmen (eigener PR), danach das **i18n-Fundament** vor der
Buchhaltungs-UI, dann **Phase 1+**.
