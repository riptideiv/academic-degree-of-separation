# Discovery evaluation — 2026-10-29

Local application requests against live OpenAlex data. These are spot checks, not a relevance benchmark or a load test. No student usability study has been conducted.

| Query | Results | Publications checked | Observed response | Examples |
| --- | --- | --- | --- | --- |
| Oxford bacterial genetics | 20 | 15 | 2.27 s | [Benjamin F Cooper](https://openalex.org/A5047398308), [Georgia L. Isom](https://openalex.org/A5050856039) |
| Harvard labor markets | 17 | 15 | 1.21 s | [George J. Borjas](https://openalex.org/A5003739742), [Daniel Schneider](https://openalex.org/A5074607822) |
| MIT transformer paper | 20 | 37 | 1.87 s | [James A. Michaelov](https://openalex.org/A5054612798), [Roger Lévy](https://openalex.org/A5090215557) |
| MIT Hinton reference | 20 | 44 | 1.38 s | [Jeff Gore](https://openalex.org/A5003202779), [Philippe Rigollet](https://openalex.org/A5001421646) |

## Evidence reviewed

- **Oxford bacterial genetics:** Benjamin F Cooper — [Lipid-associated architecture and divergent ammonium transport distinguish a bacterial Rhesus protein](https://openalex.org/W7213536819). The publication carries the selected institution on the author’s own authorship and matches at least one selected/derived topic.
- **Harvard labor markets:** George J. Borjas — [The mismeasurement of work time: Implications for wage differentials and inequality](https://openalex.org/W7161979563). The publication carries the selected institution on the author’s own authorship and matches at least one selected/derived topic.
- **MIT transformer paper:** James A. Michaelov — [Target-Language Generation in Multilingual Models: Activation Steering and Optimal Control](https://openalex.org/W7213403805). The publication carries the selected institution on the author’s own authorship and matches at least one selected/derived topic.
- **MIT Hinton reference:** Jeff Gore — [Optimizer-dependent training dynamics converge to the same one-third optimal data scaling](https://openalex.org/W7214910994). The publication carries the selected institution on the author’s own authorship and matches at least one selected/derived topic.

## Findings and changes

- MIT robotics was also exercised through the browser: selecting a topic and university produced 20 researchers from 15 sampled publications without a graph prerequisite.
- A card-level check from Chuchu Fan to Sanjit Arunkumar Seshia returned two publication-backed hops through Jyotirmoy V. Deshmukh. The discovery list remained intact.
- Oxford data included unresolved authorships with null author IDs. These now skip safely, covered by a regression test.
- The initial economics sample included replication datasets. Retrieval now restricts the initial sample to articles, preprints, reviews, books, book chapters, and dissertations.
- Results use recent publications rather than citation-count sorting. Citation fame does not determine eligibility or ordering.

## Limits

- The sample covers at most eight topics, 15 recent works per topic, and 80 author profiles; results show at most 20 people. These bounds omit relevant people.
- Topic classification is broad. A selected robotics-related reinforcement-learning topic can also match general machine-learning work; the actual paper is shown so the visitor can judge fit.
- OpenAlex author identity, dates, and institutional metadata can be wrong. A listed affiliation remains explicitly last-known unless separately reviewed.
- Recent preprints can dominate fast-moving fields. A later usability study should compare this with a publication-date filter or wider sample.
- These few requests do not establish production latency, quota capacity, or an unbiased recommendation ranking.

## Automated coverage

Backend checks cover topic/paper inputs, exact authorship affiliation, former affiliations, reviewed identity work scopes, malformed/null records, invalid requests, partial source failures, cancellation, and connection-status semantics. Frontend tests cover independent references, stale response ordering, cancellation, and preservation of earlier results after refresh failure.
