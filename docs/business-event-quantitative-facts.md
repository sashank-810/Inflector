# Deterministic quantitative business-event facts

Phase 6C-B derives narrowly supported quantitative source observations from
already-persisted Phase 6C-A `BusinessEventEvidence`. It never searches beyond
an accepted evidence excerpt. This attribution boundary intentionally prefers
false negatives over associating an unrelated number with an event.

```text
PIT BusinessEvent
  -> persisted, revalidated BusinessEventEvidence excerpts
  -> business_event_quantitative_rules_v1
  -> immutable derivation envelope (including an empty result)
  -> zero or more exact quantitative fact observations
```

These observations are not totals, materiality measurements, catalyst scores,
directions, sentiment, confidence, or recommendations.

## Ruleset and fact vocabulary

The explicit ruleset identity is `business_event_quantitative_rules` /
`business_event_quantitative_rules_v1`; individual rules use
`business_event_quantitative_rule_v1` plus a stable descriptive rule code.
Changing interpretation requires a new semantic ruleset version. Recomputing
an existing event under the same version must reproduce the exact derivation
fingerprint or fail closed.

V1 supports exactly:

| Event type | Permitted fact codes |
|---|---|
| `order_award` | `order_value` |
| `capex_announcement` | `capex_value` |
| `capacity_expansion` | `capacity_before`, `capacity_after`, `additional_capacity` |
| `acquisition_agreement` | `acquisition_consideration`, `acquisition_stake_fraction` |
| `commercial_commencement` | `commercial_commencement_date` |
| `regulatory_approval` | none |

Fact kinds are exactly `monetary`, `capacity`, `fraction`, and `date`.
Incompatible event/fact mappings fail validation.

## Money

Money rules require a narrow grammatical relationship between the event and
the amount. Supported explicit currencies are INR (`₹`, `INR`, `Rs`, `Rs.`),
USD, and EUR. Supported scales are no scale/unit, thousand, lakh/lakhs/lac/lacs,
crore/crores/cr, million/mn, and billion/bn. Both western and Indian grouping
are validated before exact `Decimal` parsing. Malformed grouping is rejected.

`reported_value`, scale, and currency preserve the source representation as
structured fields. `normalized_value` applies the exact scale multiplier in
the same currency and uses `currency_major`; it never performs FX conversion.
Ranges and qualified amounts such as “approximately,” “about,” “around,” “up
to,” “more than,” or “less than” are intentionally unavailable in v1. An order
book, revenue, market capitalization, enterprise value, or target revenue is
not substituted for the event amount.

## Capacity

The v1 unit vocabulary is TPA, KTPA, MTPA, TPD, MW, GW, KLPD, and the exact
text equivalents tonnes/thousand tonnes/million tonnes per annum and tonnes
per day. Annual mass normalizes to `tonnes_per_annum`, power to `megawatt`,
daily mass to `tonnes_per_day`, and liquid capacity to
`kilolitres_per_day`. No daily/annual conversion is made.

Explicit `from … to …` pairs produce separate `capacity_before` and
`capacity_after` observations only when their normalized dimensions match.
`additional_capacity` is stored only when the disclosure explicitly states
it. The system never derives an additional amount by subtracting before from
after and never computes a growth ratio in this phase.

## Acquisitions and dates

Acquisition evidence can yield an explicit consideration under the money
contract and an explicit stake percentage. A valid stake is greater than zero
and no more than 100 percent; its exact normalized fraction is percentage / 100.
Share counts are not converted into a stake.

Commercial-commencement evidence supports only `YYYY-MM-DD`, `DD Month YYYY`,
`DD Mon YYYY`, `Month DD, YYYY`, and `Mon DD, YYYY`, using English month names.
Numeric slash dates, two-digit years, relative dates, and fiscal-quarter labels
are rejected. The date must be directly tied to commercial commencement.

## Persistence, fingerprints, and lineage

Migration 0014 adds `business_event_quantitative_derivations` and
`business_event_quantitative_facts`. Every processed event receives an
immutable derivation envelope, including when `facts = ()`; this distinguishes
“processed with no supported explicit quantity” from “not processed.” Exact
reruns reuse the same derivation and children.

Each fact retains its event-evidence ID, rule identity, absolute source offsets,
exact raw substring and SHA-256, reported and normalized representation, source
knowledge time, warnings, and canonical fingerprint. Headline and document
observations are not deduplicated across evidence rows and are never summed.
The derivation fingerprint covers the event detection fingerprint, ruleset,
ordered fact fingerprints, available fact codes, and warnings. Operational and
generated derivation/fact IDs are excluded from semantic fingerprints.

Before derivation, persisted event identity and the complete evidence set are
revalidated. Document evidence additionally revalidates document, acquired
asset, successful extraction, object hashes, canonical text offsets, page
hashes, and excerpt hash. `source_available_at` always comes from the
BusinessEvent/announcement. Event derivation time, quantitative `derived_at`,
document retrieval time, and extraction time remain operational metadata and
never replace knowledge time.

Corrections remain immutable. A corrected announcement may change a value,
remove the event, or retain the event while removing its explicit value. PIT
selection exposes only the corrected announcement revision and its own event
and derivation; no prior fact is carried forward.

## Deliberate boundaries

Phase 6C-B performs no financial-statement join, FX conversion, materiality or
capacity arithmetic, cross-announcement economic deduplication, ranking,
direction, sentiment, confidence, AI/LLM/NER extraction, OCR, embeddings, or
whole-document rescan. It adds no Business Catalyst policy or scorer, applies
no top-level 20% weight, creates no snapshot version, and changes no existing
snapshot. V1/V2/V3 maximum persisted coverage remains 0.25/0.60/0.75,
Business Catalyst and Low Market Attention remain absent, and `final_score`
remains null.
