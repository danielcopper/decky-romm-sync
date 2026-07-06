/**
 * One-time prompt for signing in to RomM — by minting a Client API Token from a
 * username + password, by pasting a token created in RomM's web UI, or by
 * entering a short-lived pairing code the plugin exchanges for a token (the two
 * token paths are for OIDC accounts, which have no password to mint from).
 *
 * The credentials and the pasted token are write-only: never pre-filled, never
 * echoed back by the backend. The pairing code is short-lived (60 seconds) and
 * single-use, so it is entered in the clear across eight single-character boxes
 * (an OTP-style input grouped 4 – 4 around a hyphen, auto-advancing as you type)
 * — typability matters more than obscuring a value that expires almost
 * immediately. On submit the parent's matching handler runs —
 * `connect_with_credentials` (exchanges the credentials for a scoped token and
 * discards the password), `connect_with_token` (validates and stores the pasted
 * token), or `connect_with_pairing_code` (exchanges the code for a token). This
 * modal owns only the in-flight field values and the selected mode; token
 * minting/validation, status, and persistence live in the parent (SettingsPage).
 */

import { FC, Fragment, useState, useRef, ChangeEvent, KeyboardEvent } from "react";
import { ConfirmModal, TextField, DropdownItem, Focusable } from "@decky/ui";

type SignInMode = "credentials" | "token" | "pairing";

interface ConnectModalProps {
  closeModal?: () => void;
  onConnect: (username: string, password: string) => void;
  onConnectToken: (token: string) => void;
  onConnectPairing: (code: string) => void;
}

// The pairing code is eight characters, shown as eight single-character boxes
// split into two groups of four around a hyphen.
const CODE_LENGTH = 8;
const CODE_GROUP = 4;
const CODE_INDICES = Array.from({ length: CODE_LENGTH }, (_unused, i) => i);

const helperTextStyle = { fontSize: "12px", marginBottom: "12px", color: "rgba(255,255,255,0.6)" } as const;
const codeLabelStyle = {
  fontSize: "12px",
  fontWeight: 600,
  marginBottom: "8px",
  color: "rgba(255,255,255,0.6)",
} as const;
// A single non-wrapping row: eight fixed-size boxes plus the hyphen must always
// stay on one line (8·36 (boxes) + 8·6 (gaps between the 9 flex children) + ~8
// (hyphen) ≈ 344px, comfortably inside the ConfirmModal's content width), so each
// box is a fixed size and never shrinks or grows as characters are typed.
const codeRowStyle = {
  display: "flex",
  flexWrap: "nowrap",
  justifyContent: "center",
  alignItems: "center",
  gap: "6px",
  width: "100%",
} as const;
// @decky/ui's TextField spreads the `style` prop straight onto its underlying
// <input>, so these metrics apply to the glyph directly. The input's default
// line-height/padding clips a single character when the cell is squeezed this
// narrow, so we take over its box model: an explicit box height (44px wrapper),
// zeroed padding, and a large centered font render the character fully and
// centered both ways.
// display:flex with the default align-items:stretch lets Steam's input fill the
// full box height (a portrait rectangle); centering it here would leave the input
// at its shorter intrinsic height and read as a square.
const codeBoxWrapperStyle = { flexShrink: 0, width: "36px", height: "48px", display: "flex" } as const;
const codeBoxFieldStyle = {
  minWidth: 0,
  width: "100%",
  height: "100%",
  boxSizing: "border-box",
  padding: "0",
  textAlign: "center",
  fontSize: "20px",
  lineHeight: "1.2",
} as const;
// Match the boxes' 44px flex-centered block so the hyphen shares their exact
// vertical centerline (a bare inline span aligns by text baseline, which reads
// as a minor offset next to the input boxes).
const codeSeparatorStyle = {
  flexShrink: 0,
  height: "48px",
  display: "flex",
  alignItems: "center",
  fontSize: "18px",
  lineHeight: 1,
  color: "rgba(255,255,255,0.6)",
} as const;

/**
 * Reduce a pairing-code fragment to uppercased alphanumerics — strips any
 * whitespace, hyphen, or stray punctuation a paste or keystroke introduced. The
 * backend normalizes identically, so this is the canonical form each keystroke
 * or paste reduces to before it is placed into or distributed across the
 * single-character boxes.
 */
const normalizePairingCode = (value: string): string => value.replace(/[^A-Za-z0-9]/g, "").toUpperCase();

export const ConnectModal: FC<ConnectModalProps> = ({ closeModal, onConnect, onConnectToken, onConnectPairing }) => {
  const [mode, setMode] = useState<SignInMode>("pairing");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [code, setCode] = useState<string[]>(() => new Array<string>(CODE_LENGTH).fill(""));

  // The @decky/ui TextField is a plain FC with no ref to its underlying input,
  // so focus is moved via the wrapping row (querySelectorAll('input')[i] — the
  // same DOM pattern the QAM uses for buttons) rather than a forwarded ref. The
  // eight inputs sit in DOM order inside the row, so index maps to box.
  const codeRowRef = useRef<HTMLDivElement>(null);

  const boxInputAt = (index: number): HTMLInputElement | null =>
    codeRowRef.current?.querySelectorAll("input")[index] ?? null;

  const focusBox = (index: number) => {
    boxInputAt(index)?.focus();
  };

  const focusBoxAtEnd = (index: number) => {
    const input = boxInputAt(index);
    if (!input) return;
    input.focus();
    const end = input.value.length;
    input.setSelectionRange(end, end);
  };

  const setBoxChar = (index: number, char: string) => {
    setCode((prev) => prev.map((current, idx) => (idx === index ? char : current)));
  };

  // Write `chars` into consecutive boxes starting at `start`, then land the caret
  // on the first still-empty box — or blur (dismissing the keyboard) once the
  // whole code is filled. Used for pastes and any multi-character keystroke.
  const distributeChars = (start: number, chars: string) => {
    setCode((prev) => {
      const next = [...prev];
      for (let offset = 0; offset < chars.length && start + offset < CODE_LENGTH; offset += 1) {
        next[start + offset] = chars.charAt(offset);
      }
      return next;
    });
    const filledThrough = start + chars.length;
    if (filledThrough >= CODE_LENGTH) {
      (document.activeElement as HTMLElement | null)?.blur();
    } else {
      focusBox(filledThrough);
    }
  };

  const handleBoxChange = (index: number) => (e: ChangeEvent<HTMLInputElement>) => {
    const incoming = normalizePairingCode(e.target.value);
    if (incoming.length <= 1) {
      // A single character (or a deletion, which leaves the box empty). Advancing
      // only on a real character, so a delete never jumps focus forward.
      setBoxChar(index, incoming);
      if (incoming.length === 1) focusBox(index + 1);
      return;
    }
    // Multiple characters at once — a paste, or typing past a box that already
    // holds a character. Drop a leading copy of the box's existing character (a
    // type-over prepends it) so only the fresh characters are distributed.
    const existing = code[index] ?? "";
    const fresh = existing.length > 0 && incoming.startsWith(existing) ? incoming.slice(existing.length) : incoming;
    distributeChars(index, fresh);
  };

  const handleBoxKeyDown = (index: number) => (e: KeyboardEvent<HTMLInputElement>) => {
    // Standard OTP behavior: Backspace on an already-empty box steps focus back
    // to the previous box with the caret at the end.
    if (e.key === "Backspace" && (code[index] ?? "") === "" && index > 0) {
      focusBoxAtEnd(index - 1);
    }
  };

  const submit = () => {
    if (mode === "token") {
      onConnectToken(token);
    } else if (mode === "pairing") {
      // Backend normalizes, so the bare eight characters in order are enough.
      onConnectPairing(code.join(""));
    } else {
      onConnect(username, password);
    }
  };

  return (
    <ConfirmModal
      {...(closeModal === undefined ? {} : { closeModal })}
      onOK={submit}
      strTitle="Sign in to RomM"
      strOKButtonText="Sign in"
      bDisableBackgroundDismiss={true}
    >
      <DropdownItem
        label="Sign-in method"
        rgOptions={[
          { data: "pairing", label: "Pairing code" },
          { data: "token", label: "API token" },
          { data: "credentials", label: "Username & password" },
        ]}
        selectedOption={mode}
        onChange={(option) => setMode(option.data as SignInMode)}
      />
      {mode === "token" && (
        <>
          <div style={helperTextStyle}>
            Create a token in RomM&apos;s web UI (Settings → API Tokens) and paste it here. Make sure it has the scopes
            listed in the plugin docs so downloads, saves, and device sync work. The plugin never deletes a pasted
            token; you manage it in RomM.
          </div>
          <TextField
            focusOnMount={true}
            label="API Token"
            value={token}
            bIsPassword
            onChange={(e: ChangeEvent<HTMLInputElement>) => setToken(e.target.value)}
          />
        </>
      )}
      {mode === "pairing" && (
        <>
          <div style={helperTextStyle}>
            In RomM&apos;s web UI open your API token and click <strong>Pair</strong>, then enter the 8-character code
            here within 60 seconds. The plugin fetches the token itself — nothing to copy or paste.
          </div>
          <div style={codeLabelStyle}>Pairing code</div>
          {/* Focusable + flow-children="horizontal" tells Steam's gamepad nav to
              step between the boxes left/right (the analog stick/d-pad), not
              up/down — a plain div defaults to vertical traversal. */}
          <Focusable flow-children="horizontal" ref={codeRowRef} style={codeRowStyle} data-testid="pairing-code-row">
            {CODE_INDICES.map((i) => (
              <Fragment key={i}>
                {i === CODE_GROUP && <span style={codeSeparatorStyle}>-</span>}
                <div style={codeBoxWrapperStyle}>
                  <TextField
                    focusOnMount={i === 0}
                    style={codeBoxFieldStyle}
                    value={code[i] ?? ""}
                    onChange={handleBoxChange(i)}
                    onKeyDown={handleBoxKeyDown(i)}
                  />
                </div>
              </Fragment>
            ))}
          </Focusable>
        </>
      )}
      {mode === "credentials" && (
        <>
          <div style={helperTextStyle}>
            Enter your RomM username and password once. The plugin exchanges them for an API token and never stores your
            password.
          </div>
          <TextField
            focusOnMount={true}
            label="Username"
            value={username}
            onChange={(e: ChangeEvent<HTMLInputElement>) => setUsername(e.target.value)}
          />
          <TextField
            label="Password"
            value={password}
            bIsPassword
            onChange={(e: ChangeEvent<HTMLInputElement>) => setPassword(e.target.value)}
          />
        </>
      )}
    </ConfirmModal>
  );
};
