"""Faisceau d'indices d'identité d'un site candidat et nature des coordonnées (logique pure).

Niveaux : ``confirme`` (identifiant légal attribué au site, référence SIREN de ce site, ou faisceau
mentions légales + adresse/dirigeant enregistrés + domaine), ``probable`` (nom + indice propre à
l'entité), ``groupe`` (site de la marque ou du groupe, pas de l'entité), ``non_fiable``. Les blocs
hébergeur/prestataire sont ignorés ; un autre SIREN publié interdit toute confirmation. Chaque
indice garde son URL et un extrait : rien n'est affirmé sans source.
"""

import re

from .cascade.keys import domain_key
from .discovery import brand_word, name_words
from .verification import contact_relevance, normalized, siren_contradicted

STRONG = {"siren", "tva", "reference_siren"}
VOIE = {
    "rue",
    "avenue",
    "av",
    "boulevard",
    "bd",
    "chemin",
    "route",
    "allee",
    "place",
    "quai",
    "impasse",
    "cours",
    "traverse",
    "zone",
    "za",
    "zi",
    "zac",
    "parc",
    "lieu",
    "dit",
    "rond",
    "point",
    "square",
    "montee",
    "voie",
    "residence",
    "batiment",
    "bat",
    "lot",
    "lotissement",
    "de",
    "du",
    "des",
    "la",
    "le",
    "les",
    "l",
    "d",
    "et",
    "en",
    "sur",
    "bis",
    "ter",
    "cs",
    "bp",
}
LEVELS = ("confirme", "probable", "groupe", "non_fiable")


SEP = r"[\s .]{0,3}"
LEGAL_WORDS = re.compile(
    r"mentions?\s+l[ée]gales|informations?\s+l[ée]gales|[ée]diteur|"
    r"(?:directeur|directrice|responsable)\s+de\s+(?:la\s+)?publication|si[èe]ge\s+social|"
    r"capital\s+(?:social\s+)?de|\brcs\b|\bsiren\b|\bsiret\b|legal\s+notice|imprint",
    re.I,
)
THIRD_PARTY = (
    r"h[ée]berg\w*|hosting|hosted\s+by|conception|r[ée]alisation|cr[ée]ation\s+(?:du\s+)?site|"
    r"cr[ée]ation\s*:|cr[ée]dits?\s*:|"
    r"d[ée]velopp[ée]\s+par|webdesign|agence\s+web|propuls[ée]\s+par|powered\s+by|"
    r"site\s+r[ée]alis[ée]|design\s*:|"
    # « Le site X a été réalisé par… », « contenus rédigés par… » : le prestataire suit, pas l'éditeur.
    r"r[ée]alis[ée]e?s?\s+par|con[çc]ue?s?\s+par|cr[ée][ée]e?s?\s+par|r[ée]dig[ée]e?s?\s+par"
)
THIRD_PARTY_START = re.compile(THIRD_PARTY, re.I)
THIRD_PARTY_WINDOW = 250
# Identifiant légal d'un prestataire : « RCS Lille 424 761 419 » ou « 424 761 419 RCS Lille ».
LEGAL_ID = re.compile(
    r"\b(?:rcs|r\.\s?c\.\s?s\.?|siren|siret|immatricul\w*)\b[^\d€]{0,60}(\d(?:[\s . ]?\d){8,13})"
    r"|(\d{3}[\s . ]?\d{3}[\s . ]?\d{3}(?:[\s . ]?\d{5})?)\s*(?:rcs|r\.\s?c\.\s?s)",
    re.I,
)
OFFICER_ROLE = re.compile(
    r"directeur|directrice|pr[ée]sident|g[ée]rant|publication|dirig\w*|\bceo\b|fondat|"
    r"managing\s+director|chairman",
    re.I,
)


def tva_number(siren):
    """Numéro de TVA intracommunautaire français, calculé depuis le SIREN (jamais publié par nous)."""
    if not re.fullmatch(r"\d{9}", siren or ""):
        return ""
    return f"FR{(12 + 3 * (int(siren) % 97)) % 97:02d}{siren}"


def separated(text):
    """Coupe les suites de chiffres jointes par la normalisation (« 987 654 321, 12 rue » donnait
    11 chiffres et masquait l'identifiant publié) ; ``legal_identifiers`` reste inchangé."""
    return re.sub(r"[,;:()\[\]|/\n]|\.(?=\s)", " x ", text)


def third_party_spans(text):
    """Blocs hébergeur / prestataire / agence web : ``(début, fin)`` et SIREN de ces tiers.

    Un bloc s'arrête à l'identifiant légal du prestataire (et à ses répétitions) : au-delà, le texte
    peut redevenir celui de l'éditeur. Sans identifiant, la fenêtre fixe historique s'applique."""
    spans, sirens = [], set()
    for found in THIRD_PARTY_START.finditer(text):
        start = found.start()
        window = text[start : start + THIRD_PARTY_WINDOW]
        identifiers = [(m.end(), re.sub(r"\D", "", m.group(1) or m.group(2))[:9])
                       for m in LEGAL_ID.finditer(window)]
        end = start + len(window)
        if identifiers:
            provider = identifiers[0][1]
            sirens.add(provider)
            end = start + max(stop for stop, number in identifiers if number == provider)
        # Prestataires enchaînés (« Conception : … Hébergeur : … SIREN … ») : chaque mention prolonge le bloc,
        # sinon l'identifiant du second tombe juste après la fenêtre du premier.
        if spans and start <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans, sirens


def without_third_parties(text):
    """Retire les blocs hébergeur / prestataire / agence web (identifiants d'un tiers)."""
    kept, position = [], 0
    for start, end in third_party_spans(text)[0]:
        kept += [text[position:start], " "]
        position = end
    return "".join([*kept, text[position:]])


def own_identifier(siren, text):
    """SIREN, SIRET (14 chiffres) ou TVA de l'entreprise publiés comme nombres complets."""
    if not re.fullmatch(r"\d{9}", siren or ""):
        return "", ""
    core = f"{siren[:3]}{SEP}{siren[3:6]}{SEP}{siren[6:]}"
    tva = tva_number(siren)
    found = re.search(rf"(?<![A-Za-z0-9])FR{SEP}{tva[2:4]}{SEP}{core}(?!{SEP}\d)", text, re.I)
    if found:
        return "tva", excerpt(text, found.start(), found.end())
    found = re.search(rf"(?<![\d]){core}(?:{SEP}\d{{5}})?(?!{SEP}\d)", text)
    if found:
        return "siren", excerpt(text, found.start(), found.end())
    return "", ""


def other_identifiers(siren, text):
    """Autres SIREN publiés comme identifiants légaux, hors blocs hébergeur/prestataire."""
    from .verification import legal_identifiers

    # Le SIREN d'un prestataire répété ailleurs sur la page (pied de page) reste celui d'un tiers.
    providers = third_party_spans(text)[1]
    return legal_identifiers(separated(without_third_parties(text))) - {siren} - providers


def street_tokens(address):
    """Mots distinctifs de la voie (« 48 RUE DE L'ESCALET 13013 MARSEILLE » → escalet) + CP."""
    plain = normalized(address)
    postal = re.search(r"\b(\d{5})\b", plain)
    before = plain[: postal.start()] if postal else plain
    words = [w for w in re.findall(r"[a-z]{3,}", before) if w not in VOIE]
    return words, postal.group(1) if postal else ""


def street_number(address):
    found = re.match(r"\s*(\d{1,4})\b", normalized(address))
    return found.group(1) if found else ""


def address_found(address, plain, window=220):
    """Adresse complète (numéro, mots de voie, code postal) réunie dans un même passage."""
    words, postal = street_tokens(address)
    if not postal or not words:
        return ""
    number = street_number(address)
    for match in re.finditer(rf"\b{postal}\b", plain):
        passage = plain[max(0, match.start() - window) : match.end() + 40]
        if all(re.search(rf"\b{w}\b", passage) for w in words) and (
            not number or re.search(rf"\b{number}\b", passage)
        ):
            return passage[-180:]
    return ""


def profile_from_registry(item, company):
    """Profil d'identité tiré d'une fiche du registre public (recherche-entreprises)."""
    siege = item.get("siege") or {}
    people = []
    for person in item.get("dirigeants") or []:
        first = str(person.get("prenoms") or "").split(" ")[0]
        last = str(person.get("nom") or "")
        if first and last:
            people.append(f"{first} {last}")
    names = [item.get("nom_raison_sociale"), item.get("nom_complet"), item.get("sigle")]
    names += list(siege.get("liste_enseignes") or [])
    names = [re.sub(r"\([^)]*\)", "", str(n)).strip() for n in names if isinstance(n, str) and n]
    addresses = [siege.get("adresse", "")] + [
        e.get("adresse", "") for e in item.get("matching_etablissements") or []
    ]
    return dict(
        siren=str(item.get("siren", "")),
        names=list(dict.fromkeys(n for n in names if n)),
        sigle=str(item.get("sigle") or ""),
        people=list(dict.fromkeys(people))[:12],
        addresses=list(dict.fromkeys(a for a in addresses if a)),
        commune=str(siege.get("libelle_commune") or ""),
        postal=str(siege.get("code_postal") or ""),
    )


def excerpt(text, start, end, width=70):
    return text[max(0, start - width) : end + width].strip()


def _search(pattern, text):
    found = re.search(pattern, text)
    return excerpt(text, found.start(), found.end()) if found else ""


def site_label(url):
    """Libellé du domaine enregistrable (``acme`` pour www.acme.fr ou jobs.acme.fr)."""
    return re.sub(r"[^a-z0-9]", "", domain_key(url).split(".")[0])


def same_site(a, b):
    domaine = domain_key(a)
    return bool(domaine) and domaine == domain_key(b)


def domain_signal(company, profile, root):
    label = site_label(root)
    stems = set()
    for name in [company.name, *profile.get("names", [])]:
        words = [w for w in name_words(name) if len(w) >= 2]
        if words:
            stems.add("".join(words))
    if profile.get("sigle"):
        stems.add(re.sub(r"[^a-z0-9]", "", normalized(profile["sigle"])))
    # Nom complet, forme juridique comprise (CMR GROUP → cmrgroup) : égalité stricte seulement.
    full = {
        re.sub(r"[^a-z0-9]", "", normalized(n)) for n in [company.name, *profile.get("names", [])]
    }
    if label and (label in stems or label in full):
        return "domain"
    brand = brand_word(company)
    if label and brand and (label == brand or label.startswith(brand)):
        return "brand"
    if len(label) >= 4 and any(
        len(s) >= 4 and (label.startswith(s) or s.startswith(label)) for s in stems
    ):
        return "domain_partial"
    return ""


# Extensions nationales acceptées sans réserve pour une entreprise française : France et
# outre-mer, Union européenne, et extensions nationales employées comme génériques.
HOME_TLDS = {
    "fr", "re", "gp", "mq", "gf", "yt", "pm", "nc", "pf", "wf", "tf", "bl", "mf",
    "eu", "io", "co", "ai", "me", "tv", "cc",
}  # fmt: skip
# Indices qui rattachent un site à l'établissement français malgré une extension étrangère.
LOCAL_CODES = {
    "siren", "tva", "reference_siren", "reference", "locality", "postal", "address",
    "legal_address", "people", "legal_people",
}  # fmt: skip
URL_CODES = {"domain", "domain_partial", "brand", "foreign_tld"}


def foreign_tld(root):
    """Extension nationale d'un autre pays (cmr.it, acme.de) ; les génériques ne comptent pas."""
    tld = (root.split("//")[-1].split("/")[0]).lower().rstrip(".").rsplit(".", 1)[-1]
    return len(tld) == 2 and tld.isalpha() and tld not in HOME_TLDS


def site_signals(company, profile, root):
    """Indices tirés de l'adresse du site seule (domaine, marque, extension étrangère)."""
    signals = []
    domain = domain_signal(company, profile, root)
    if domain == "domain":
        signals.append(dict(code="domain", label="Domaine identique au nom, sigle ou enseigne"))
    elif domain == "domain_partial":
        signals.append(dict(code="domain_partial", label="Domaine proche du nom"))
    elif domain == "brand":
        signals.append(dict(code="brand", label="Domaine de la marque/du groupe"))
    if foreign_tld(root):
        signals.append(dict(code="foreign_tld", label="Extension nationale d'un autre pays"))
    return [dict(s, url=root, excerpt="") for s in signals]


def rejudge(company, profile, candidate):
    """(niveau, fondement, indices) d'un candidat déjà examiné, selon les règles actuelles : les
    indices lus sur les pages sont conservés, ceux tirés de l'adresse sont recalculés."""
    signals = [s for s in candidate.get("signals", []) if s.get("code") not in URL_CODES]
    signals += site_signals(company, profile, candidate["url"])
    level, basis = verdict(signals)
    return level, basis, signals


def collect_signals(company, profile, pages, root, references=(), *, full_excerpt=False):
    """Indices d'identité, chacun avec source et extrait. ``pages`` : [(kind, Page, texte)]."""
    signals = []

    def add(code, label, url, quote=""):
        if not any(s["code"] == code for s in signals):
            signals.append(dict(code=code, label=label, url=url,
                                excerpt=quote if full_excerpt else quote[:180]))

    siren = company.siren or company.siret[:9]
    names = [
        n
        for n in dict.fromkeys([company.name, *profile.get("names", [])])
        if len(normalized(n)) >= 3
    ]
    for kind, page, text in pages:
        plain = normalized(text)
        legal = kind == "légal" and bool(LEGAL_WORDS.search(text))
        clean = without_third_parties(text)
        code, quote = own_identifier(siren, clean)
        others = other_identifiers(siren, text) if siren else set()
        if code:
            label = (
                f"N° TVA intracommunautaire {tva_number(siren)} publié"
                if code == "tva"
                else "SIREN/SIRET de l'entreprise publié"
            )
            add(code, label + (" (mentions légales)" if legal else ""), page.url, quote)
            if legal:
                add("id_on_legal", "Identifiant publié sur une page de mentions légales", page.url)
            if others:
                add(
                    "multiple_ids",
                    f"Autre(s) identifiant(s) sur la même page ({', '.join(sorted(others))})",
                    page.url,
                )
        conflict = siren_contradicted(company, separated(clean)) if siren else set()
        if conflict and not code:
            add("conflict", f"SIREN publié différent ({sorted(conflict)[0]})", page.url)
        for name in names:
            found = re.search(rf"\b{re.escape(normalized(name))}\b", plain)
            if found:
                quote = excerpt(plain, found.start(), found.end())
                add("name", f"Nom « {name} » publié", page.url, quote)
                if legal:
                    add("legal_notice", f"Mentions légales nommant « {name} »", page.url, quote)
                break
        for address in profile.get("addresses", []):
            passage = address_found(address, normalized(clean))
            if passage:
                words, postal = street_tokens(address)
                add(
                    "address",
                    f"Adresse enregistrée publiée ({street_number(address)} {' '.join(words)} {postal})",
                    page.url,
                    passage,
                )
                if legal:
                    add("legal_address", "Adresse enregistrée dans les mentions légales", page.url)
        for person in profile.get("people", []):
            first, last = normalized(person).split(" ", 1)
            for found in re.finditer(
                rf"\b{first}\b\W+(?:\w+\W+)?{last}\b|\b{last}\b\W+{first}\b", plain
            ):
                around = plain[max(0, found.start() - 80) : found.end() + 80]
                if OFFICER_ROLE.search(around):
                    add(
                        "people",
                        f"Dirigeant enregistré cité dans sa fonction ({person})",
                        page.url,
                        around,
                    )
                    if legal:
                        add("legal_people", "Dirigeant cité dans les mentions légales", page.url)
                    break
        place = (
            normalized(profile.get("commune") or "")
            or normalized(re.sub(r"[\d,]", " ", company.city or "")).split(" ")[-1]
        )
        postal = profile.get("postal", "")
        if place and re.search(rf"\b{re.escape(place)}\b", plain):
            add(
                "locality",
                f"Localité « {place} » publiée",
                page.url,
                _search(rf"\b{re.escape(place)}\b", plain),
            )
        if postal and re.search(rf"\b{postal}\b", plain):
            add("postal", f"Code postal {postal} publié", page.url, _search(postal, plain))
    for signal in site_signals(company, profile, root):
        add(signal["code"], signal["label"], root)
    has_name = "name" in {s["code"] for s in signals}
    for ref in references:
        if not same_site(ref["url"], root):
            continue  # une référence ne vaut que pour le site qu'elle désigne
        if ref["kind"] == "wikidata_siren" and has_name:
            add(
                "reference_siren", "Site déclaré pour ce SIREN (Wikidata) et nom publié", ref["url"]
            )
        elif ref["kind"] == "encyclopedia":
            add("reference", "Site cité par l'article encyclopédique de l'entreprise", ref["url"])
    return signals


def reference_confirms(codes):
    """Une référence Wikidata désigne un site officiel de l'entité (ou de son groupe mondial) : elle ne
    confirme le site que s'il porte aussi un indice propre à l'établissement français, ou exactement son
    nom. Vu en réel : le site de la filiale américaine déclaré sur la fiche Wikidata du groupe."""
    return ("reference_siren" in codes and "conflict" not in codes
            and bool(codes & (LOCAL_CODES - {"reference", "reference_siren"}) or "domain" in codes))


def verdict(signals):
    """(niveau, fondement) : règles explicites, jamais une addition opaque de points.

    Un identifiant publié ne confirme que s'il est attribué au site : domaine cohérent, référence
    SIREN de ce site, ou mentions légales effectives. Aucun identifiant concurrent ne doit
    apparaître sur la même page ni sur une autre page du site."""
    codes = {s["code"] for s in signals}
    identifier = codes & {"siren", "tva"}
    owned = bool(codes & {"domain", "domain_partial", "brand", "reference_siren"}) or (
        "id_on_legal" in codes and "legal_notice" in codes
    )
    # Un SIREN concurrent sur une autre page interdit aussi la confirmation du site.
    if identifier and not codes & {"multiple_ids", "conflict"} and owned:
        basis = next(s["label"] for s in signals if s["code"] in identifier)
        return "confirme", f"identifiant légal : {basis}"
    if (identifier and "multiple_ids" in codes and "conflict" not in codes
            and {"id_on_legal", "legal_notice", "domain"} <= codes):
        # Page légale décrivant plusieurs sociétés (éditeur et filiales, société de services associée) :
        # l'entreprise y figure avec son propre identifiant ET le domaine porte exactement son nom.
        # Un domaine de marque partagé (« brand ») ne suffit pas : la filiale reste à vérifier.
        return "confirme", ("identifiant légal : mentions légales multi-entités nommant l'entreprise, "
                            "domaine à son nom")
    if reference_confirms(codes):
        return "confirme", "référence : site déclaré pour ce SIREN (Wikidata), nom publié"
    if identifier:
        reason = (
            "plusieurs identifiants sur la page"
            if "multiple_ids" in codes
            else "identifiant concurrent sur une autre page" if "conflict" in codes
            else "attribution du site non établie (domaine sans rapport, hors mentions légales)"
        )
        return "probable", f"identifiant publié mais {reason}"
    if "conflict" in codes:
        if codes & {"brand", "domain", "domain_partial"}:
            return "groupe", "SIREN d'une autre entité sur un domaine de la marque : site du groupe"
        return "non_fiable", "SIREN publié d'une autre entité (homonyme)"
    if "name" not in codes:
        if "brand" in codes:
            return "groupe", "domaine de la marque, nom exact de l'entité absent"
        return "non_fiable", "nom de l'entreprise absent des pages lues"
    if "foreign_tld" in codes and not codes & (LOCAL_CODES | {"brand"}):
        # CMR GROUP (Marseille) ≠ cmr.it (Italie) : même nom, aucun indice de l'établissement.
        return "non_fiable", "domaine national étranger sans indice local (homonyme probable)"
    if "legal_notice" in codes and codes & {"legal_address", "legal_people"} and "domain" in codes:
        found = sorted(codes & {"legal_address", "legal_people"})
        return "confirme", "faisceau : mentions légales + " + " + ".join(found) + " + domaine"
    entity = codes & {"address", "people"}
    # Un domaine proche du nom ne prouve rien seul : il faut un indice de l'établissement
    # (localité, code postal, adresse, dirigeant) ou une référence externe (Wikidata).
    domain_backed = "domain" in codes and codes & {"locality", "postal"}
    if (entity or domain_backed or codes & {"reference", "reference_siren"}
            or {"legal_notice", "postal"} <= codes):
        return "probable", "nom + " + ", ".join(sorted(codes - {"name"}))
    if "domain" in codes:
        # Nom publié + domaine proche, rien de l'établissement : jamais « probable » ni confirmé ;
        # conservé comme site à vérifier (aucun contact exploitable).
        return "groupe", "domaine proche du nom, sans indice propre à l'établissement"
    if "brand" in codes:
        return "groupe", "domaine de la marque, sans indice propre à l'entité"
    if "domain_partial" in codes and codes & {"locality", "postal"}:
        return "probable", "nom + domaine proche + localisation"
    return "non_fiable", "nom seul ou indices trop faibles (" + ", ".join(sorted(codes)) + ")"


def rank_key(level, signals):
    return (LEVELS.index(level), -len({s["code"] for s in signals}))


def third_party_text(text, company):
    """Bloc prestataire/hébergeur/agence web qui ne nomme pas l'entreprise."""
    return bool(
        re.search(THIRD_PARTY, text, re.I)
        and not re.search(rf"\b{re.escape(normalized(company.name))}\b", normalized(text))
    )


def classify_contact(contact, page_kind):
    """Type (email/telephone) et portée (recrutement/general/autre) d'une coordonnée publiée."""
    kind = "email" if contact.get("email") else "telephone" if contact.get("phone") else "personne"
    relevance = contact.get("relevance") or contact_relevance(contact)
    if relevance == "rh":
        scope = "recrutement"
    elif page_kind == "recrutement" and relevance in ("general", "management", "direction"):
        scope = "general (page recrutement)"
    elif relevance == "autre":
        scope = "autre (" + (contact.get("role") or "service non RH") + ")"
    else:
        scope = "general"
    return kind, scope
