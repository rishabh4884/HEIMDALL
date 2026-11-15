import { test } from "node:test";
import assert from "node:assert/strict";
import { atlasPoint, equatorialToGalactic, hammer, projectSky, tileSample } from "../heimdall/web/hips.js";
import { atlasRadii, openProjection, closeProjection } from "../heimdall/web/sky.js";

test("Hammer round trips known equatorial positions, including across the RA seam", () => {
  for (const [ra, dec] of [[266.405, -28.936], [0.001, 20], [359.999, -30], [150.1, 2.2]]) {
    const [gotRA, gotDec] = atlasPoint(...hammer(...equatorialToGalactic(ra, dec)));
    const difference = ((gotRA - ra + 540) % 360) - 180;
    assert.ok(Math.abs(difference) < 1e-6);
    assert.ok(Math.abs(gotDec - dec) < 1e-6);
  }
  assert.equal(atlasPoint(1, 1), null);
});

test("HEALPix NESTED tile agrees with the IRSA tutorial's example", () => {
  const [index, x, y] = tileSample(60.8109955, 51.3404038, 5, 9);
  assert.equal(index, 445);
  assert.ok(x >= 0 && x < 512 && y >= 0 && y < 512);
});

test("overview and high-resolution pixels address the same HEALPix parent", () => {
  for (const [ra, dec] of [[60.81, 51.34], [266.405, -28.936], [270, -89.5], [0, 0]]) {
    const [coarse] = tileSample(ra, dec, 3, 6);
    const [fine] = tileSample(ra, dec, 5, 9);
    assert.equal(coarse, Math.floor(fine / 16));
  }
});

test("the entire all-sky ellipse fits wide, narrow and short viewports", () => {
  for (const [width, height] of [[1400, 680], [360, 540], [940, 220], [310, 160]]) {
    const { rx, ry } = atlasRadii(width, height);
    assert.ok(2 * rx <= width - 19);
    assert.ok(2 * ry <= height - 19);
    assert.equal(rx, 2 * ry);
  }
});

test("a recentered zoom keeps projection and image sampling aligned across the RA seam", () => {
  const center = equatorialToGalactic(359.9, 38);
  const [ra, dec] = atlasPoint(...projectSky(.1, 38.2, ...center), ...center);
  assert.ok(Math.abs(ra - .1) < 1e-6);
  assert.ok(Math.abs(dec - 38.2) < 1e-6);
});

test("the full-sky outline opens smoothly into a field view", () => {
  assert.deepEqual(openProjection(.9, 0, 0), [.9, 0]);
  assert.ok(openProjection(.9, 0, .8)[0] > 2);
  for (const zoom of [0, .1, .45, .8, .999]) {
    const [x, y] = closeProjection(...openProjection(.65, -.4, zoom), zoom);
    assert.ok(Math.abs(x - .65) < 1e-10 && Math.abs(y + .4) < 1e-10);
  }
});
