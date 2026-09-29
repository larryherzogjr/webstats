"""Optional application-layer activity suppression.

No configured site currently needs this extra boundary.  Ad Fontes is protected
at collection time by nginx's query-free, referrer-free log format and otherwise
participates in the authenticated dashboard like every other site.
"""

INDIVIDUAL_ACTIVITY_PRIVATE_SITES: tuple[str, ...] = ()
