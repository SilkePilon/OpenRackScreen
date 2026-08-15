import { describe, expect, expectTypeOf, it } from "vitest";
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
