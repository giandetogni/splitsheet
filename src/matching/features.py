"""Label-blind similarity features, defined once and emitted as both Python and SQL.

Every feature exists here twice on purpose: a pure Python function that the unit tests pin,
and a SQL expression generator used to compute it over 3,045,208 candidate pairs in
BigQuery. That is a real duplication risk, so it is handled the way the evaluation split
handles it -- both forms are produced from the same definition object, and an integration
test recomputes the SQL results in Python on real rows and asserts they agree. Two
implementations that are never compared is how drift happens; two that are compared on every
run is a check.

WHAT IS DELIBERATELY ABSENT, from the Phase 4B constraints:

  * the mapper reference label, in any form;
  * popularity (the canonical snapshot's `score` column is never read);
  * candidate order, row number, or anything else that would let position decide;
  * MBID lexical value, artist_credit_id, or any derived identifier;
  * duration and ISRC, which the canonical snapshot does not carry.

Features compare the FULL normalized Unicode text on both sides, never the ASCII lookup
key. The ASCII key is what discarded the information in the first place: for a
CJK-titled recording it can retain 5 characters out of 26, so scoring on it would score the
residue rather than the content.
"""

from __future__ import annotations

import unicodedata

FEATURE_VERSION = "1.0.0"

# Listen-side classification of how much content the ASCII lookup key kept. Phase 4
# preflight measured the < 0.20 band: 793 keys, 2,046 listens, but block cardinality up to
# 945 and roughly 1.4% of all candidate pairs. Unique agreement in that band was 100% with
# zero disagreement, so this is a cost and routing signal only -- no candidate is ever
# suppressed by it and it is never evidence in a score.
LOW_INFORMATION_RATIO_CUT = 0.20
NORMAL = "NORMAL"
LOW_INFORMATION = "POTENTIAL_LOW_INFORMATION"


# --- pure Python -------------------------------------------------------------------------

def _alnum_count(s: str) -> int:
    """Unicode alphanumerics, any script. Matches SQL's \\p{L}\\p{N} class."""
    return sum(1 for ch in s if unicodedata.category(ch)[0] in ("L", "N"))


def _ascii_alnum_count(s: str) -> int:
    return sum(1 for ch in s if ("a" <= ch <= "z") or ("0" <= ch <= "9"))


def ascii_retention_ratio(artist_unicode: str, recording_unicode: str,
                          lookup_key: str) -> float | None:
    """How much of the Unicode content survived into the ASCII key.

    NULL (None) when there is no Unicode alphanumeric content at all: a ratio over zero
    content is undefined, not zero, and such a listen produces no candidates anyway.
    """
    denom = _alnum_count(artist_unicode) + _alnum_count(recording_unicode)
    if denom == 0:
        return None
    return _ascii_alnum_count(lookup_key) / denom


def information_class(ratio: float | None) -> str:
    if ratio is None:
        return NORMAL
    return LOW_INFORMATION if ratio < LOW_INFORMATION_RATIO_CUT else NORMAL


def unicode_exact(left: str, right: str) -> bool:
    """Equality of normalized Unicode values. Two empty strings are NOT a match: absence of
    content is not evidence of agreement."""
    return bool(left) and left == right


def token_similarity(left: str, right: str) -> float:
    """Jaccard over whitespace-separated tokens.

    Order-insensitive on purpose: "beatles the" and "the beatles" are the same credit
    written two ways, and treating that as a difference would penalise a real match.
    """
    a, b = set(left.split()), set(right.split())
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def edit_distance(left: str, right: str) -> int:
    """Levenshtein over code points. Two rows of ints; no dependency added for this."""
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prev = list(range(len(right) + 1))
    for i, lc in enumerate(left, 1):
        cur = [i]
        for j, rc in enumerate(right, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (lc != rc)))
        prev = cur
    return prev[-1]


def string_similarity(left: str, right: str) -> float:
    """1 - editDistance/max(len). Zero when either side is empty."""
    if not left or not right:
        return 0.0
    return 1.0 - edit_distance(left, right) / max(len(left), len(right))


def features_for_pair(listen_artist: str, listen_recording: str,
                      canonical_artist: str, canonical_recording: str) -> dict:
    """The six scored features for one candidate pair, in one call."""
    return {
        "artist_unicode_exact": unicode_exact(listen_artist, canonical_artist),
        "recording_unicode_exact": unicode_exact(listen_recording, canonical_recording),
        "artist_token_similarity": token_similarity(listen_artist, canonical_artist),
        "recording_token_similarity": token_similarity(listen_recording, canonical_recording),
        "artist_string_similarity": string_similarity(listen_artist, canonical_artist),
        "recording_string_similarity": string_similarity(listen_recording, canonical_recording),
    }


# --- the same definitions as SQL ----------------------------------------------------------

def sql_unicode_exact(left: str, right: str) -> str:
    return f"({left} != '' AND {left} = {right})"


def sql_token_similarity(left: str, right: str) -> str:
    """Jaccard in SQL. Correlated subqueries over token arrays; no UDF, no JS."""
    return f"""(
      SELECT CASE WHEN ARRAY_LENGTH(la) = 0 OR ARRAY_LENGTH(ra) = 0 THEN 0.0
                  ELSE SAFE_DIVIDE(
                    (SELECT COUNT(*) FROM UNNEST(la) t WHERE t IN (SELECT * FROM UNNEST(ra))),
                    ARRAY_LENGTH(ARRAY(SELECT DISTINCT t FROM UNNEST(ARRAY_CONCAT(la, ra)) t)))
             END
      FROM (SELECT ARRAY(SELECT DISTINCT t FROM UNNEST(SPLIT({left}, ' ')) t WHERE t != '') AS la,
                   ARRAY(SELECT DISTINCT t FROM UNNEST(SPLIT({right}, ' ')) t WHERE t != '') AS ra)
    )"""


def sql_string_similarity(left: str, right: str) -> str:
    return (f"IF({left} = '' OR {right} = '', 0.0, "
            f"1.0 - SAFE_DIVIDE(EDIT_DISTANCE({left}, {right}), "
            f"GREATEST(CHAR_LENGTH({left}), CHAR_LENGTH({right}))))")


def sql_ascii_retention_ratio(artist_unicode: str, recording_unicode: str,
                              lookup_key: str) -> str:
    uni = (f"CHAR_LENGTH(REGEXP_REPLACE(CONCAT({artist_unicode}, {recording_unicode}), "
           r"r'[^\p{L}\p{N}]', ''))")
    asc = f"CHAR_LENGTH(REGEXP_REPLACE({lookup_key}, r'[^a-z0-9]', ''))"
    return f"SAFE_DIVIDE({asc}, NULLIF({uni}, 0))"


def sql_information_class(ratio_expr: str) -> str:
    return (f"IF({ratio_expr} IS NOT NULL AND {ratio_expr} < {LOW_INFORMATION_RATIO_CUT}, "
            f"'{LOW_INFORMATION}', '{NORMAL}')")


def sql_feature_columns(listen_alias: str = "l", canon_alias: str = "k") -> str:
    """The six scored features as a SELECT fragment, generated from the definitions above."""
    la = f"{listen_alias}.artist_normalized_unicode"
    lr = f"{listen_alias}.recording_normalized_unicode"
    ka = f"{canon_alias}.artist_normalized_unicode"
    kr = f"{canon_alias}.recording_normalized_unicode"
    return ",\n          ".join([
        f"{sql_unicode_exact(la, ka)} AS artist_unicode_exact",
        f"{sql_unicode_exact(lr, kr)} AS recording_unicode_exact",
        f"{sql_token_similarity(la, ka)} AS artist_token_similarity",
        f"{sql_token_similarity(lr, kr)} AS recording_token_similarity",
        f"{sql_string_similarity(la, ka)} AS artist_string_similarity",
        f"{sql_string_similarity(lr, kr)} AS recording_string_similarity",
    ])
