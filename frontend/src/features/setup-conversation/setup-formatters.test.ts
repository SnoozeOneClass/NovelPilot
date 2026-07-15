import { describe, expect, it } from "vitest";
import { formatBookTitle } from "./setup-formatters";

describe("formatBookTitle", () => {
  it("adds Chinese book-title marks when they are absent", () => {
    expect(formatBookTitle("退潮前的十一分钟")).toBe("《退潮前的十一分钟》");
  });

  it("does not duplicate existing book-title marks", () => {
    expect(formatBookTitle("《退潮前的十一分钟》")).toBe("《退潮前的十一分钟》");
  });
});
