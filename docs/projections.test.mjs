/* =============================================================================
   Tests for the projection engine — node --test docs/projections.test.mjs

   Node's built-in runner, no dependencies, in keeping with the rest of the repo
   (the site ships one vendored file and nothing else). projections.js is
   evaluated rather than imported so these run against the exact script the page
   loads, not a module copy of it that could drift.

   What is covered, in the order the requirements ask for it:
     · the shipped configuration ties back to the city's published figures
     · a surplus case, a deficit case, and a case that crosses between them
     · the revenue-based mechanism (certified rate falls; new growth is additive)
     · fixed-rate vs fixed-revenue as genuinely different policies
     · every input actually moves the outputs
     · validation catches missing and impossible assumptions
   ============================================================================= */

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const here = (f) => new URL(f, import.meta.url);

(0, eval)(readFileSync(here("./projections.js"), "utf8"));
const { project, validate, findCrossings, scenarioById } = globalThis.Projections;

const CONFIG = JSON.parse(readFileSync(here("./data.projections.json"), "utf8"));

// Money comparisons are to the cent; rates to nine places, which is three more
// than the city publishes.
const money = (a, b, msg) => assert.ok(Math.abs(a - b) < 0.005, `${msg}: ${a} vs ${b}`);
const close = (a, b, eps, msg) => assert.ok(Math.abs(a - b) < eps, `${msg}: ${a} vs ${b}`);

/* A deliberately tiny config, so the arithmetic under test is arithmetic anyone
   can check in their head rather than the real city's six-figure lines. */
function toyConfig(over) {
  return Object.assign(
    {
      first_year: 2027,
      last_year: 2030,
      year_labels: {},
      baseline: {
        taxable_value: { value: 1_000_000_000 },
        collection_rate: { value: 1 },
        other_revenue: { value: 0 },
        expenditures: { value: 1_000_000 },
        appreciation_rate: { value: 0 },
        new_growth_rate: { value: 0 },
        other_revenue_growth: { value: 0 },
        expenditure_growth: { value: 0 },
      },
      value_additions: [],
      adjustments: [],
      scenarios: [
        { id: "flat", name: "Flat", method: "revenue_based", start_rate: 0.001, years: {} },
        { id: "fixed", name: "Fixed rate", method: "fixed_rate", start_rate: 0.001, years: {} },
      ],
    },
    over
  );
}

/* ========================================================================== */
/* 1. The shipped configuration reproduces the city's own published figures.  */
/* ========================================================================== */

test("the scenarios levy the rates the page says they levy", () => {
  const rates = Object.fromEntries(CONFIG.scenarios.map((s) => [s.id, s.start_rate]));
  assert.equal(rates.none, 0.00053, "no increase = the 2026 certified rate");
  assert.equal(rates.half, 0.00105, "half = the partial increase this project models");
  assert.equal(rates.adopted, 0.0015, "adopted = the rate adopted 6 August");
  // No scenario carries an invented annual step on top of the certified rate.
  CONFIG.scenarios.forEach((s) => {
    assert.ok(!s.default_above_certified, `${s.id} must not carry a made-up annual step`);
    assert.deepEqual(s.years, {}, `${s.id} must not carry per-year rate overrides`);
  });
});

test("every scenario carries the copy section 01 renders for it", () => {
  // Section 01 lists the three rates off this same array and writes the picked
  // one out underneath the switcher, so a scenario missing any of these
  // renders "undefined" at the top of the page.
  CONFIG.scenarios.forEach((s) => {
    assert.ok(s.short, `${s.id} needs a switcher label`);
    assert.ok(s.rate_label, `${s.id} needs a name for the rate list`);
    assert.ok(s.blurb, `${s.id} needs a one-line definition for the rate list`);
    assert.ok(s.name, `${s.id} needs a heading for the scenario block`);
    assert.ok(s.description, `${s.id} needs an opening paragraph`);
    assert.ok(s.closer, `${s.id} needs a closing paragraph`);
    // The subject of the sentence that states the levy — "The adopted rate is
    // 183.02% higher than…" — so the figure is never written into the copy.
    assert.ok(s.rate_noun, `${s.id} needs a noun for the revenue sentence`);
  });
});

test("a rate nobody proposed is declared as an assumption, and only that one is", () => {
  // 0.001050 is this project's own middle case. The file's rule is that a
  // chosen number carries `assumption: true` and a `basis`; a real one carries
  // a `source`. This is what keeps the invented rate from reading as a
  // published one — on the page and in the file both.
  CONFIG.scenarios.forEach((s) => {
    if (s.assumption) {
      assert.ok(s.basis, `${s.id} must say what its rate is based on`);
      assert.ok(!s.source, `${s.id} is invented, so it cannot carry a source`);
    } else {
      assert.ok(s.source, `${s.id} must cite where its rate comes from`);
    }
  });
  assert.deepEqual(
    CONFIG.scenarios.filter((s) => s.assumption).map((s) => s.id),
    ["half"],
    "exactly one scenario is this project's own"
  );
});

test("each published scenario's FY2026-27 levy reproduces the city's own figure", () => {
  // Both of these are printed somewhere: the impact schedule's no-increase
  // revenue and the motion's adopted levy. To the dollar, not the cent: the
  // city publishes $3,055,201 and the rate it publishes gives $3,055,200.90.
  close(project(CONFIG, "none").rows[0].propertyTax, 3_055_201, 1, "certified rate raises");
  money(project(CONFIG, "adopted").rows[0].propertyTax, 8_646_795, "adopted rate raises");
  // The middle case has nothing published to check against, so it is checked
  // against its own arithmetic: rate × the roll the other two agree on.
  money(project(CONFIG, "half").rows[0].propertyTax, 0.00105 * 5_764_530_000, "the modelled middle rate raises");
});

test("the above-certified percentages fall out at the figures in the record", () => {
  // Not fed to the model anywhere — it is rate ÷ certified rate, and it lands
  // on the motion's own 183.02%. If the taxable value or either rate were
  // wrong, this would not agree.
  close(project(CONFIG, "adopted").rows[0].aboveCertified * 100, 183.02, 0.01, "adopted vs certified");
  close(project(CONFIG, "none").rows[0].aboveCertified * 100, 0, 1e-9, "no increase is 0% above certified");
});

test("the certified rate in the config reproduces the schedule's no-increase revenue", () => {
  const v = CONFIG.baseline.taxable_value.value;
  const c = CONFIG.baseline.certified_rate.value;
  // The impact schedule's "$3,055,201 the certified rate would raise".
  close(v * c, 3_055_201, 1, "certified-rate revenue");
});

test("the FY2026-27 bridge from the balanced budget holds for every scenario", () => {
  // The interim budget balances at $42,650,441. Every scenario's FY2026–27
  // figure is that, less the reserve draw counted as revenue, plus or minus
  // its own levy against the one the interim budget assumed.
  const c = CONFIG.reconciliation;
  CONFIG.scenarios.forEach((s) => {
    const row = project(CONFIG, s.id).rows[0];
    const bridge = c.balanced - c.reserve_draw + (row.propertyTax - c.interim_levy);
    money(row.gap, bridge, `${s.id} bridge`);
    money(row.expenditures, 42_650_441, `${s.id} FY2027 appropriations`);
    money(row.totalRevenue, row.propertyTax + 32_336_071, `${s.id} FY2027 revenue`);
  });

  // And for the adopted rate the bridge is the rate cut plus the reserve draw,
  // which is the arithmetic the page states in words.
  money(project(CONFIG, "adopted").rows[0].gap, -(1_151_344 + 516_231), "adopted-rate gap");
});

test("the no-increase case is the widest gap and the adopted rate the narrowest", () => {
  const [none, half, adopted] = ["none", "half", "adopted"].map((id) => project(CONFIG, id));
  assert.ok(none.summary.cumulative < half.summary.cumulative);
  assert.ok(half.summary.cumulative < adopted.summary.cumulative);
  // None of the three recovers. Every scenario on the page is in deficit in
  // every projected year, so no line ever crosses: the highest rate the page
  // now models is the one the council actually adopted, and even that does not
  // reach spending. Any future scenario that does cross will trip this.
  [["none", none], ["half", half], ["adopted", adopted]].forEach(([id, r]) => {
    assert.equal(r.summary.deficitYears, r.rows.length, `${id} is in deficit every year`);
    assert.equal(r.crossings.length, 0, `${id} never crosses`);
  });
});

test("every shipped scenario runs clean and covers the stated period", () => {
  CONFIG.scenarios.forEach((s) => {
    const r = project(CONFIG, s.id);
    assert.ok(r.ok, `${s.id}: ${JSON.stringify(r.errors)}`);
    assert.equal(r.rows.length, CONFIG.last_year - CONFIG.first_year + 1, `${s.id} row count`);
    assert.equal(r.rows[0].year, CONFIG.first_year);
    assert.equal(r.rows.at(-1).year, CONFIG.last_year);
    r.rows.forEach((row) => {
      assert.ok(Number.isFinite(row.gap), `${s.id} ${row.year} gap is finite`);
      assert.ok(Number.isFinite(row.effectiveRate), `${s.id} ${row.year} rate is finite`);
      assert.ok(row.taxableValue > 0, `${s.id} ${row.year} taxable value positive`);
    });
    // Cumulative is the running sum of the annual gaps, by construction.
    let run = 0;
    r.rows.forEach((row) => {
      run += row.gap;
      money(row.cumulative, run, `${s.id} ${row.year} cumulative`);
    });
  });
});

test("more rate means more revenue, in every projected year", () => {
  const none = project(CONFIG, "none");
  const half = project(CONFIG, "half");
  const adopted = project(CONFIG, "adopted");
  // The anchor year is common to all three, so compare from the first projected year.
  for (let i = 1; i < none.rows.length; i++) {
    assert.ok(half.rows[i].propertyTax > none.rows[i].propertyTax, `half > none in year ${i}`);
    assert.ok(adopted.rows[i].propertyTax > half.rows[i].propertyTax, `adopted > half in year ${i}`);
  }
  assert.ok(adopted.summary.cumulative > half.summary.cumulative);
  assert.ok(half.summary.cumulative > none.summary.cumulative);
});

test("the data-center cross-check in the config is internally consistent", () => {
  const d = CONFIG.value_additions_derivation;
  d.rows.forEach((r) => {
    // Each row's stated implication is its amount over its rate, in thousands.
    close(r.amount / r.rate / 1000, r.implies, 1, `${r.who} implied value`);
  });
  // And the additions add up to the ~$1.9B those four rows agree on.
  const total = CONFIG.value_additions.reduce((a, x) => a + x.amount, 0);
  const mean = d.rows.reduce((a, r) => a + r.implies * 1000, 0) / d.rows.length;
  close(total / 1e9, mean / 1e9, 0.15, "additions match the cross-check mean");
});

/* ========================================================================== */
/* 1b. The data-center sensitivity.                                           */
/* ========================================================================== */

test("the additions mode defaults to full, and junk falls back to it", () => {
  const full = project(CONFIG, "none", { additions: "full" });
  ["", null, undefined, "half", "FULL", 1].forEach((junk) => {
    const r = project(CONFIG, "none", { additions: junk });
    assert.equal(r.additions, "full", `additions=${String(junk)} should fall back`);
    money(r.summary.cumulative, full.summary.cumulative, `additions=${String(junk)}`);
  });
  // And omitting the option entirely is the same as asking for full, so the
  // page's default and every existing caller agree.
  money(project(CONFIG, "none").summary.cumulative, full.summary.cumulative, "no option given");
});

test("the three additions modes are strictly ordered, and 'none' zeroes the roll additions", () => {
  const [none, partial, full] = ["none", "partial", "full"].map((m) =>
    project(CONFIG, "none", { additions: m })
  );

  // No data-center value at all: every projected year's addition is zero.
  none.rows.forEach((row) => money(row.valueAddition, 0, `${row.year} carries no addition`));

  // Less assumed value can only mean less revenue, never more.
  assert.ok(none.summary.cumulative < partial.summary.cumulative, "none is worse than partial");
  assert.ok(partial.summary.cumulative < full.summary.cumulative, "partial is worse than full");
});

test("'partial' keeps exactly the buildings the city made a claim about", () => {
  const r = project(CONFIG, "none", { additions: "partial" });
  const kept = CONFIG.value_additions.filter((a) => a.incentive_free === true);
  const dropped = CONFIG.value_additions.filter((a) => a.incentive_free !== true);

  // The split is a property of the config, not of a hard-coded year in the
  // engine: whichever rows carry the flag are the rows that survive.
  assert.ok(kept.length && dropped.length, "the config must distinguish the two kinds");
  kept.forEach((a) => {
    const row = r.rows.find((x) => x.year === a.year);
    money(row.valueAddition, a.amount, `FY${a.year} keeps the incentive-free addition`);
  });
  dropped.forEach((a) => {
    const row = r.rows.find((x) => x.year === a.year);
    money(row.valueAddition, 0, `FY${a.year} drops the unconfirmed addition`);
  });
});

test("dropping the data-center additions is what section 02 quotes as the downside", () => {
  // The page no longer offers this as a control — it states one assumption and
  // names the alternative in a sentence. But that sentence's figure is computed
  // by rerunning the model with the additions dropped rather than written into
  // the copy, so the option has to keep working even with no UI on it.
  ["none", "half", "adopted"].forEach((id) => {
    const full = project(CONFIG, id);
    const without = project(CONFIG, id, { additions: "none" });
    assert.ok(without.ok, `${id} must still run with the additions dropped`);
    assert.ok(
      without.summary.cumulative < full.summary.cumulative,
      `${id}: dropping $1.9bn of taxable value must widen the gap, not narrow it`
    );
  });
});

/* ========================================================================== */
/* 1c. The exposure figures the page states more than once.                   */
/* ========================================================================== */

test("the four spending categories add up to the flexible pool exactly", () => {
  // Section 04 derives this quantity twice from different places — the pool
  // table subtracts the constrained tiers from total appropriations, and the
  // composition below it sums the four spending categories — and now prints
  // both. They were a dollar apart, which read as an arithmetic error on a page
  // whose whole claim is that its arithmetic checks out.
  const P = CONFIG.exposure.pool;
  const residual = P.total - P.tiers.reduce((a, t) => a + t.amount, 0);
  const categories = CONFIG.exposure.order.rungs.reduce((a, x) => a + x.amount, 0);
  assert.equal(categories, residual, "the two derivations of the flexible pool must agree");
});

test("the transfers tier is flagged deferrable, so the wider figure gets stated", () => {
  // The page's own footnote says these transfers are mostly deferrable capital
  // rather than a fixed obligation. Section 04 reads this flag to print the
  // second, larger reachable figure; without it the page understates what a
  // council could move and contradicts its own note.
  const P = CONFIG.exposure.pool;
  const deferrable = P.tiers.filter((t) => t.deferrable);
  assert.equal(deferrable.length, 1, "exactly the transfers tier is deferrable");
  assert.equal(deferrable[0].key, "transfers");
  assert.ok(P.deferrable_label && P.deferrable_note, "the wider figure needs its own copy");
  // Every tier states how constrained it is; the table renders a column of it.
  P.tiers.forEach((t) => assert.ok(t.flex, `${t.key} needs a flexibility note`));
  assert.ok(P.reachable_flex, "the reachable row needs one too");
});

/* ========================================================================== */
/* 2. Surplus, deficit, and a crossover.                                      */
/* ========================================================================== */

test("surplus case: revenue above expenditure every year", () => {
  const r = project(
    toyConfig({
      baseline: Object.assign(toyConfig().baseline, {
        other_revenue: { value: 500_000 },
        expenditures: { value: 1_000_000 },
      }),
    }),
    "flat"
  );
  assert.ok(r.ok);
  // 1,000,000 of property tax + 500,000 other against 1,000,000 of spending.
  r.rows.forEach((row) => assert.ok(row.gap > 0, `${row.year} should be a surplus`));
  money(r.rows[0].gap, 500_000, "FY2027 surplus");
  money(r.summary.cumulative, 2_000_000, "cumulative surplus over four years");
  assert.equal(r.crossings.length, 0, "a pure surplus never crosses");
  assert.equal(r.summary.deficitYears, 0);
});

test("deficit case: expenditure above revenue every year", () => {
  const cfg = toyConfig();
  cfg.baseline.expenditures = { value: 2_000_000 };
  const r = project(cfg, "flat");
  assert.ok(r.ok);
  r.rows.forEach((row) => assert.ok(row.gap < 0, `${row.year} should be a deficit`));
  money(r.rows[0].gap, -1_000_000, "FY2027 deficit");
  money(r.summary.cumulative, -4_000_000, "cumulative deficit over four years");
  assert.equal(r.summary.deficitYears, 4);
  assert.equal(r.crossings.length, 0, "a pure deficit never crosses");
});

test("crossover case: a surplus that grows into a deficit, located exactly", () => {
  const cfg = toyConfig();
  cfg.baseline.other_revenue = { value: 200_000 };   // total revenue 1,200,000, flat
  cfg.baseline.expenditures = { value: 1_000_000 };
  cfg.baseline.expenditure_growth = { value: 0.1 };  // 1.0M, 1.1M, 1.21M, 1.331M
  const r = project(cfg, "flat");
  assert.ok(r.ok);

  money(r.rows[0].gap, 200_000, "FY2027 surplus");
  money(r.rows[1].gap, 100_000, "FY2028 surplus");
  money(r.rows[2].gap, -10_000, "FY2029 deficit");
  money(r.rows[3].gap, -131_000, "FY2030 deficit");

  assert.equal(r.crossings.length, 1, "exactly one crossing");
  const x = r.crossings[0];
  assert.equal(x.fromYear, 2028);
  assert.equal(x.toYear, 2029);
  assert.equal(x.into, "deficit");
  // 100,000 / (100,000 - -10,000) = 0.909…
  close(x.fraction, 100_000 / 110_000, 1e-12, "crossing fraction");

  // The interpolated gap at the crossing is zero, which is what the chart's
  // green/red split depends on.
  const gapAt = r.rows[1].gap + x.fraction * (r.rows[2].gap - r.rows[1].gap);
  close(gapAt, 0, 1e-6, "gap is zero at the crossing");

  assert.equal(r.summary.deficitYears, 2);
  assert.equal(r.summary.worstYear, 2030);
});

test("a deficit that recovers into surplus crosses the other way", () => {
  const cfg = toyConfig();
  cfg.baseline.other_revenue = { value: 100_000 };
  cfg.baseline.other_revenue_growth = { value: 0.5 };
  cfg.baseline.expenditures = { value: 1_200_000 };
  // Levy flat at 1,000,000; other revenue 100k → 150k → 225k → 337.5k.
  // Gaps: −100,000, −50,000, +25,000, +137,500.
  const r = project(cfg, "flat");
  assert.ok(r.rows[0].gap < 0, "starts in deficit");
  assert.ok(r.rows.at(-1).gap > 0, "ends in surplus");
  assert.equal(r.crossings.length, 1);
  assert.equal(r.crossings[0].into, "surplus");
});

test("touching zero without changing sign is not a crossing", () => {
  const rows = [{ gap: 5 }, { gap: 0 }, { gap: 5 }];
  assert.equal(findCrossings(rows).length, 0);
  // Whereas passing through it is.
  assert.equal(findCrossings([{ gap: 5 }, { gap: -5 }]).length, 1);
});

/* ========================================================================== */
/* 3. The revenue-based mechanism itself.                                     */
/* ========================================================================== */

test("with no new growth, holding the certified rate holds revenue flat", () => {
  const cfg = toyConfig();
  cfg.baseline.appreciation_rate = { value: 0.2 }; // values climb 20% a year
  cfg.baseline.new_growth_rate = { value: 0 };
  const r = project(cfg, "flat");

  r.rows.forEach((row) => money(row.propertyTax, 1_000_000, `${row.year} levy is unchanged`));
  // And the rate falls to do it: 0.001000 → 0.000833… → 0.000694…
  close(r.rows[1].effectiveRate, 0.001 / 1.2, 1e-12, "FY2028 certified rate");
  close(r.rows[2].effectiveRate, 0.001 / 1.44, 1e-12, "FY2029 certified rate");
  assert.ok(r.rows[3].effectiveRate < r.rows[0].effectiveRate, "the rate keeps falling");
});

test("new growth is additive — it is the only thing that raises revenue", () => {
  const cfg = toyConfig();
  cfg.baseline.appreciation_rate = { value: 0 };
  cfg.baseline.new_growth_rate = { value: 0.05 };
  const r = project(cfg, "flat");

  // Year 2: 1.0B existing + 50M new, certified rate still 0.001 (existing value
  // is unchanged), so 1,000,000 + 50,000.
  close(r.rows[1].certifiedRate, 0.001, 1e-15, "certified rate holds when values do not move");
  money(r.rows[1].propertyTax, 1_050_000, "FY2028 levy");
  money(r.rows[1].newGrowthValue, 50_000_000, "FY2028 new growth value");
  // Year 3 grows off the new, larger base.
  money(r.rows[2].propertyTax, 1_050_000 + 1_050_000_000 * 0.05 * 0.001, "FY2029 levy");
});

test("a named value addition lands as new growth, and is kept at the levied rate", () => {
  const cfg = toyConfig();
  cfg.value_additions = [{ year: 2029, amount: 500_000_000 }];
  const r = project(cfg, "flat");

  money(r.rows[1].valueAddition, 0, "nothing in FY2028");
  money(r.rows[2].valueAddition, 500_000_000, "the addition lands in FY2029");
  // 0.001 x 500M = 500,000 on top of the flat 1,000,000.
  money(r.rows[2].propertyTax, 1_500_000, "FY2029 levy");
  // And it stays in the base afterwards.
  money(r.rows[3].propertyTax, 1_500_000, "FY2030 levy");
});

test("the rate the city levies decides what the data centers are worth to it", () => {
  const lo = toyConfig();
  lo.value_additions = [{ year: 2028, amount: 1_900_000_000 }];
  const hi = JSON.parse(JSON.stringify(lo));
  hi.scenarios[0].start_rate = 0.003;

  const a = project(lo, "flat").rows[1].propertyTax - project(lo, "flat").rows[0].propertyTax;
  const b = project(hi, "flat").rows[1].propertyTax - project(hi, "flat").rows[0].propertyTax;
  money(a, 1_900_000, "at 0.001");
  money(b, 5_700_000, "at 0.003");
});

test("an above-certified step compounds off the certified rate, not the old rate", () => {
  const cfg = toyConfig();
  cfg.baseline.appreciation_rate = { value: 0.1 };
  cfg.scenarios[0].default_above_certified = 0.05;
  const r = project(cfg, "flat");

  const certified = 0.001 / 1.1;
  close(r.rows[1].certifiedRate, certified, 1e-12, "FY2028 certified rate");
  close(r.rows[1].effectiveRate, certified * 1.05, 1e-12, "FY2028 levied rate");
  close(r.rows[1].aboveCertified, 0.05, 1e-12, "reported as 5% above certified");
  // 5% above certified raises 5% more than last year, values having done nothing else.
  money(r.rows[1].propertyTax, 1_050_000, "FY2028 levy");
});

test("a rate override for one year wins, and later years re-base off it", () => {
  const cfg = toyConfig();
  cfg.scenarios[0].years = { 2028: { rate_override: 0.002 } };
  const r = project(cfg, "flat");
  close(r.rows[1].effectiveRate, 0.002, 1e-15, "the override applies");
  money(r.rows[1].propertyTax, 2_000_000, "FY2028 levy doubles");
  // FY2029 goes back to certified, which now reproduces the *new* higher levy.
  money(r.rows[2].propertyTax, 2_000_000, "FY2029 holds the raised levy");
});

/* ========================================================================== */
/* 4. Fixed rate and fixed revenue are different policies.                    */
/* ========================================================================== */

test("fixed rate and fixed revenue diverge as soon as values move", () => {
  const cfg = toyConfig();
  cfg.baseline.appreciation_rate = { value: 0.08 };

  // Two scenarios in the same configuration, differing only in `method` —
  // which is how a fixed-rate case would be declared if one were ever wanted.
  const rev = project(cfg, "flat");
  const fix = project(cfg, "fixed");

  // Same starting point.
  money(rev.rows[0].propertyTax, fix.rows[0].propertyTax, "anchor year agrees");

  // Then: flat under the revenue-based reading, compounding under the fixed rate.
  money(rev.rows.at(-1).propertyTax, 1_000_000, "revenue-based stays flat");
  money(fix.rows.at(-1).propertyTax, 1_000_000 * Math.pow(1.08, 3), "fixed rate compounds");
  assert.ok(fix.summary.cumulative > rev.summary.cumulative);

  // The fixed-rate case reports how far above certified it has drifted, which
  // is the number a Truth-in-Taxation notice would have to carry.
  close(fix.rows[1].aboveCertified, 0.08, 1e-12, "a held rate is an 8% increase in year two");
});

test("with values flat, the two readings are identical", () => {
  const cfg = toyConfig();
  cfg.baseline.appreciation_rate = { value: 0 };
  cfg.baseline.new_growth_rate = { value: 0 };
  const rev = project(cfg, "flat");
  const fix = project(cfg, "fixed");
  rev.rows.forEach((row, i) => money(row.propertyTax, fix.rows[i].propertyTax, `year ${row.year}`));
});

/* ========================================================================== */
/* 5. Every input moves the model.                                            */
/* ========================================================================== */

test("each baseline input changes the result, and only in the right direction", () => {
  const baseline = project(CONFIG, "none");
  const bump = (field, delta) => project(CONFIG, "none", { overrides: { [field]: delta } });

  const b = CONFIG.baseline;
  const cases = [
    ["taxable_value", b.taxable_value.value * 1.1, "up"],
    ["collection_rate", 0.95, "down"],
    ["other_revenue", b.other_revenue.value + 1_000_000, "up"],
    ["expenditures", b.expenditures.value + 1_000_000, "down"],
    ["new_growth_rate", b.new_growth_rate.value + 0.01, "up"],
    ["other_revenue_growth", b.other_revenue_growth.value + 0.01, "up"],
    ["expenditure_growth", b.expenditure_growth.value + 0.01, "down"],
  ];

  cases.forEach(([field, value, dir]) => {
    const r = bump(field, value);
    assert.ok(r.ok, `${field}: ${JSON.stringify(r.errors)}`);
    const moved = r.summary.cumulative - baseline.summary.cumulative;
    assert.ok(Math.abs(moved) > 1, `${field} should move the cumulative gap`);
    if (dir === "up") assert.ok(moved > 0, `${field} should improve the gap, moved ${moved}`);
    else assert.ok(moved < 0, `${field} should worsen the gap, moved ${moved}`);
  });
});

test("appreciation is near-inert under the revenue-based reading", () => {
  const a = project(CONFIG, "none", { overrides: { appreciation_rate: 0.02 } });
  const b = project(CONFIG, "none", { overrides: { appreciation_rate: 0.09 } });
  // Not bit-identical — new growth is a share of the whole roll, which
  // appreciation does inflate — but small against a multi-million-dollar gap.
  const drift = Math.abs(b.summary.cumulative - a.summary.cumulative);
  assert.ok(drift < Math.abs(a.summary.cumulative) * 0.25, `drift was ${drift}`);
});

test("under a fixed rate, appreciation is the whole engine instead", () => {
  const at = (rate, id) => {
    const c = toyConfig();
    c.baseline.appreciation_rate = { value: rate };
    c.baseline.new_growth_rate = { value: 0.04 };
    return project(c, id).summary.cumulative;
  };
  const revDrift = Math.abs(at(0.09, "flat") - at(0.02, "flat"));
  const fixDrift = Math.abs(at(0.09, "fixed") - at(0.02, "fixed"));
  assert.ok(fixDrift > revDrift * 4, `fixed-rate drift ${fixDrift} vs revenue-based ${revDrift}`);
});

test("the collection rate scales the levy and nothing else", () => {
  const full = project(CONFIG, "none");
  const part = project(CONFIG, "none", { overrides: { collection_rate: 0.9 } });
  // Property tax scales; other revenue and expenditure do not.
  money(part.rows[0].propertyTax, full.rows[0].propertyTax * 0.9, "FY2027 levy scales");
  money(part.rows[0].otherRevenue, full.rows[0].otherRevenue, "other revenue untouched");
  money(part.rows[0].expenditures, full.rows[0].expenditures, "expenditure untouched");
});

test("recurring adjustments compound; one-time adjustments do not", () => {
  const cfg = toyConfig();
  cfg.baseline.expenditure_growth = { value: 0.1 };
  cfg.adjustments = [
    { year: 2028, amount: 100_000, recurring: true, side: "expenditure", label: "recurring" },
    { year: 2029, amount: 100_000, recurring: false, side: "expenditure", label: "one-time" },
  ];
  const r = project(cfg, "flat");

  money(r.rows[0].expenditures, 1_000_000, "FY2027");
  money(r.rows[1].expenditures, 1_100_000 + 100_000, "FY2028 = growth + the recurring add");
  // FY2029: the recurring add is now inside the base and grows with it, plus a
  // one-time item on top.
  money(r.rows[2].expenditures, 1_200_000 * 1.1 + 100_000, "FY2029");
  // FY2030: the one-time item is gone; the recurring one is still compounding.
  money(r.rows[3].expenditures, 1_200_000 * 1.1 * 1.1, "FY2030");
});

test("the shipped deputy adjustments land in the years the config says", () => {
  const r = project(CONFIG, "none");
  const withAdj = r.rows.filter((row) => row.adjustments.some((a) => a.amount > 0));
  assert.deepEqual(withAdj.map((row) => row.year), [2028, 2029]);
  // The UFSA placeholder is carried but contributes nothing, by design.
  const ufsa = CONFIG.adjustments.find((a) => /Unified Fire/.test(a.label));
  assert.equal(ufsa.amount, 0);
  assert.ok(ufsa.confirm, "the UFSA placeholder must explain what needs confirming");
});

/* ========================================================================== */
/* 6. Validation.                                                             */
/* ========================================================================== */

test("impossible assumptions are refused rather than drawn", () => {
  const bad = [
    ["taxable_value", 0],
    ["taxable_value", -1],
    ["collection_rate", 0],
    ["collection_rate", 1.5],
    ["expenditures", 0],
    ["expenditure_growth", -1],
  ];
  bad.forEach(([field, value]) => {
    const r = project(CONFIG, "none", { overrides: { [field]: value } });
    assert.equal(r.ok, false, `${field}=${value} should be refused`);
    assert.ok(r.errors.some((e) => e.field === field), `${field}=${value} should name the field`);
    assert.equal(r.rows.length, 0, "a refused model draws nothing");
  });
});

test("an unknown scenario is an error, not an exception", () => {
  const r = project(CONFIG, "not-a-scenario");
  assert.equal(r.ok, false);
  assert.ok(r.errors.some((e) => e.field === "scenario"));
});

test("a reversed projection period is refused", () => {
  const cfg = toyConfig({ first_year: 2032, last_year: 2027 });
  assert.equal(project(cfg, "flat").ok, false);
});

test("extreme but survivable assumptions warn instead of failing", () => {
  const r = project(CONFIG, "none", { overrides: { expenditure_growth: 0.6 } });
  assert.equal(r.ok, true);
  assert.ok(r.warnings.some((w) => w.field === "expenditure_growth"));
});

test("non-numeric overrides fall back to the configured value", () => {
  const good = project(CONFIG, "none");
  ["", null, undefined, NaN, "6%", Infinity].forEach((junk) => {
    const r = project(CONFIG, "none", { overrides: { expenditure_growth: junk } });
    assert.ok(r.ok, `override ${String(junk)} should fall back`);
    money(r.summary.cumulative, good.summary.cumulative, `override ${String(junk)}`);
  });
});

test("validate() names missing baseline fields", () => {
  const cfg = toyConfig();
  delete cfg.baseline.expenditures;
  const { errors } = validate(cfg, globalThis.Projections.readBaseline(cfg, {}), scenarioById(cfg, "flat"));
  assert.ok(errors.some((e) => e.field === "expenditures"));
});

/* ========================================================================== */
/* 7. Precision.                                                              */
/* ========================================================================== */

test("full precision is kept internally — nothing is pre-rounded", () => {
  const cfg = toyConfig();
  cfg.baseline.taxable_value = { value: 1_000_000_003 };
  cfg.baseline.expenditure_growth = { value: 0.0333 };
  const r = project(cfg, "flat");
  assert.notEqual(r.rows[1].expenditures, Math.round(r.rows[1].expenditures));
  assert.notEqual(r.rows[0].propertyTax, Math.round(r.rows[0].propertyTax));
});
