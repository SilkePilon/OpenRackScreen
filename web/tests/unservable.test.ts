import { describe, expect, it } from "vitest";
import { parseUnservable } from "../src/api/unservable";

describe("the header that says an edit did not reach a rack", () => {
  it("reads a list of ids", () => {
    expect(parseUnservable(new Headers({ "X-Unservable-Daemons": "3,7" }))).toEqual([3, 7]);
  });

  it("is empty when the header is absent, which means every rack got it", () => {
    expect(parseUnservable(new Headers())).toEqual([]);
  });

  it("reads it off a 201, because a create carries it too", () => {
    // The status code is not the signal. A create answers 201 and sets the
    // header when the row exists but no rack could be given it.
    const response = new Response(null, {
      status: 201,
      headers: { "X-Unservable-Daemons": "4" },
    });
    expect(parseUnservable(response.headers)).toEqual([4]);
  });

  it("survives whitespace and a single id", () => {
    expect(parseUnservable(new Headers({ "X-Unservable-Daemons": " 12 " }))).toEqual([12]);
  });

  it("ignores anything that is not a number rather than yielding NaN", () => {
    expect(parseUnservable(new Headers({ "X-Unservable-Daemons": "3,,x,7" }))).toEqual([3, 7]);
  });
});
