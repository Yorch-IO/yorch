import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import en from "./en.json";
import es from "./es.json";

/** Every non-test source file under src/, concatenated.
 *
 * Rooted at the vitest project directory rather than `__dirname`, which under
 * ESM does not point where it looks like it does. That mattered: the first
 * version of this scan silently read nothing and reported every key unused.
 */
function sourceText(dir: string = join(process.cwd(), "src")): string {
  let out = "";
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) {
      out += sourceText(path);
    } else if (/\.tsx?$/.test(entry.name) && !entry.name.includes(".test.")) {
      out += readFileSync(path, "utf8");
    }
  }
  return out;
}

/** Every leaf path in a nested resource bundle, dot-joined. */
function keyPaths(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) return [prefix];
  return Object.entries(value as Record<string, unknown>).flatMap(([k, v]) =>
    keyPaths(v, prefix ? `${prefix}.${k}` : k),
  );
}

describe("translation bundles", () => {
  // A missing key falls back to English silently, so a Spanish user sees a
  // half-translated screen with no error anywhere. The only cheap defence is
  // demanding the two bundles have identical shapes.
  it("define exactly the same keys", () => {
    const enKeys = keyPaths(en).sort();
    const esKeys = keyPaths(es).sort();

    expect(esKeys.filter((k) => !enKeys.includes(k))).toEqual([]);
    expect(enKeys.filter((k) => !esKeys.includes(k))).toEqual([]);
  });

  it("leave no value empty", () => {
    for (const [name, bundle] of [
      ["en", en],
      ["es", es],
    ] as const) {
      const empty = keyPaths(bundle).filter((path) => {
        const value = path
          .split(".")
          .reduce<unknown>((acc, k) => (acc as Record<string, unknown>)?.[k], bundle);
        return typeof value !== "string" || value.trim() === "";
      });
      expect(empty, `${name} has empty values`).toEqual([]);
    }
  });

  it("define no key the UI never asks for", () => {
    // A dead key is how a bundle rots: it survives every parity check, gets
    // dutifully translated, and hides that the feature it was written for was
    // never wired up. This test found exactly that — `docker.install` existed
    // in both languages while the Docker-missing screen offered no link.
    const src = sourceText();
    // A scan that reads nothing would report every key unused — or, with the
    // assertion inverted, pass forever. Prove it actually found the sources.
    expect(src).toContain('t("stack.title")');

    const isUsed = (path: string): boolean => {
      if (src.includes(path)) return true;
      // Keys reached dynamically, e.g. t(`stack.state.${s.state}`): treat the
      // whole group as used when the source interpolates on its prefix.
      const prefix = path.slice(0, path.lastIndexOf("."));
      return prefix.length > 0 && src.includes(`${prefix}.\${`);
    };

    expect(keyPaths(en).filter((path) => !isUsed(path))).toEqual([]);
  });

  it("use the same interpolation placeholders in both languages", () => {
    // `t("docker.found", {docker, compose})` breaks silently if one bundle
    // names the placeholders differently.
    const placeholders = (bundle: unknown, path: string): string[] => {
      const value = path
        .split(".")
        .reduce<unknown>((acc, k) => (acc as Record<string, unknown>)?.[k], bundle);
      return [...String(value).matchAll(/\{\{(\w+)\}\}/g)].map((m) => m[1] as string).sort();
    };

    for (const path of keyPaths(en)) {
      expect(placeholders(es, path), `placeholders differ at ${path}`).toEqual(
        placeholders(en, path),
      );
    }
  });
});
