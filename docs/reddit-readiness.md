# Coauthor Distance: Reddit readiness

Updated for the app expansion, 2026-10-29. Audience fit remains a product hypothesis;
no student usability study or Reddit demand study has been completed.

## Audience and promise

Start with students exploring research and early-career researchers getting oriented
in a field. They can begin with a university and an interest rather than knowing two
author names. Science-curious visitors can still try the Hinton–Bengio graph example.

The promise is: **find people whose work interests you, then understand how they
connect.** Discover supplies publication-backed leads; Connections explains networks;
Saved keeps useful people, papers, and notes. The three destinations form one journey.

Keep the quiet interface: labels, controls, readable evidence, and details in the
guide. Avoid decorative metrics, generic helper descriptions, collaborator scores,
and implied recruitment or personal-introduction claims.

## Name and domain

The selected name is **Coauthor Distance**, with source repository
`riptideiv/coauthor-distance`. The user has registered a domain, but its exact hostname
has not been supplied in this task. The Render service and canonical hostname stay
unchanged until deployment configuration is known. Browser-local research requires
export/import when moving origins.

## Suggested post draft

**Title:** I built a free tool to find researchers at your university from topics and papers

**Body:**

I revived a side project called Coauthor Distance. Pick your university and a research
topic, paper, or author, and it suggests researchers with publications you can inspect.

You can open a profile, read recent work, explore coauthor connections, and save people
or papers with notes. The shortlist stays in your browser, with JSON export/import
and no account. There is also a graph example if you just want to explore.

It uses OpenAlex, so identity and affiliation records can be incomplete or outdated.
Discovery checks a bounded sample, and an unsuccessful connection search does not
prove that no path exists. It does not tell you whether someone is recruiting.

[Try it](https://academic-degree-of-separation.onrender.com/) ·
[Source](https://github.com/riptideiv/coauthor-distance)

I'd like feedback on whether you found someone worth reading about, whether the
supporting papers made the match clear, and which steps felt confusing.

## Before posting

Deploy the complete three-phase stack and verify the public deployment first; local
changes and draft PRs do not update the hosted app. Replace the post link only after
the registered domain works. Read the destination community's current rules before
posting and disclose that you made the tool. Posting eligibility has not been verified,
and no post has been submitted.

## Evidence and limits

- [Expansion design](app-expansion-design.md): user journeys, state boundaries, scope.
- [Implementation plan](app-expansion-plan.md): branch stack and verification gates.
- [Live discovery evaluation](discovery-evaluation.md): example queries and coverage limits.
- [Completion audit](app-expansion-audit.md): implementation and runtime evidence.

Live relevance checks are a small sample, not a general quality benchmark. Useful
results depend on OpenAlex coverage and correct identity records. Accounts, cloud
sync, recruitment information, and automated outreach remain outside this release.
