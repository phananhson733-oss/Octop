# OpenSEO reconstruction provenance

Reference repository: https://github.com/every-app/open-seo
Pinned commit: d7ea7852480601af9bc8870942af8311a410e567
Copyright (c) 2026 Ben Senescu. MIT text is retained in UPSTREAM-LICENSE.txt.

The Python keyword normalization, intent mapping, source interleaving and Labs mapping are ports of helpers.ts and research-data.ts under src/server/features/keywords/services/research. Research blending/fallback follows research.ts and selection.ts. The frontend groupSharedVolumeRows.ts preserves the source algorithm and includes its notice. Report-writing guidance is adapted from plugins/openseo/skills/keyword-research and seo-report. Schema and API behavior are adapted to Octop; no OpenSEO server is used by the implementation.

The checked-in tests/fixtures/seo/openseo-reference.json is language-neutral fixture data based on source tests and additional source-contract cases. reference-result.json records executed source hashes and all explicit external substitutes. REA execution evidence ev_9ed43ef5c70697930e77e0ee8c8430388ce6277d7de13152096e96e7232ae55c observed 23 passing checks against9 unmodified source modules.

Intentional native differences: UI and Agent share native service contracts; saved counts describe actual normalized writes; raw provider cost retained without hosted1.28 markup; metrics preserve source observed_at; selected metrics are read from research rather than supplied by browser; tags saved transactionally; private reports are immutable rows rather than in-place updates; context hash prevents saved provenance from drifting while an Agent writes; local/city metrics and Ads fallback not yet supported. Report UTF8 size cap matches source, native title/summary length uses Unicode code points.

No authenticated OpenSEO website, real provider billing, real model report quality, or PostgreSQL runtime parity is claimed by the offline replay.
