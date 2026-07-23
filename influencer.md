# Plan: AI Creator Discovery & Outreach Agent

**Goal:** Build Serendpt's influencer marketing product starting with a focused wedge — creator discovery + outreach — validated before expanding into Tano's full stack.

---

## Scraping Reality Check (from actual tests)

| Platform | Can Scrape? | How | Production Approach |
|---|---|---|---|
| **YouTube** | ✅ Yes, no API key | Data embedded as JSON in page HTML (`ytInitialData`) | YouTube Data API v3 ($, but reliable) |
| **TikTok** | ⚠️ Browser only | Raw HTTP returns no data (JS-rendered). Browser renders profiles OK | TikTok Research API (apply) + scraper service |
| **Instagram** | ❌ Blocked | Redirects to login. Unauthenticated scraping impossible | Instagram Graph API + paid proxy scraper |
| **Email/Contact** | ⚠️ Partially | Linktree, bio scraping works. Bulk email lookup needs Apollo/Hunter API | Hunter.io, Apollo, Skrapp APIs |

**The honest takeaway:** You can launch with **YouTube + TikTok browser scraping** and add Instagram once API access clears. YouTube alone covers a huge share of meaningful influencer campaigns — it has the best audience analytics and highest CPM content.

---

## The Wedge

**AI-powered Creator Discovery + Automated Outreach**

A brand submits a brief → gets a ranked shortlist of vetted creators in 24 hours → one click to start personalized outreach → pipeline dashboard tracks responses.

### Why this wedge first
- No legal/financial complexity (no contracts, rights management, or payments)
- Pure AI-agent work — scraping, analysis, ranking, outreach
- Clear standalone value prop that doesn't exist as a self-serve tool today
- Builds a creator database that compounds in value
- Natural upsell path into affiliate management and paid ads later

---

## Phase 1 — Creator Discovery Engine (Weeks 1–4)

### Week 1: Infrastructure & Brief Intake

| Task | What |
|---|---|
| **Brief intake form & agent** | Web form (target audience, category, geography, budget) → LLM converts to structured query |
| **Scraper framework** | Abstract scraper interface so each platform is a pluggable driver. Starts with YouTube driver |
| **Storage** | SQLite/Postgres schema for creators, brand briefs, shortlists |

**Build, don't buy:** The scraper abstraction layer is your core IP. Each platform gets a driver that implements: `search(query) → List[Creator]`, `get_profile(url) → CreatorDetail`, `get_engagement(url) → EngagementMetrics`.

### Week 2: YouTube Scraper Driver (fully functional from day one)
- Use `ytInitialData` JSON extraction (confirmed working — no API key needed)
- Extract: channel name, URL, subscriber count, video titles, views, publish dates, engagement ratios
- Function: search by keyword, location, category
- **Get this working end-to-end before touching other platforms**

### Week 3: TikTok & Instagram Drivers

| Platform | Approach |
|---|---|
| **TikTok** | Use browser automation (Playwright/Puppeteer) to render pages & extract profile data. Alternatively, apply for TikTok Research API (takes 2–4 weeks — start now) |
| **Instagram** | **Two paths:** (a) Apply for Instagram Graph API (Meta Business account needed), (b) Use a paid scraping service like Apify Instagram Scraper or BrightData — $0.50–$2 per 1K profiles |
| **Fallback** | If neither works in time, source Instagram data from public embed pages or use a search-engine proxy (Google cache, Exa) |

### Week 4: Vetter, Ranker & Shortlist Generator

| Agent | What It Does |
|---|---|
| **Authenticity Vetter** | Flag suspicious accounts: follower:following ratio outliers, irregular posting patterns, comment-spam detection, account age checks |
| **Ranking Agent** | Score creators by weighted fit: brief alignment (40%), engagement rate (25%), audience authenticity (20%), reach (15%) |
| **Shortlist Generator** | LLM compiles top 10–20 creators into a readable report with one-liner rationale per pick |

### Phase 1 Deliverable
- Working discovery engine covering **YouTube (fully) + TikTok (browser-based)**
- Brand submits brief → gets ranked shortlist with rationale
- **Manual validation:** First 5 shortlists delivered to beta partners by hand (you plus a domain person) to tune quality before automating

---

## Phase 2 — Outreach Automation (Weeks 5–7)

### Week 5: Contact Finder Agent

| Source | Method |
|---|---|
| **YouTube About page** | Scrape "For business inquiries" email from channel page |
| **Linktree/Instagram bio** | Parse bio links for email or contact forms |
| **Hunter.io / Apollo API** | Paid email lookup by domain/social handle — ~$0.01–$0.10 per contact |
| **TikTok bio** | Extract email from profile bio text |

### Week 6: Message Composer & Sequencer

| Component | What |
|---|---|
| **Message Composer** | LLM generates per-creator outreach referencing their recent content, style, and audience overlap with the brand. 90%+ personalization |
| **Sequencer** | Schedule emails/DMs with follow-up cadence (Day 0 → Day 3 → Day 7). Auto-stop on reply |
| **Sender infrastructure** | SendGrid/Mailgun for email. Instagram/TikTok DM requires either API access or semi-manual workflow |

### Week 7: Reply Classifier & Lightweight CRM

| Component | What |
|---|---|
| **Reply Classifier** | LLM reads replies → tag as: Interested / Negotiating / Not Interested / Out of Office / Wrong person |
| **CRM** | Simple pipeline: Discovered → Contacted → Replied → Interested → Onboarded → Active. Store interactions, notes, next follow-up date |

### Phase 2 Deliverable
- Automated outreach with personalization per creator
- Pipeline dashboard (think: simplified HubSpot for creators)
- Reply classification working at 85%+ accuracy

---

## Phase 3 — Lightweight Tracking (Weeks 8–10)

### What it does
Once creators are onboarded and posting, track performance:
- **Post monitor** — Poll creator feeds for new sponsored content
- **Engagement collector** — Likes, comments, shares, saves per post
- **Simple dashboard** — Creator ranking by ROI, cost-per-engagement
- **Top-performer flag** — Auto-identify creators worth renewing or upsell to paid ads

### Key Decision
**At this point you face a fork:**
- **Path A:** Stay a discovery + outreach tool (SaaS, lighter)
- **Path B:** Expand into Tano's territory — affiliate management, content rights, paid ad creative (heavier, higher revenue per customer)

The data from Phase 3 (which creators convert, what content performs) tells you which path has the stronger pull.

---

## Scraping Infrastructure (The Part You Actually Need to Buy)

This is the non-obvious cost most AI agent builds underestimate:

| Item | Cost/Month | Why |
|---|---|---|
| **Proxy rotation** (BrightData/ScrapingBee) | $200–$500 | TikTok & Instagram block datacenter IPs |
| **Browser automation host** (Browserbase/Playwright cloud) | $100–$300 | Render JS-heavy pages (TikTok, Instagram) |
| **YouTube Data API v3** | $0–$200 | Free quota is 10K units/day. $200 for 100K/day |
| **Hunter.io / Apollo** | $50–$200 | Email lookup for creator contact info |
| **SendGrid/Mailgun** | $20–$100 | Email outreach sending |
| **Total infra** | **$370–$1,300/mo** | |

---

## Go-to-Market

### Pricing

| Tier | Price | What |
|---|---|---|
| **Starter** | $499/mo | 5 shortlists/mo, up to 15 creators each, YouTube + TikTok |
| **Growth** | $1,499/mo | Unlimited shortlists + outreach automation + CRM + Instagram |
| **Enterprise** | Custom | Managed service + dedicated strategist + custom platform integrations |

### Target Customers

| Segment | Why They'd Buy |
|---|---|
| **DTC brands** ($1–10M GMV) | Can't afford agencies charging 20% retainers. Need to scale influencer without hiring a team |
| **Digital agencies** | Want to offer influencer marketing without building an in-house team. White-label potential |
| **Nigerian/African brands** | You have home-field advantage — local creator discovery in Nigeria is underserved |

### Competitive Positioning vs Tano

| Dimension | Tano | You |
|---|---|---|
| **Model** | Managed service (agency + software) | Self-serve tool (software-first) |
| **Price point** | $10K+/mo (enterprise retainer) | $499–$1,499/mo |
| **Go-to-market** | Sales-led (demo call, contract) | Product-led (sign up, paste brief, go) |
| **Platforms** | All major (paid team behind it) | YouTube-first, then TikTok, then Instagram |
| **Geography** | London / UK / EU | Nigeria / Africa / Global |
| **Speed** | 24hr shortlists | Same, but self-serve |

### The Real Advantage

Tano is enterprise-focused, expensive, and sales-led. There's **nobody** offering a self-serve creator discovery tool at $500/mo that actually works. Brands doing their first influencer campaigns won't pay Tano $10K — they need a try-before-you-buy option. That's your gap.

---

## Team

| Role | Commitment | Notes |
|---|---|---|
| **Backend/fullstack engineer** | Full-time | Python (FastAPI) + React. Agent orchestration, APIs, dashboard |
| **ML/AI engineer** | Part-time → Full-time | Scraping pipelines, ranking models, LLM prompt engineering |
| **Domain marketer** | Part-time consultant | Validates shortlist quality, knows influencer space, can bring beta customers |
| **You (Ebuka)** | Product + direction | Scope, customer convos, prioritization |

---

## Validation Milestones

| When | Milestone | How to Measure |
|---|---|---|
| **Week 1** | Apply for TikTok Research + Instagram Graph API | Submitted. If rejected, alternative path activated |
| **Week 2** | Manual shortlist for 3 friendly brands | "Would you pay $499 for this?" — 2/3 say yes |
| **Week 4** | Automated discovery engine live (YouTube) | Shortlist in <24hr, creators rated 4/5+ relevant by brand testers |
| **Week 7** | Outreach automation live | 2× reply rate vs generic template (A/B test) |
| **Week 10** | 5 paying customers | $2.5K–$7.5K MRR |

---

## What Success Looks Like

**Month 1:** You can give a brand a brief intake form → get back a ranked YouTube shortlist with audience analysis.

**Month 2:** One click sends personalized outreach to 20 creators. Reply classifier fills your pipeline.

**Month 3:** Brands are paying $499/mo. You know exactly which creators convert. Now you decide: stay a discovery tool, or build the affiliate + ads stack and go after Tano's market.

---

## Key Risks

| Risk | Mitigation |
|---|---|
| **YouTube is the only reliable source at launch** | TikTok browser scraping works for MVPs. Instagram via paid scraper in Month 2. But YouTube discovery is already valuable — don't let perfect be enemy of good |
| **Creator contact info is hard to find** | Hunter.io + manual curation. Early customers will accept 60–70% contact rate if shortlist quality is high |
| **Brands don't trust AI-vetted creators** | Show the vetting criteria transparently. Offer 1 free manual review per shortlist |
| **TikTok Research API application rejected** | Use browser-based scraping + a service like Apify. Less reliable but works |
| **Instagram API access delayed** | Launch without Instagram (YouTube + TikTok). Add it when approved. Most brands will still pay |
