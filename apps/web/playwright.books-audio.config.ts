import { defineConfig } from "@playwright/test";
import books from "./playwright.books.config";

export default defineConfig({
  ...books,
  testMatch: "books-audio-joint.spec.ts",
  outputDir: "../../.runtime/books-audio-ui/test-results",
});
