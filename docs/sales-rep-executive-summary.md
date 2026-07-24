# Sales-Rep Executive Summary — frontend guide

Rep-level version of the existing KPI-card executive summary. Same response shape, same
text markers, so **the renderer you already use for the group cards works unchanged** — you
only add a rep parameter.

Base URL: `REACT_APP_API_BASEURL_INSIGHTS` (dev: `http://20.244.108.100:8014`).

---

## 1. List the reps

```
GET /pbi/portal/main-insights/sales-reps?min_signals=1
```

```json
[
  { "sales_rep": "Arpita Priyadarshini", "signal_count": 53 },
  { "sales_rep": "Fawzan Yusuf",         "signal_count": 49 },
  { "sales_rep": "Uday Singh",           "signal_count": 47 }
]
```

Sorted busiest first. Use it to populate the rep picker — this is exactly the population the
summaries exist for. `min_signals` filters the thin tail (e.g. `?min_signals=20` → 35 reps).

---

## 2. Get a rep's summary

```
POST /pbi/portal/main-insights/executive-summary/sales-rep?sales_rep=Uday%20Singh&group=group5
```

| Param | Required | Notes |
|---|---|---|
| `sales_rep` | yes | Matched case-insensitively. `400` if blank. |
| `group` | no | `group1`..`group5`. **Omit for the rep's overall summary.** `400` on an unknown group. |
| `limit` | no | Max signals to load on the generate path. Default 500. |

```json
{
  "sales_rep": "Uday Singh",
  "group": "group5",
  "source": "stored",
  "signal_count": 4,
  "executive_summary": [
    "0 of Uday Singh's 19 eligible deals have funding approved. /red That is 0% attach, and this ratio is unaffected by the data problem.",
    "..."
  ],
  "insight": {
    "heading": "/h Uday Singh: 19 Deals Carrying Unclaimed Partner Funding /h",
    "description": "Plain text, 2-4 sentences."
  },
  "recommended_action": "Walk Uday Singh through the funding claim process..."
}
```

- `executive_summary` is **always exactly 5 strings**.
- `insight` and `recommended_action` are `null` when `group` is omitted (the overall card is
  pointers-only — same as the group `overall` today).
- `source` is `"stored"` when served from the table, `"generated"` when it fell through to the
  LLM. Don't branch on it; it's for debugging. A `generated` response is slow (seconds).

### Card mapping

| `group` | Card |
|---|---|
| `group1` | Real vs Stated Pipeline |
| `group2` | Infant Mortality |
| `group3` | Organic vs Referral Sourcing |
| `group4` | Forecast Smoke |
| `group5` | OEM Funding Left on Table |
| *(omitted)* | Overall |

Every rep has all 6 — 40 reps × 6 = 240 rows are populated.

---

## 3. Rendering contract — the important part

The text carries inline markers. **Strip them; never print them.**

### Pointers: `/green` and `/red`

Each pointer has a neutral lead clause, then **exactly one** marker, then the clause to
emphasise. Split on the first marker:

```ts
const MARKER = /\s*\/(green|red)\s+/;

export function renderPointer(p: string) {
  const m = MARKER.exec(p);
  if (!m) return { lead: p, tone: null, emphasis: "" };   // defensive
  return {
    lead: p.slice(0, m.index).trim(),
    tone: m[1] as "green" | "red",
    emphasis: p.slice(m.index + m[0].length).trim(),
  };
}
```

Use `.exec()` + `slice`, **not** `split().filter()` — a filter that drops `"green"`/`"red"`
tokens also eats an emphasis clause that happens to start with those words.

Verified against all 1,230 stored pointers: every one matches, none produce an empty lead or
empty emphasis.

Render `lead` in normal body text and `emphasis` in the tone colour (bold red / bold green) —
matching how the group card renders today.

### Heading: `/h ... /h`

```ts
const heading = insight.heading.replace(/^\/h\s*/, "").replace(/\s*\/h$/, "");
```

`description` and `recommended_action` are plain text — no markers, render as-is.

---

## 4. Things that will bite you

- **`sales_rep` must be the exact name string** from `/sales-reps`. There's no rep ID. Names
  contain spaces (URL-encode) and one has a double space: `"Divyanshu  Srivastava"`. Take
  values from the list endpoint rather than typing them.
- **Thin cards read as disclosure, not analysis.** Where a rep has little data on a card
  (Forecast Smoke is thin for most reps — company-wide there are only three commits), some of
  the 5 pointers say so explicitly, e.g. *"Only 2 measure(s) reported … /red Too thin to read a
  trend from."* That's deliberate. If you want to visually de-emphasise those, `signal_count`
  is your signal — it's the number of KPIs backing that card.
- **Don't cache across a refresh.** Rows are replaced wholesale when the summaries are
  regenerated; there's no ETag or version.
- **`generated` responses are not persisted.** If a row is missing, every request re-generates
  (slow + costs an LLM call). Missing rows mean the table needs refreshing, not a retry loop.
- The current data is the **10-16 Jul run**. Ratios (real-vs-stated %, funding attach %) are
  sound; absolute counts are inflated by a known data-load problem. The pointer text says so
  where it matters — don't add your own "up X%" chrome on top of these numbers.

---

## 5. Related

The group-level equivalent, unchanged:

```
POST /pbi/portal/main-insights/executive-summary?group=group1
```

Same response fields minus `sales_rep` / `signal_count`. See
[main-insights-endpoints.md](main-insights-endpoints.md).
