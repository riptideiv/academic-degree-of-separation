# Coauthor Atlas: Reddit readiness

Decision date: 2026-10-28. These are product judgments, not evidence of audience demand.

## Who to target first

STEM students, graduate students, and early-career researchers who recognize their
professors and the authors they read. Science-curious readers are a secondary
curiosity audience. They have a concrete first question: “How is my professor
connected to this scientist?” A broad consumer audience often cannot name two
researchers; the one-click Hinton–Bengio example gives them a way in.

Lead with an enjoyable discovery, then offer the useful follow-up: explore people
at your university whose coauthor networks connect to the authors you read. Do not
position the app as a general social network, an authoritative collaborator ranking,
a complete literature-review tool, or a certified Erdős-number calculator.

## The five launch questions

1. **Audience:** people with a personal connection to research first; science-curious
   readers through the example second. There is no evidence yet that the app needs
   an account system, daily game, or retention mechanics.
2. **Actions and discovery:** the previous screen exposed researcher and paper search
   without explaining their relationship, opened advanced connection controls early,
   and hid useful exports at the bottom of the sidebar. The refresh adds visible
   labels, a next-step prompt, a map toolbar, a result banner that opens the evidence,
   campus onboarding, and a way to turn an inspected neighbor into a starting point.
   Connection and display settings remain available through disclosures.
3. **Name:** “Six Degrees of Academia” is already used by a closely related
   [coauthorship-map project](https://github.com/dyllan-to-you/Six-Degrees-Of-Academia).
   Use **Coauthor Atlas**. Exact-name web
   searches did not identify a competing Coauthor Atlas app during this review;
   domain availability and trademark status have not been established. The name
   reflects the default coauthor search while the guide explains broader modes.
   Keep the existing Render service name, repository slug, and canonical URL so
   this change does not create a new service or break existing links.
4. **Visual style:** use a quiet workspace with plain typography and controls.
   The starting screen contains a heading and actions; explanatory copy belongs in
   About or beside the specific setting that needs it. Omit decorative captions,
   numbered feature blocks, repeated branding, and disabled tools on the empty map.
   No frontend framework or build step is needed for this change.
5. **Engagement:** give a newcomer a visible result with one click, then let them
   inspect a publication, add a nearby researcher, try their own professor, discover
   campus researchers, and share the starting points. This is a credible short
   exploration loop; sustained engagement still needs feedback from actual users.

## Implemented behavior

- Fresh maps search coauthorship only and draw a few surrounding connections.
  Existing saved maps keep their selected settings. Citations, shared institutions,
  paper authorship, and paper citations remain configurable.
- The Hinton–Bengio example requests live OpenAlex records. It is not a fabricated
  graph. Search coverage limits remain visible even when a connection is found.
- Share map puts at most ten public starting points (IDs and display names), the
  edge selections, and the neighborhood size into a URL fragment. Keys, campus
  preferences, analytics IDs, and saved graph data are excluded. Opening the link
  reruns the search, so results are not a frozen snapshot. Use the image/report
  export to preserve a result. Malformed links restore the visitor's saved map
  and display an error instead of issuing arbitrary requests.
- Map links also work when pasted while already on the app. Cancelling a shared
  search stops its remaining origins. After import, reload restores subsequent
  local edits rather than replaying the original link.
- On phones, Search & tools opens the search panel. Hidden panels are inert to
  keyboard and assistive technology. See the connection opens its evidence.
- Graph animation respects reduced-motion preferences. The guide and social preview
  use the same name and positioning as the app.

## Suggested post draft

**Title:** I made a free map to see how your professor is connected to other researchers

**Body:**

I revived a side project called Coauthor Atlas. Add two researchers and it looks for
publications that connect them, then draws a little network you can explore.

There’s a one-click Geoffrey Hinton / Yoshua Bengio example, or you can try yourself,
a professor, or an author you read. You can also start with a paper, explore
researchers at your university, and save or share a map.

It uses OpenAlex records, so author identities and affiliations can be incomplete
or outdated. Searches are bounded, and “no path found” doesn’t prove that no
connection exists. It maps scholarly records, not who personally knows whom.

[Try it](https://academic-degree-of-separation.onrender.com/) ·
[Source](https://github.com/riptideiv/academic-degree-of-separation)

I’d especially like feedback on confusing results, picking the right author profile,
and whether the campus explorer is useful.

---

Use the post after deploying this branch: the current hosted site does not acquire
local changes automatically. A side-project feedback community is a practical first
venue; subject communities are a better audience fit only where their rules allow
this type of post. The Reddit rules pages did not expose their rule text to the web
reader during this review, so posting eligibility has not been verified. Read the
live community rules before posting, and disclose that you made the tool.

## Verification and limits

The offline backend/frontend regression suites and live browser checks cover the
main example, publication details, responsive layout, shared-map restoration,
exports, and public page metadata. New sharing tests cover validation, credential
exclusion, explicit empty edge selections, cancellation, and same-page map links.

The preview uses the local JSON cache and disables analytics. The existing hosted
database and production deployment have not been changed or load-tested. Public
traffic still depends on the configured OpenAlex quota and hosting capacity. This
is a small Reddit preview, not a claim of production scale or complete accessibility
of the graph canvas.
