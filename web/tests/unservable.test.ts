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

  it("takes decimal ids only, and not the other things Number would read", () => {
    // `Number` reads "0x10" as 16, "1e3" as 1000 and "" as 0, and
    // `Number.isInteger` agrees with every one of them -- so a parser that
    // decided on the number rather than on the text would name racks 16, 1000
    // and 0 for a header that named none of them. The server writes decimal
    // ids and nothing else, so this reads decimal ids and nothing else.
    const odd = "0x10,1e3,-4,+9,3.0, 13 ,8";
    // 13 before 8, so a parser that sorted would fail here too: the order is
    // the server's, and the server already sends them ascending.
    expect(parseUnservable(new Headers({ "X-Unservable-Daemons": odd }))).toEqual([13, 8]);
  });
});
