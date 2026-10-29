# History quality and duplicate observations

## Preservation

Pre-change snapshot: `.deploy/snapshots/pre-history-dedup-20261029/`.
The ZIP preserves published reports, original HTML and private runtime metadata.
`raw-database-hashes.json` records the original news/RSS files before the
offline transformation. Normal scheduled collection may subsequently update
today's database; the migration itself never writes news or RSS databases.

`filter_report_history.py` parses published HTML, removes title-level
peripheral items, repairs category counts/navigation and numbering, removes
empty categories, and leaves scripts/styles and original stored reports intact.
Every publish and preview build reapplies it, so syncing old source HTML cannot
undo the quality policy. Search, recommendations, events and trends filter
the same titles while building their display data.

Historical AI scores and podcasts are not regenerated. They cost API calls
and cannot be faithfully corrected simply by deleting unrelated sentences.
Changed HTML indicates that its AI analysis/audio still reflects its original
generation. Audio remains governed by the existing 90-day policy.

## Definitions

- Collection duplicate rate: observations already seen in an earlier batch
  within their source, or a title repeated in this batch, divided by all hotlist
  observations in this batch. One observation is counted at most once.
- Cross-source duplicates: extra observations of exactly the same normalized
  title from a different source within the batch; not a count of semantic events.
- Semantic candidate pairs: long titles with a shared prefix and high character
  similarity. Observation only: these do not suppress delivery.
- Delivered-repeat blocks: candidates rejected using the success ledger for the
  specific Feishu destination. They exclude repeated regions of the same card.

The collection metrics cover the main hotlist stream; RSS has its existing
source/GUID storage deduplication and participates in the Feishu delivery gate.
Metrics include timestamps. Missing measurements are unknown, not zero.

## Delivery behavior

The Feishu ledger records titles only after all batches for that destination
confirm success. Incremental messages suppress exact normalized-title repeats
across sources and past deliveries. Daily summaries deliberately remain complete
and also record confirmed delivery. New title text, versions, figures and
conclusions remain eligible. Semantic similarity never blocks a message.

The ledger starts when this change is enabled. Previous collection does not
prove successful delivery, so past seen-items are not imported as delivered.
After a partially delivered multi-batch failure, a retry can repeat a successful
earlier batch. A crash between a confirmed HTTP response and the ledger commit
can also repeat a message: this is at-least-once delivery, not exactly once.
The ledger is private (`TrendRadar/output/delivery_ledger.db`); group addresses
are hashed, never published. The health-report webhook is a separate channel.
