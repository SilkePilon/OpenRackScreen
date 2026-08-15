import { describe, expect, expectTypeOf, it } from "vitest";
import { detailFrom } from "../src/api/client";
import type { paths } from "../src/api/schema";

describe("the generated schema", () => {
  it("types a daemon rather than leaving it an object", () => {
    type Daemon =
      paths["/api/daemons"]["get"]["responses"][200]["content"]["application/json"][number];
    const daemon: Daemon = {
      id: 4,
      name: "pi-rack",
      status: "paired",
      online: true,
      config_version: 7,
      applied_version: 7,
      config_error: null,
      version: "0.1.0",
      last_seen: null,
      created_at: "2026-08-15T00:00:00+00:00",
      paired_at: null,
      capabilities: {},
    };
    expect(daemon.applied_version).toBe(7);
    // The literal above only proves the response model still exists. A field
    // that degraded to `unknown` -- a pydantic type widened to `Any`, say --
    // would still accept `7` and still pass that assertion. This is the part
    // that notices.
    expectTypeOf<Daemon["applied_version"]>().toEqualTypeOf<number | null>();
  });

  it("knows a pairing token is only ever on the create response", () => {
    type Created =
      paths["/api/daemons"]["post"]["responses"][201]["content"]["application/json"];
    type Listed =
      paths["/api/daemons"]["get"]["responses"][200]["content"]["application/json"][number];

    const created: Created = {} as Created;
    expectTypeOf(created).toHaveProperty("token");
    expectTypeOf({} as Listed).not.toHaveProperty("token");
  });
});

describe("what the server said when it refused", () => {
  it("hands back the sentence a route wrote itself", () => {
    expect(detailFrom({ detail: "a screen may appear once in a reorder" })).toBe(
      "a screen may appear once in a reorder",
    );
  });

  it("reads a validation report as the fields it is about", () => {
    // FastAPI's own 422 shape, and the one every schema refusal in this project
    // takes: `DisplayConfig`'s two rules are a `model_validator`, so clearing
    // the DC box on a gc9a01 arrives here and nowhere else. `loc` leads with the
    // part of the request the value came from, which is `body` for every field
    // on every form in this interface and says nothing to the person looking at
    // one; what is left is the path to the box.
    expect(
      detailFrom({
        detail: [
          {
            type: "value_error",
            loc: ["body", "display"],
            msg: "Value error, a gc9a01 display needs dc",
          },
        ],
      }),
    ).toBe("display: Value error, a gc9a01 display needs dc");
  });

  it("names every field a report names, not just the first", () => {
    expect(
      detailFrom({
        detail: [
          { type: "greater_than_equal", loc: ["body", "position"], msg: "Input should be >= 1" },
          { type: "string_too_short", loc: ["body", "name"], msg: "String should have at least 1 character" },
        ],
      }),
    ).toBe("position: Input should be >= 1; name: String should have at least 1 character");
  });

  it("keeps a `loc` that names only the request part, having nothing better", () => {
    // `extra="forbid"` on the whole body, or a body that is not a document at
    // all: there is no field to point at, and "body" is the most this can
    // honestly say about where.
    expect(detailFrom({ detail: [{ loc: ["body"], msg: "Extra inputs are not permitted" }] })).toBe(
      "body: Extra inputs are not permitted",
    );
  });

  it("says nothing rather than something invented", () => {
    // Each of these is a body a proxy or a bug can produce, and each must leave
    // the caller's own sentence standing rather than rendering as `[object
    // Object]`, `undefined`, or an empty string.
    expect(detailFrom({ detail: [] })).toBeNull();
    expect(detailFrom({ detail: "" })).toBeNull();
    expect(detailFrom({ detail: null })).toBeNull();
    expect(detailFrom({ detail: [{ loc: ["body", "name"] }] })).toBeNull();
    expect(detailFrom({ detail: [42, "not an error"] })).toBeNull();
    expect(detailFrom("<html>502 Bad Gateway</html>")).toBeNull();
    expect(detailFrom(undefined)).toBeNull();
  });
});
