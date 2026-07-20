# Changelog

All notable changes to `dataset-prober` are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to follow [Semantic Versioning](https://semver.org/).

Entries describe what changed **for someone using the tool**, not what was done
to the source. For the latter, read `git log`.

## [Unreleased]

### Added

- Interactive directory descent for autoindex sources. A source URL that serves
  a folder listing (Apache/nginx style, as used by RIVM and INSPIRE portals) is
  now walked before probing: pick a subfolder, descend, pick files. Chosen files
  flow into the existing probe and download pipeline.
- HTML landing-page guard. A URL that serves HTML is rejected before load, and a
  table whose contents look like markup is dropped after load. Redirect traps
  and portal landing pages now fail honestly instead of being stored as data.

### Changed

- The CSV dialect decision is now made once and shared by probing and
  downloading. European-dialect files (`;` delimited, `#` comment preamble,
  ragged rows) previously failed at probe time and never reached download.
  Clean comma-separated files keep DuckDB's inferred column types; only files
  that fail or mis-sniff fall back to all-VARCHAR, and are cast in SQL when
  analysed.
- Table names produced by the manual probe path no longer repeat the filename.
  `_url_identity` returns the URL hash only; readability comes from the title
  suffix. A file at `.../2026_05_NO2.csv` previously produced
  `t_2026_05_no2_csv_00175841_2026_05_no2_csv` and now produces
  `t_00175841_2026_05_no2_csv`.

  **Tables created before this change keep their old names and will not be
  found by the new scheme.** Drop and re-download them, or rename them with
  `ALTER TABLE ... RENAME TO ...`. Tables created by the CBS, CKAN, and Tavily
  adapters are unaffected — they key on catalogue IDs, not URLs.

### Fixed

- Mis-sniffed CSVs are detected reliably. The previous check looked only for
  generic `column0`, `column1` names; the common real-world failure is a file
  collapsing into a single column named after its first physical line, which
  went undetected.
- Directory listings are fetched once per level instead of twice.

## [0.1.0]

First working version, predating this changelog. Manual URL probing, agentic
discovery through source profiles, DuckDB loading behind explicit download
consent, source adapters for CBS, CKAN, and Tavily, and freshness, licence, and
budget checks.
