# Two-week discovery and traffic plan

This is an experiment plan, not a traffic forecast. The drafts below have not been posted. Confirm deployment before sharing new guide or sitemap URLs.

## Positioning to test

**Find researchers at your university connected to the authors you read.**

Keep Six Degrees of Academia as the product name. Lead with a task people recognize: add authors from papers you like, select your university, then inspect local researchers and the publications connecting them. This works even for students who have not published.

This positioning is a hypothesis. [ResearchRabbit](https://www.researchrabbit.ai/) and [Litmaps](https://www.litmaps.com/about/for-researchers) already emphasize literature discovery and reviews; people and publication-backed connections give this tool a more specific starting point.

| Audience | Useful task | First distribution experiment |
| --- | --- | --- |
| Students entering research or a new PhD program | Turn a few favorite paper authors into a shortlist of campus researchers to investigate | Five observed trials through existing campus contacts |
| Researchers preparing for a seminar or conference | Explore coauthor connections between their adviser/themselves and a speaker; inspect the connecting publications | One short demo in a relevant lab or student society |
| Librarians and research-methods instructors | Teach the difference between coauthorship, citation, and institution networks | Offer one ten-minute classroom or workshop exercise |

Libraries already curate comparable [visual research tools](https://library.harvard.edu/services-tools/open-knowledge-maps) and offer [network visualization teaching](https://library.harvard.edu/services-tools/visualization-support). That supports testing this channel; it is not an endorsement of this app.

## Days 1–3: make discovery measurable

1. Set `ANALYTICS_ADMIN_TOKEN` in Render and open the private `/analytics.html` dashboard. Confirm durable PostgreSQL storage is configured. Collection can run without the token, but reports cannot be accessed. Record the baseline before outreach.
2. Confirm the deployed tool serves [the guide](https://academic-degree-of-separation.onrender.com/guide/) and [sitemap](https://academic-degree-of-separation.onrender.com/sitemap.xml).
3. Update the separate [xingruic.net wrapper page](https://www.xingruic.net/tools/researchers). It already has metadata; its next improvement is visible use-case copy and a normal HTML link to the guide, outside the iframe. This wrapper is outside this repository. Suggested copy: “Find researchers at your university connected to the authors you read. Explore the publication evidence, then investigate the people and research that interest you.” Link text: “How to find researchers and explore coauthor connections.”
4. In Google Search Console, verify the properties covering both hosts. Use a `xingruic.net` domain property if DNS access is available, or the appropriate HTTPS URL-prefix property; use a separate `https://academic-degree-of-separation.onrender.com/` URL-prefix property for the tool. Submit the tool sitemap in its property and the wrapper site's own sitemap in its property, ensuring it includes the wrapper page. [Google's property guide](https://support.google.com/webmasters/answer/34592).
5. Use [URL Inspection](https://support.google.com/webmasters/answer/9012289) on the wrapper, tool, and guide: check fetchability, rendered content, and Google's selected canonical. Request indexing after deployment when appropriate. Account verification, sitemap submission, and inspection require the owner's Search Console access; they have not been performed here. Indexing and rankings are not guaranteed.

The tool and guide use their own Render URLs as canonicals; the wrapper is a separate introduction. Any future domain move should deliberately consolidate pages and redirects rather than add unrelated cross-domain canonicals.

## Days 4–7: test usefulness

Watch five students use authors from papers they actually read. Ask them to find one campus researcher worth investigating, inspect a supporting publication, and explain why that person seems relevant. Record completion, confusion, and whether they would return. Five is a proposed pilot size, not a statistically representative sample.

Fix the largest repeated obstacle. Record one demo using a verified example from the pilot, with permission if it includes a participant's work. Do not infer personal relationships or willingness to collaborate from graph paths.

### Draft 45-second demo

- **0–7 seconds:** “Know the papers you like, but not who to talk to on campus? Start with their authors.”
- **7–17:** Search for two authors; show how publications distinguish people with similar names.
- **17–28:** Select a university in Institution Explorer and run the search.
- **28–38:** Open one candidate, inspect topics and a publication supporting a coauthor connection. “This is a lead to investigate, not a recommendation or proof that these people know each other.”
- **38–45:** “Try it with authors you already read.” Show the guide and tool URL.

## Days 8–14: try two focused channels

Have the owner share the demo with one relevant campus group or librarian, then separately with the [OpenAlex Community](https://groups.google.com/g/openalex-community), after reviewing its posting rules. Keep dates separate and note them alongside daily activity. Ask for specific feedback, not generic promotion. Do not buy ads yet.

**Campus draft:** “Looking for research opportunities? I built Six Degrees of Academia to explore researchers through their publications. Add authors whose work you like, select your university, and investigate campus researchers connected through coauthorship. I'd love feedback on whether it helps you find someone worth reading about. [Walkthrough](https://academic-degree-of-separation.onrender.com/guide/).”

**OpenAlex draft:** “I built a researcher graph using OpenAlex and made the source code public, including an Institution Explorer that connects seed authors to researchers at a selected university through publication-backed coauthor paths. I'm testing whether this helps students discover local research. Feedback on usefulness and author matching would be especially helpful. [Guide](https://academic-degree-of-separation.onrender.com/guide/) · [Source](https://github.com/riptideiv/academic-degree-of-separation).”

## Decide what to repeat

Rough activation rate = **searching browsers / unique browsers × 100**, over the same period; if the denominator is zero, report “not available.” Also compare manual search, graph-run, and explorer-run counts with the baseline.

These estimate browsers, not people. Runs include retries and repeated attempts; they are not successful connections. The guide intentionally has no tracker, so guide visits and guide-to-app conversions are unmeasured. Search Console can report organic impressions and clicks, not the complete on-site funnel. Current analytics do not establish channel attribution, completed-result funnels, export conversions, or retention. Timing alone cannot prove which outreach caused activity. Combine counts with observed task completion and feedback; repeat the channel that brings users with a concrete task. Later, consider result-outcome and export/share events without collecting search terms, plus reproducible graph links.
