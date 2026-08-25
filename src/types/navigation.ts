/**
 * QAM panel navigation — the set of pages the panel can show. Anything that
 * names a page the QAM router can land on lives here.
 */

/** Every page the QAM panel can show. A surface that cannot navigate to itself
 *  narrows this with `Exclude<Page, "...">` rather than restating the union. */
export type Page = "main" | "settings" | "library" | "data" | "downloads" | "system";
