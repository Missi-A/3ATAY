"""Déduplication des observations nouvelles sans remplacer les contacts fournis."""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .pages import canonical
from .verification import CONFIDENCES, RELEVANCES, contact_relevance, normalize_phone, normalized


def priority(contact):
    """Pertinence d'abord ; à pertinence égale, une personne nommée avant une boîte générique."""
    relevance = contact.get("relevance")
    rank = RELEVANCES.index(relevance if relevance in RELEVANCES else contact_relevance(contact))
    return rank * 2 + (1 if contact.get("name") else 2)


def confidence_order(contact):
    confidence = contact.get("confidence")
    if contact.get("reliability") == "reliable":
        return 0
    return CONFIDENCES.index(confidence) if confidence in ("probable", "incertain") else 2


def same_identity(contact, old):
    """Aucune adresse ni aucun nom divergent : un même numéro peut alors fusionner."""
    email, old_email = contact.get("email", "").lower(), old.get("email", "").lower()
    name, old_name = normalized(contact.get("name", "")), normalized(old.get("name", ""))
    return (not email or not old_email or email == old_email) and (
        not name or not old_name or name == old_name
    )


def deduplicate_contacts(contacts, existing=()):
    existing_emails = {str(c.get("email", "")).strip().lower() for c in existing if c.get("email")}
    existing_people = {
        normalized(c.get("name") or c.get("person") or "")
        for c in existing
        if c.get("name") or c.get("person")
    }
    existing_phones = {p for c in existing if (p := normalize_phone(c.get("phone", "")))}
    result = []
    for contact in sorted(contacts, key=confidence_order):
        contact = dict(contact)
        phone = contact["phone"] = normalize_phone(contact.get("phone", ""))
        email, name = contact.get("email", "").lower(), normalized(contact.get("name", ""))
        if (
            email
            and email in existing_emails
            or name
            and name in existing_people
            or phone
            and phone in existing_phones
            and not email
            and not name
        ):
            continue
        duplicate = next(
            (
                old
                for old in result
                if email
                and old.get("email", "").lower() == email
                or phone
                and old.get("phone") == phone
                and same_identity(contact, old)
                or name
                and normalized(old.get("name", "")) == name
                and (not email or not old.get("email") or email == old.get("email", "").lower())
            ),
            None,
        )
        if duplicate:
            duplicate["source_urls"] = list(
                dict.fromkeys(
                    duplicate.get("source_urls", [duplicate["source_url"]])
                    + contact.get("source_urls", [contact["source_url"]])
                )
            )
            duplicate["evidence"] = list(
                dict.fromkeys(duplicate.get("evidence", []) + contact.get("evidence", []))
            )
            for key in ("name", "role", "email", "phone", "phone_type", "profile_url"):
                if key in ("email", "phone") and confidence_order(contact) > confidence_order(
                    duplicate
                ):
                    continue  # Une preuve sur le nom ne confirme pas une autre coordonnée.
                if not duplicate.get(key) and contact.get(key):
                    duplicate[key] = contact[key]
                    if key == "email":
                        duplicate["source_url"] = contact["source_url"]
            if contact.get("reliability") == "reliable":
                duplicate["reliability"] = "reliable"
                duplicate["association_verified"] = True
        else:
            result.append(dict(contact))
    for contact in result:
        if contact.get("relevance") not in RELEVANCES:
            contact["relevance"] = contact_relevance(contact)
        contact["priority"] = priority(contact)
    return sorted(
        result,
        key=lambda c: (confidence_order(c), c["priority"], c.get("name", ""), c.get("email", "")),
    )


def deduplicate_offers(offers):
    result = []
    for offer in offers:
        duplicate = next(
            (
                old
                for old in result
                if old["company_id"] == offer["company_id"]
                and (
                    (
                        offer_url(old["url"]) == offer_url(offer["url"])
                        and not (
                            old.get("external_id")
                            and offer.get("external_id")
                            and old["external_id"] != offer["external_id"]
                        )
                    )
                    or (
                        old.get("external_id")
                        and old.get("external_id") == offer.get("external_id")
                        and old.get("id_namespace")
                        and old.get("id_namespace") == offer.get("id_namespace")
                    )
                )
            ),
            None,
        )
        if duplicate:
            duplicate["source_urls"] = list(
                dict.fromkeys(
                    duplicate.get("source_urls", [duplicate["source_url"]])
                    + offer.get("source_urls", [offer["source_url"]])
                )
            )
            evidence = list(
                dict.fromkeys(duplicate.get("evidence", []) + offer.get("evidence", []))
            )
            more_precise = (
                offer.get("employer_source") == "posting"
                and duplicate.get("employer_source") == "board_publisher"
            )
            methods = set(duplicate.get("discovery_methods", [])) | set(
                offer.get("discovery_methods", [])
            )
            methods.update(
                v for v in (duplicate.get("discovery_method"), offer.get("discovery_method")) if v
            )
            if (
                more_precise
                or offer["reliability"] == "reliable"
                and not (
                    duplicate.get("employer_source") == "posting"
                    and offer.get("employer_source") == "board_publisher"
                )
            ):
                duplicate.update({k: v for k, v in offer.items() if k != "source_urls"})
            if methods:
                duplicate["discovery_methods"] = sorted(methods)
            duplicate["evidence"] = evidence
        else:
            result.append(dict(offer))
    return result


def offer_url(url):
    parts = urlsplit(canonical(url))
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in ("gclid", "fbclid")
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(sorted(query)), ""))
