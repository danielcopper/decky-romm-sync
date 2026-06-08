/**
 * Public surface for the frontend type catalog. This file is a façade —
 * actual type definitions live in domain modules (api, sync, downloads,
 * firmware, saves, achievements). Consumers may deep-import the domain
 * module directly (`from "../types/saves"`) or use the broad re-export
 * surface here (`from "../types"`).
 */

export * from "./api";
export * from "./sync";
export * from "./downloads";
export * from "./firmware";
export * from "./saves";
export * from "./achievements";
export * from "./migration";
export * from "./devices";
export * from "./retrodeck";
