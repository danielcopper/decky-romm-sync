/** Shared RomM connection state — set by RomMPlaySection, read by CustomPlayButton and sessionManager */
let _state: "checking" | "connected" | "offline" = "checking";
export function getRommConnectionState() {
  return _state;
}
export function setRommConnectionState(s: "checking" | "connected" | "offline") {
  _state = s;
}

/** Version mismatch error — set when server returns reason: "version_error" */
let _versionError: string | null = null;
const versionErrorListeners = new Set<(err: string | null) => void>();

export function getVersionError() {
  return _versionError;
}
export function setVersionError(msg: string | null) {
  if (_versionError === msg) return;
  _versionError = msg;
  versionErrorListeners.forEach((l) => l(msg));
}
export function onVersionErrorChange(cb: (err: string | null) => void): () => void {
  versionErrorListeners.add(cb);
  return () => {
    versionErrorListeners.delete(cb);
  };
}
