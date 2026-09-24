import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  getActiveContext,
  isActive,
  platformHeaders,
  resetActiveContext,
  resolveInitial,
  setActiveContext,
  subscribe,
  type PlatformConnection,
} from "./platformContext";
import { isDatabricksComputeRequest } from "./databricksAgentApi";

/**
 * The active platform context is the single source of truth for which platform
 * the application is in. These tests pin down that it reaches the backend on
 * every request and that switching genuinely changes context rather than just
 * a label.
 */

const DATABRICKS: PlatformConnection = {
  id: "db-prod",
  platform: "databricks",
  name: "Production Lakehouse",
  status: "connected",
};
const FABRIC: PlatformConnection = {
  id: "fabric-prod",
  platform: "fabric",
  name: "Fabric Workspace",
  status: "connected",
};

beforeEach(() => {
  resetActiveContext();
});

describe("active platform context", () => {
  it("sends the platform and connection on every request", () => {
    setActiveContext(DATABRICKS);

    expect(platformHeaders()).toEqual({
      "X-Acelo-Platform": "databricks",
      "X-Acelo-Connection-Id": "db-prod",
    });
  });

  it("switches context completely from Fabric to Databricks and back", () => {
    setActiveContext(FABRIC);
    expect(getActiveContext().platform).toBe("fabric");
    expect(isActive("databricks")).toBe(false);
    expect(platformHeaders()["X-Acelo-Connection-Id"]).toBe("fabric-prod");

    setActiveContext(DATABRICKS);
    expect(getActiveContext().platform).toBe("databricks");
    expect(isActive("fabric")).toBe(false);
    // No stale connection from the previous platform.
    expect(platformHeaders()["X-Acelo-Connection-Id"]).toBe("db-prod");

    setActiveContext(FABRIC);
    expect(getActiveContext().platform).toBe("fabric");
    expect(isActive("databricks")).toBe(false);
  });

  it("notifies subscribers so the whole UI follows a switch", () => {
    const seen: (string | null)[] = [];
    const unsubscribe = subscribe((ctx) => seen.push(ctx.platform));

    setActiveContext(DATABRICKS);
    setActiveContext(FABRIC);
    unsubscribe();
    setActiveContext(DATABRICKS);

    expect(seen).toEqual(["databricks", "fabric"]);
  });

  it("sends no platform headers when nothing is selected", () => {
    expect(platformHeaders()).toEqual({});
  });

  it("prefers the remembered connection, then a connected one", () => {
    setActiveContext(FABRIC); // remembers fabric-prod
    expect(resolveInitial([DATABRICKS, FABRIC])?.id).toBe("fabric-prod");

    resetActiveContext();
    const notConnected = { ...DATABRICKS, status: "pending" };
    expect(resolveInitial([notConnected, FABRIC])?.id).toBe("fabric-prod");
    expect(resolveInitial([])).toBeNull();
  });
});

describe("generic AI requests resolve within the active platform", () => {
  it("routes 'show me all clusters' to Databricks when Databricks is active", () => {
    setActiveContext(DATABRICKS);

    expect(isDatabricksComputeRequest("show me all clusters")).toBe(true);
    expect(isDatabricksComputeRequest("list my warehouses")).toBe(true);
    expect(isDatabricksComputeRequest("what compute do I have")).toBe(true);
  });

  it("does NOT route the same request to Databricks when Fabric is active", () => {
    setActiveContext(FABRIC);

    expect(isDatabricksComputeRequest("show me all clusters")).toBe(false);
    expect(isDatabricksComputeRequest("list my warehouses")).toBe(false);
  });

  it("still honours an explicit Databricks request from any context", () => {
    setActiveContext(FABRIC);

    expect(
      isDatabricksComputeRequest("Analyze my Databricks compute and find optimization opportunities."),
    ).toBe(true);
  });

  it("never routes a non-compute prompt to compute discovery", () => {
    setActiveContext(DATABRICKS);

    expect(isDatabricksComputeRequest("who approved this")).toBe(false);
    expect(isDatabricksComputeRequest("summarise last month")).toBe(false);
  });
});
