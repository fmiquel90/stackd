import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, apiFile, attachmentName } from "./client";

function respond(status: number, body: unknown, headers: Record<string, string> = {}) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(typeof body === "string" ? body : JSON.stringify(body), {
          status,
          headers: { "content-type": "application/problem+json", ...headers },
        }),
    ),
  );
}

afterEach(() => vi.unstubAllGlobals());

describe("API errors (problem+json)", () => {
  it("keeps a 422's field errors — where and what — but never the offending input", async () => {
    respond(422, {
      title: "Validation error",
      detail: "Request payload failed validation.",
      errors: [
        { loc: ["body", "stacks", 0, "variables", 0, "value"], msg: "bad", input: "hunter2", type: "x" },
        { nonsense: true },
      ],
    });
    const err = (await api("/spaces/s/import?dry_run=true", { body: {} }).catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.errors).toEqual([{ loc: ["body", "stacks", 0, "variables", 0, "value"], msg: "bad" }]);
    expect(JSON.stringify(err.errors)).not.toContain("hunter2");
  });

  it("has no field list on other refusals", async () => {
    respond(403, { title: "Forbidden", detail: "Space admin required." });
    const err = (await api("/spaces/s/export").catch((e) => e)) as ApiError;
    expect(err.detail).toBe("Space admin required.");
    expect(err.errors).toBeUndefined();
  });
});

describe("downloads", () => {
  it("reads the file name from Content-Disposition", () => {
    expect(attachmentName('attachment; filename="stackd-payments-20261106T120000Z.json"')).toBe(
      "stackd-payments-20261106T120000Z.json",
    );
    expect(
      attachmentName(
        "attachment; filename=\"stackd-space-x.json\"; filename*=UTF-8''stackd-%E7%94%9F%E4%BA%A7-x.json",
      ),
    ).toBe("stackd-生产-x.json");
    expect(attachmentName("attachment")).toBeNull();
    expect(attachmentName(null)).toBeNull();
  });

  it("returns the body and the server's file name", async () => {
    respond(
      200,
      { format: "stackd.config" },
      { "content-type": "application/json", "content-disposition": 'attachment; filename="x.json"' },
    );
    const { blob, filename } = await apiFile("/spaces/s/export");
    expect(filename).toBe("x.json");
    expect(JSON.parse(await blob.text())).toEqual({ format: "stackd.config" });
  });
});
