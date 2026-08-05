/**
 * The launch-target probe both launch paths run before starting a ROM.
 *
 * A download can succeed and still leave nothing the system can boot — a PS3
 * title shipped as a `.pkg` installer, a disc rip whose only sizeable file is a
 * raw track. The backend records that verdict on the install (`launchable`) and
 * withholds the shortcut's launch command, so a launch would start nothing and
 * explain nothing. This is the read the gate blocks on.
 *
 * Lives apart from `launchGate.ts` because the gate itself performs no I/O —
 * every side effect reaches it as an injected callback, and this is the callback
 * both the Play button and the global watcher inject.
 */

import { getInstalledRom, logError } from "../api/backend";

/**
 * Toast copy both launch paths surface on a `no_launch_target` block. Says what
 * happened, that nothing was thrown away, and where the detail lives.
 */
export const NO_LAUNCH_TARGET_TOAST_BODY =
  "This download has no file the emulator can launch. The files are on disk — see the game page.";

/**
 * Does `romId` have a launch target? `context` names the caller in the error log.
 *
 * Fails **open**: a transport hiccup or a missing install record resolves to
 * `true`. Blocking is reserved for a verdict the backend actually returned —
 * a launch trapped behind a failed probe is worse than one that starts nothing,
 * because the ROM whose install record is unreadable is usually fine.
 */
export async function romHasLaunchTarget(romId: number, context: string): Promise<boolean> {
  const installed = await getInstalledRom(romId).catch((e) => {
    logError(`${context} launch-target check threw (allowing launch): ${e}`);
    return null;
  });
  return installed == null || installed.launchable;
}
