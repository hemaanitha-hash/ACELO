import "@testing-library/jest-dom/vitest";

// Per-tab conveniences (e.g. the Agent remembering its last run) must not leak between tests.
import { beforeEach as __beforeEach } from "vitest";
__beforeEach(() => {
  try {
    window.sessionStorage.clear();
    window.localStorage.clear();
  } catch {
    /* no DOM storage in this environment */
  }
});
