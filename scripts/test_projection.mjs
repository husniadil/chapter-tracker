/**
 * Unit tests for the release projection in docs/index.html.
 *
 * The functions are pulled out of the page source between the projection
 * markers and evaluated as-is, so these tests exercise the code that actually
 * ships rather than a copy of it.
 *
 * Run: node scripts/test_projection.mjs
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import vm from "node:vm";
import assert from "node:assert/strict";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const page = readFileSync(join(root, "docs", "index.html"), "utf8");

const block = page.match(/\/\* === projection:begin ===[\s\S]*?\/\* === projection:end === \*\//);
assert.ok(block, "projection block not found in docs/index.html");

const context = vm.createContext({});
vm.runInContext(block[0] + "\n;({ projectNextRelease, resolveNextRelease, weeklyRunLength })", context);
const { projectNextRelease, resolveNextRelease, weeklyRunLength } = vm.runInContext(
  "({ projectNextRelease, resolveNextRelease, weeklyRunLength })",
  context
);

/* MANGA Plus releases One Piece at 15:00 UTC on Sundays. */
const SUN_MAY_03 = "2026-05-03T15:00:00Z";
const SUN_MAY_10 = "2026-05-10T15:00:00Z";
const SUN_MAY_17 = "2026-05-17T15:00:00Z";
const SUN_MAY_24 = "2026-05-24T15:00:00Z";
const SUN_MAY_31 = "2026-05-31T15:00:00Z";
const SUN_APR_26 = "2026-04-26T15:00:00Z";
const SUN_JUN_14 = "2026-06-14T15:00:00Z";

const at = (iso) => Date.parse(iso);
const isoOf = (ms) => new Date(ms).toISOString().replace(".000Z", "Z");

let failures = 0;
function test(name, fn) {
  try {
    fn();
    console.log(`ok   ${name}`);
  } catch (error) {
    failures++;
    console.error(`FAIL ${name}\n     ${error.message}`);
  }
}

test("normal week: two weeks running projects the next Sunday", () => {
  const result = resolveNextRelease({
    latest_chapter: 1150,
    latest_release_utc: SUN_MAY_10,
    next_chapter: 1151,
    next_release_utc: null,
    next_confirmed: false,
    recent_releases_utc: [SUN_MAY_03, SUN_MAY_10]
  });
  assert.equal(isoOf(result.at), SUN_MAY_17);
  assert.equal(result.source, "projected-weekly");
  assert.equal(result.confirmed, false);
  assert.equal(result.breakWeek, false);
});

test("Oda break: three weeks running projects a skipped week", () => {
  const result = resolveNextRelease({
    latest_chapter: 1151,
    latest_release_utc: SUN_MAY_17,
    next_chapter: 1152,
    next_release_utc: null,
    next_confirmed: false,
    recent_releases_utc: [SUN_MAY_03, SUN_MAY_10, SUN_MAY_17]
  });
  assert.equal(isoOf(result.at), SUN_MAY_31);
  assert.equal(result.source, "projected-break");
  assert.equal(result.breakWeek, true);
  assert.equal(result.gapDays, 14);
});

test("magazine break: a break already served resets the run to weekly", () => {
  // Jump skipped a week between Apr 26 and May 10, so the two releases since
  // then are all the rotation has run: the next week is back on.
  const result = resolveNextRelease({
    latest_chapter: 1151,
    latest_release_utc: SUN_MAY_17,
    next_chapter: 1152,
    next_release_utc: null,
    next_confirmed: false,
    recent_releases_utc: [SUN_APR_26, SUN_MAY_10, SUN_MAY_17]
  });
  assert.equal(isoOf(result.at), SUN_MAY_24);
  assert.equal(result.source, "projected-weekly");
  assert.equal(result.breakWeek, false);
});

test("confirmed date overrides the projection", () => {
  // The rotation would project May 24, but MANGA Plus announced June 14.
  const schedule = {
    latest_chapter: 1151,
    latest_release_utc: SUN_MAY_17,
    next_chapter: 1152,
    next_release_utc: SUN_JUN_14,
    next_confirmed: true,
    recent_releases_utc: [SUN_APR_26, SUN_MAY_10, SUN_MAY_17]
  };
  assert.equal(isoOf(projectNextRelease(at(SUN_MAY_17), schedule.recent_releases_utc.map(at)).at), SUN_MAY_24);

  const result = resolveNextRelease(schedule);
  assert.equal(isoOf(result.at), SUN_JUN_14);
  assert.equal(result.source, "confirmed");
  assert.equal(result.confirmed, true);
  assert.equal(result.breakWeek, true);
});

test("no history at all falls back to weekly", () => {
  const result = resolveNextRelease({
    latest_release_utc: SUN_MAY_10,
    next_confirmed: false,
    next_release_utc: null
  });
  assert.equal(isoOf(result.at), SUN_MAY_17);
  assert.equal(result.source, "projected-weekly");
});

test("next_confirmed without a usable date falls back to the projection", () => {
  const result = resolveNextRelease({
    latest_release_utc: SUN_MAY_10,
    next_confirmed: true,
    next_release_utc: null,
    recent_releases_utc: [SUN_MAY_03, SUN_MAY_10]
  });
  assert.equal(isoOf(result.at), SUN_MAY_17);
  assert.equal(result.confirmed, false);
});

test("weeklyRunLength ignores duplicates and unparseable entries", () => {
  assert.equal(weeklyRunLength([at(SUN_MAY_03), at(SUN_MAY_03), at(SUN_MAY_10), NaN]), 2);
  assert.equal(weeklyRunLength([]), 0);
});

test("weekly projection crosses into the next year", () => {
  const result = resolveNextRelease({
    latest_chapter: 1230,
    latest_release_utc: "2026-12-27T15:00:00Z",
    next_chapter: 1231,
    next_release_utc: null,
    next_confirmed: false,
    recent_releases_utc: ["2026-12-20T15:00:00Z", "2026-12-27T15:00:00Z"]
  });
  assert.equal(isoOf(result.at), "2027-01-03T15:00:00Z");
  assert.equal(result.source, "projected-weekly");
});

test("break projection crosses into the next year", () => {
  const result = resolveNextRelease({
    latest_chapter: 1230,
    latest_release_utc: "2026-12-27T15:00:00Z",
    next_chapter: 1231,
    next_release_utc: null,
    next_confirmed: false,
    recent_releases_utc: [
      "2026-12-13T15:00:00Z",
      "2026-12-20T15:00:00Z",
      "2026-12-27T15:00:00Z"
    ]
  });
  assert.equal(isoOf(result.at), "2027-01-10T15:00:00Z");
  assert.equal(result.source, "projected-break");
  assert.equal(result.gapDays, 14);
});

test("a confirmed date in the next year is used verbatim", () => {
  const result = resolveNextRelease({
    latest_chapter: 1230,
    latest_release_utc: "2026-12-20T15:00:00Z",
    next_chapter: 1231,
    next_release_utc: "2027-01-17T15:00:00Z",
    next_confirmed: true,
    recent_releases_utc: ["2026-12-13T15:00:00Z", "2026-12-20T15:00:00Z"]
  });
  assert.equal(isoOf(result.at), "2027-01-17T15:00:00Z");
  assert.equal(result.confirmed, true);
  assert.equal(result.gapDays, 28);
});

test("projection spans February in a leap year", () => {
  // 2028-02-20 plus 14 days lands on 2028-03-05 only if the 29th exists.
  const result = resolveNextRelease({
    latest_release_utc: "2028-02-20T15:00:00Z",
    next_confirmed: false,
    next_release_utc: null,
    recent_releases_utc: [
      "2028-02-06T15:00:00Z",
      "2028-02-13T15:00:00Z",
      "2028-02-20T15:00:00Z"
    ]
  });
  assert.equal(isoOf(result.at), "2028-03-05T15:00:00Z");
  assert.equal(result.source, "projected-break");
});

test("gaps are measured in real elapsed time, not calendar arithmetic", () => {
  // Every one of these is exactly seven days apart across a year boundary.
  assert.equal(weeklyRunLength([
    at("2026-12-20T15:00:00Z"),
    at("2026-12-27T15:00:00Z"),
    at("2027-01-03T15:00:00Z")
  ]), 3);
  // A month boundary is not a gap: Jan 31 to Feb 7 is still one week.
  assert.equal(weeklyRunLength([
    at("2027-01-31T15:00:00Z"),
    at("2027-02-07T15:00:00Z")
  ]), 2);
});

test("the live schedule.json parses and resolves", () => {
  const live = JSON.parse(readFileSync(join(root, "docs", "schedule.json"), "utf8"));
  const result = resolveNextRelease(live);
  assert.ok(result, "live schedule.json did not resolve");
  assert.ok(isFinite(result.at), "resolved release time is not a number");
});

console.log(failures === 0 ? "\nall tests passed" : `\n${failures} test(s) failed`);
process.exit(failures === 0 ? 0 : 1);
