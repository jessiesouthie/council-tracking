/* =============================================================================
   The fiscal engine behind projections.html.

   Pure arithmetic on data.projections.json: no DOM, no fetch, no formatting.
   The page draws whatever this returns and rounds only at the point of display,
   so the chart, the summary cards and the table can never disagree with each
   other — there is one calculation and three views of it.

   It is a classic script rather than a module because every other page on this
   site is (site.js exposes window.CT the same way), and projections.test.mjs
   evaluates this file directly so the tests exercise the shipped code rather
   than a copy of it.

   ---------------------------------------------------------------------------
   The one idea worth understanding before reading the loop
   ---------------------------------------------------------------------------
   Utah sets a city's property tax by revenue, not by rate. Each year the county
   computes a *certified rate*: the rate that raises the same dollars off
   property already on the roll as the city raised last year. Reassessment
   therefore does not raise revenue — the rate falls to cancel it. Only *new
   growth*, property that was not on the roll last year, adds money, and it adds
   it at whatever rate the city is levying.

   So the model carries taxable value in two halves every year:

       existing  = last year's roll, revalued        → revenue-neutral
       new       = new construction + named additions → additive revenue

   and "no tax increase" means levying the certified rate, which is a *falling*
   number. Holding the numeric rate constant instead is a different policy; a
   scenario can declare `method: "fixed_rate"` in the configuration to model it,
   though none of the shipped ones does.
   ============================================================================= */

(function (root) {
  "use strict";

  /* ---------------------------------------------------------------- helpers */

  const isNum = (x) => typeof x === "number" && Number.isFinite(x);

  // Baseline fields in the config are objects carrying their own sourcing
  // ({ value, derivation, confirm, … }); a few are bare numbers. Read either.
  const val = (x) => (x && typeof x === "object" && "value" in x ? x.value : x);

  const sum = (xs) => xs.reduce((a, b) => a + b, 0);

  /* ------------------------------------------------------------- assumptions
     Overrides come from the page's assumption controls. Only finite numbers
     win; anything else falls back to the config, so a half-typed input box
     ("0.0", "-", "") leaves the model on its last good value instead of
     producing NaN columns. */

  const BASE_FIELDS = [
    "taxable_value",
    "collection_rate",
    "other_revenue",
    "expenditures",
    "appreciation_rate",
    "new_growth_rate",
    "other_revenue_growth",
    "expenditure_growth",
  ];

  /* ------------------------------------------------- the data-center switch
     The largest single thing holding the projected gap down is $1.9bn of
     data-center value assumed onto the roll, and whether any of it lands there
     is not settled: value inside a community reinvestment area is subtracted
     from the certified-rate base, and every data center already in this city
     sits inside one. Modelling it as a fact would be asserting the answer, so
     the reader gets all three.

     "partial" is not half. It is the subset the record actually distinguishes:
     the buildings the city said on 6 August had declined incentives and would
     be "full taxpayers starting day one", which the config marks
     `incentive_free: true`. A 50% split would be a fourth invented number. */

  const ADDITION_MODES = ["full", "partial", "none"];

  function additionIncluded(addition, mode) {
    if (mode === "none") return false;
    if (mode === "partial") return addition.incentive_free === true;
    return true;
  }

  function readBaseline(config, overrides) {
    const b = (config && config.baseline) || {};
    const o = overrides || {};
    const out = {};
    BASE_FIELDS.forEach((k) => {
      out[k] = isNum(o[k]) ? o[k] : val(b[k]);
    });
    out.certified_rate = val(b.certified_rate); // informational, anchor year only
    return out;
  }

  /* --------------------------------------------------------------- validation
     Two severities. `errors` mean the model cannot be trusted and the page
     refuses to draw a chart; `warnings` mean it will draw but the reader is
     told why the result may be strange. Everything is checked against the
     merged baseline, so a bad override is caught the same way a bad config
     field would be. */

  function validate(config, base, scenario) {
    const errors = [];
    const warnings = [];
    const err = (field, message) => errors.push({ field, message });
    const warn = (field, message) => warnings.push({ field, message });

    const first = config && config.first_year;
    const last = config && config.last_year;

    if (!Number.isInteger(first) || !Number.isInteger(last)) {
      err("years", "The projection period is missing or not a pair of whole years.");
    } else if (last < first) {
      err("years", `The projection period ends (${last}) before it begins (${first}).`);
    } else if (last - first > 30) {
      err("years", "The projection period is longer than 30 years, which this model is not built for.");
    }

    if (!scenario) err("scenario", "No scenario with that id exists in the configuration.");

    if (!isNum(base.taxable_value) || base.taxable_value <= 0)
      err("taxable_value", "Taxable assessed value must be a positive number.");

    if (!isNum(base.collection_rate) || base.collection_rate <= 0 || base.collection_rate > 1)
      err("collection_rate", "The collection rate must be greater than 0 and no more than 1 (100%).");

    if (!isNum(base.other_revenue) || base.other_revenue < 0)
      err("other_revenue", "Other city revenue must be zero or a positive number.");

    if (!isNum(base.expenditures) || base.expenditures <= 0)
      err("expenditures", "Baseline operating expenditure must be a positive number.");

    [
      ["appreciation_rate", "Taxable-value growth"],
      ["new_growth_rate", "New growth"],
      ["other_revenue_growth", "Other-revenue growth"],
      ["expenditure_growth", "Expenditure growth"],
    ].forEach(([k, label]) => {
      const v = base[k];
      if (!isNum(v)) err(k, `${label} must be a number.`);
      else if (v <= -1) err(k, `${label} of ${(v * 100).toFixed(1)}% would wipe out the base in a single year.`);
      else if (v < -0.25 || v > 0.5)
        warn(k, `${label} is set to ${(v * 100).toFixed(1)}% a year, which is far outside anything in the record.`);
    });

    if (scenario) {
      const r = scenario.start_rate;
      if (!isNum(r) || r <= 0) err("start_rate", "The scenario's starting rate must be a positive number.");
      else if (r > 0.05) warn("start_rate", "The starting rate is above 0.05, which is not a plausible city levy.");

      Object.keys(scenario.years || {}).forEach((y) => {
        const spec = scenario.years[y] || {};
        if ("rate_override" in spec && (!isNum(spec.rate_override) || spec.rate_override < 0))
          err("rate_override", `The rate set for ${y} is not a valid rate.`);
        if ("above_certified" in spec && !isNum(spec.above_certified))
          err("above_certified", `The above-certified step for ${y} is not a number.`);
      });
    }

    return { errors, warnings };
  }

  /* ------------------------------------------------------------------- model */

  function scenarioById(config, id) {
    return ((config && config.scenarios) || []).find((s) => s.id === id) || null;
  }

  /**
   * Run one scenario across the configured years.
   *
   * @param {object} config     parsed data.projections.json
   * @param {string} scenarioId which scenario to run
   * @param {object} [opts]
   * @param {object} [opts.overrides] numeric overrides for any baseline field
   * @param {string} [opts.additions]  "full" (default), "partial" or "none" —
   *                                   how much of the named data-center value
   *                                   is assumed to reach the city's own roll
   * @returns {{ok, errors, warnings, rows, summary, crossings, scenario, method, additions}}
   */
  function project(config, scenarioId, opts) {
    const options = opts || {};
    const additionMode = ADDITION_MODES.includes(options.additions) ? options.additions : "full";
    const scenario = scenarioById(config, scenarioId);
    const base = readBaseline(config, options.overrides);
    const { errors, warnings } = validate(config, base, scenario);

    if (errors.length) {
      return {
        ok: false, errors, warnings, rows: [], summary: null, crossings: [],
        scenario, method: null, additions: additionMode,
      };
    }

    const method = scenario.method || "revenue_based";
    const first = config.first_year;
    const last = config.last_year;
    const labels = config.year_labels || {};
    const collection = base.collection_rate;

    // Named taxable-value additions (the data centers), keyed by year.
    const additions = {};
    (config.value_additions || []).forEach((a) => {
      if (!isNum(a.amount) || !Number.isInteger(a.year)) return;
      if (!additionIncluded(a, additionMode)) return;
      additions[a.year] = (additions[a.year] || 0) + a.amount;
    });

    // Named budget adjustments, split by side and by whether they persist.
    const adjustmentsFor = (year, side) =>
      (config.adjustments || []).filter(
        (a) => a.year === year && (a.side || "expenditure") === side && isNum(a.amount)
      );

    const rows = [];
    let prevRevenue = 0;     // property-tax revenue carried into the certified-rate calc
    let prevValue = 0;
    let otherRevenue = 0;
    let expenditureBase = 0; // expenditure less any one-time items, so growth compounds correctly
    let cumulative = 0;

    for (let year = first; year <= last; year++) {
      const anchor = year === first;

      let existingValue, newGrowthValue, addition, taxableValue, certifiedRate, effectiveRate;

      if (anchor) {
        // The anchor year is given, not projected: it is the city's own adopted
        // figures. Splitting it into existing/new would be inventing a history.
        existingValue = base.taxable_value;
        newGrowthValue = 0;
        addition = 0;
        taxableValue = base.taxable_value;
        certifiedRate = isNum(base.certified_rate) ? base.certified_rate : null;
        effectiveRate = scenario.start_rate;
        otherRevenue = base.other_revenue;
        expenditureBase = base.expenditures;
      } else {
        existingValue = prevValue * (1 + base.appreciation_rate);
        addition = additions[year] || 0;
        newGrowthValue = prevValue * base.new_growth_rate + addition;
        taxableValue = existingValue + newGrowthValue;

        // The certified rate: what it takes to raise last year's dollars off
        // property that was already there. Division by existing value only —
        // that exclusion of new growth is the whole mechanism.
        certifiedRate = prevRevenue / (existingValue * collection);

        const spec = (scenario.years || {})[String(year)] || {};
        if (method === "fixed_rate") {
          effectiveRate = scenario.start_rate;
        } else if (isNum(spec.rate_override)) {
          effectiveRate = spec.rate_override;
        } else {
          const step = isNum(spec.above_certified)
            ? spec.above_certified
            : isNum(scenario.default_above_certified)
              ? scenario.default_above_certified
              : 0;
          effectiveRate = certifiedRate * (1 + step);
        }

        otherRevenue = otherRevenue * (1 + base.other_revenue_growth);
        expenditureBase = expenditureBase * (1 + base.expenditure_growth);
      }

      // Named adjustments. Recurring ones join the base and compound from here;
      // one-time ones sit on top of this year only.
      const expRecurring = adjustmentsFor(year, "expenditure").filter((a) => a.recurring);
      const expOneTime = adjustmentsFor(year, "expenditure").filter((a) => !a.recurring);
      const revRecurring = adjustmentsFor(year, "revenue").filter((a) => a.recurring);
      const revOneTime = adjustmentsFor(year, "revenue").filter((a) => !a.recurring);

      expenditureBase += sum(expRecurring.map((a) => a.amount));
      otherRevenue += sum(revRecurring.map((a) => a.amount));

      const oneTimeExp = sum(expOneTime.map((a) => a.amount));
      const oneTimeRev = sum(revOneTime.map((a) => a.amount));

      const propertyTax = taxableValue * effectiveRate * collection;
      const totalRevenue = propertyTax + otherRevenue + oneTimeRev;
      const expenditures = expenditureBase + oneTimeExp;
      const gap = totalRevenue - expenditures;
      cumulative += gap;

      rows.push({
        year,
        label: labels[String(year)] || `FY${year}`,
        anchor,
        existingValue,
        newGrowthValue,
        valueAddition: addition,
        taxableValue,
        certifiedRate,
        effectiveRate,
        // How far above (or below) the certified rate this year's levy sits —
        // the figure a Truth-in-Taxation notice has to carry. Reported for the
        // anchor year too, where the certified rate is the county's published
        // one: 0.001500 against 0.000530 comes out at +183.02%, which is the
        // percentage in the motion the council passed, to the second decimal.
        aboveCertified: certifiedRate ? effectiveRate / certifiedRate - 1 : null,
        propertyTax,
        otherRevenue,
        oneTimeRevenue: oneTimeRev,
        totalRevenue,
        expenditures,
        oneTimeExpenditure: oneTimeExp,
        adjustments: adjustmentsFor(year, "expenditure").concat(adjustmentsFor(year, "revenue")),
        gap,
        cumulative,
      });

      prevRevenue = propertyTax;
      prevValue = taxableValue;
    }

    return {
      ok: true,
      errors,
      warnings,
      rows,
      crossings: findCrossings(rows),
      summary: summarise(rows),
      scenario,
      method,
      additions: additionMode,
    };
  }

  /* --------------------------------------------------------------- crossings
     Where the revenue line passes through the expenditure line. The two series
     are piecewise linear between fiscal years, so the crossing is exact
     arithmetic rather than something to eyeball off the chart — and the chart
     uses these same points to split its green and red fills, which is why they
     live here and not in the drawing code.

     A year that lands exactly on zero is a crossing only if the sign genuinely
     changes across it; a series that touches zero and comes back is not. */

  function findCrossings(rows) {
    const out = [];
    for (let i = 0; i < rows.length - 1; i++) {
      const a = rows[i].gap;
      const b = rows[i + 1].gap;
      if (a === 0 || b === 0) continue;
      if (a > 0 === b > 0) continue;
      const t = a / (a - b); // 0 → at rows[i], 1 → at rows[i + 1]
      out.push({
        fromIndex: i,
        toIndex: i + 1,
        fromYear: rows[i].year,
        toYear: rows[i + 1].year,
        fraction: t,
        into: b < 0 ? "deficit" : "surplus",
      });
    }
    return out;
  }

  /* ---------------------------------------------------------------- summary */

  function summarise(rows) {
    if (!rows.length) return null;
    const firstRow = rows[0];
    const lastRow = rows[rows.length - 1];
    const deficits = rows.filter((r) => r.gap < 0);
    const worst = rows.reduce((a, r) => (r.gap < a.gap ? r : a), rows[0]);

    return {
      firstYear: firstRow.year,
      firstLabel: firstRow.label,
      lastYear: lastRow.year,
      lastLabel: lastRow.label,
      propertyTax: firstRow.propertyTax,
      totalRevenue: firstRow.totalRevenue,
      expenditures: firstRow.expenditures,
      gap: firstRow.gap,
      cumulative: lastRow.cumulative,
      deficitYears: deficits.length,
      worstYear: worst.year,
      worstLabel: worst.label,
      worstGap: worst.gap,
      // The rate the levy would have to reach in the final year to close that
      // year's gap on property tax alone — a scale check, not a proposal.
      closingRate:
        lastRow.gap < 0 && lastRow.taxableValue > 0
          ? lastRow.effectiveRate + -lastRow.gap / lastRow.taxableValue
          : null,
    };
  }

  /* ------------------------------------------------------------------ export */

  const api = {
    project, validate, findCrossings, summarise, scenarioById, readBaseline,
    BASE_FIELDS, ADDITION_MODES,
  };

  root.Projections = api;
  if (typeof module === "object" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
