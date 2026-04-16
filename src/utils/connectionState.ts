/** Shared RomM connection state — set by RomMPlaySection, read by CustomPlayButton and sessionManager */
let _state: "checking" | "connected" | "offline" = "checking";
export function getRommConnectionState() { return _state; }
export function setRommConnectionState(s: "checking" | "connected" | "offline") { _state = s; }

/** Version mismatch error — set when server returns error_code: "version_error" */
let _versionError: string | null = null;
export function getVersionError() { return _versionError; }
export function setVersionError(msg: string | null) { _versionError = msg; }
