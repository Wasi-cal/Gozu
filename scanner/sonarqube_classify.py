"""
Classifying a raw api/issues/search result as security-relevant (a
vulnerability, or a hotspot migrated into the issues model).

`/api/hotspots/search` is NOT used anywhere in this codebase -
SonarSource deprecated it (16 June 2026) as part of merging Security
Hotspots into regular issues (fully rolled out as of 1 July 2026): a
hotspot-origin rule now raises either a VULNERABILITY-typed issue
(Standard Experience mode) or a SECURITY-impact issue (MQR mode), tagged
"former-hotspot" either way. Verified live against SonarQube Cloud's own
web API on 2026-09-03 (its webservices listing still shows hotspots/search
marked deprecatedSince "16 June, 2026"); community reports show it already
returning 410 on some SonarQube Server versions.

There's no reliable, uniform way to ask an instance "are you MQR or
Standard" - Server exposes GET /api/v2/clean-code-policy/mode (confirmed
live against a public Server instance), but that endpoint doesn't exist on
SonarQube Cloud at all (confirmed live: 404). Rather than special-case per
mode, every issue in the response always carries BOTH the legacy `type`
field AND the newer `impacts` array (confirmed live via SonarQube Cloud's
own documented response example) - so classification here is done
defensively per-issue from whichever fields are populated, not per-instance
mode. That's the one part of this migration current docs don't fully spell
out; the triple-check below is deliberately redundant for that reason.
"""

_SECURITY_IMPACT_QUALITY = "SECURITY"
FORMER_HOTSPOT_TAG = "former-hotspot"


def is_security_relevant(raw: dict) -> bool:
    """
    True if `raw` (one api/issues/search result) is a vulnerability or a
    (possibly migrated-from-hotspot) security-impacting issue. Three
    independent signals, ORed rather than trusting any single one: the
    legacy `type` field, the MQR-mode `impacts` array, and the
    "former-hotspot" tag SonarSource adds to every migrated hotspot -
    Sonar's own migration notes say a former hotspot lands on EITHER a
    VULNERABILITY type OR a SECURITY impact depending on mode, so relying
    on just one field would miss real findings for whichever mode a given
    project isn't in.
    """
    if raw.get("type") == "VULNERABILITY":
        return True
    if any(impact.get("softwareQuality") == _SECURITY_IMPACT_QUALITY for impact in raw.get("impacts", [])):
        return True
    return FORMER_HOTSPOT_TAG in raw.get("tags", [])
