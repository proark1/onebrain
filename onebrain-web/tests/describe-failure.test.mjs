import assert from "node:assert/strict";
import test from "node:test";

import { describeFailure } from "../src/lib/describe-failure.ts";

const json = (status, body, statusText = "") =>
  new Response(JSON.stringify(body), { status, statusText });

test("an API-raised failure is reported by its own detail", async () => {
  const message = await describeFailure("/api/auth/login", json(401, {
    detail: "Invalid email or password.",
  }));
  assert.equal(message, "Invalid email or password.");
});

test("a validation failure names the field rule instead of the status code", async () => {
  // FastAPI reports a Pydantic rejection as an array, not a string. Reading
  // only the string form collapsed every 422 to "422 Unprocessable Entity",
  // which on the password-change panel told the user nothing actionable --
  // `new_password` has a min_length of 12, so this is the common case there.
  const message = await describeFailure("/api/auth/password", json(422, {
    detail: [{
      type: "string_too_short",
      loc: ["body", "new_password"],
      msg: "String should have at least 12 characters",
      input: "hunter2",
    }],
  }, "Unprocessable Entity"));
  assert.equal(message, "String should have at least 12 characters");
});

test("a validation failure never echoes what the user typed", async () => {
  // `input` carries the rejected value -- on this surface, a password.
  const message = await describeFailure("/api/auth/password", json(422, {
    detail: [{ msg: "String should have at least 12 characters", input: "hunter2" }],
  }));
  assert.ok(!message.includes("hunter2"), message);
});

test("multiple validation failures are all reported", async () => {
  const message = await describeFailure("/api/auth/password", json(422, {
    detail: [{ msg: "Field required" }, { msg: "String should have at least 12 characters" }],
  }));
  assert.equal(message, "Field required; String should have at least 12 characters");
});

test("an edge failure with no JSON body still names the status and path", async () => {
  const response = new Response("<html>502 Bad Gateway</html>", {
    status: 502,
    statusText: "Bad Gateway",
  });
  assert.equal(
    await describeFailure("/api/spaces?account_id=acme", response),
    "502 Bad Gateway (/api/spaces)",
  );
});

test("a JSON body carrying no usable detail falls back rather than echoing it", async () => {
  assert.equal(
    await describeFailure("/api/spaces", json(500, { detail: [{ type: "unknown" }] })),
    "500 (/api/spaces)",
  );
});
